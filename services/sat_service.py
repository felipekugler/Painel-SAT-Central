"""Serviço da feature "Arquivos SAT" (Painel SAT Central).

Orquestra, para um PAJ incluído MANUALMENTE (busca global no SISDPU por
número — este painel não lê a caixa de entrada automaticamente):

    baixar os PDFs do SAT Central (Grupo 1/2 + PAT, via ingestao.sat_client) →
    movimentação (fase "Juntada de documento", anexos = PDFs como
    "Documentos do Assistido") → tramitação (caixa configurável).

Como o PAJ não vem de um trâmite da caixa, NÃO há conclusão (não existe
trâmite originário para concluir) — o fluxo termina na tramitação. Os PDFs
ficam em `PAJS_DIR/<paj_norm>/Arquivos SAT/` e o progresso é transmitido via
SSE, com o resultado gravado em `historico_sat`.
"""

from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import os
import re
import shutil
import time
import unicodedata
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator

_log = logging.getLogger("sat.service")

import config
from ingestao import sisdpu_movtram as movtram
from services import historico_sat

# --- parâmetros de negócio ---
FASE_MOV = "Juntada de documento"
TIPO_ARQUIVO = "Documentos do Assistido"
MOV_DESCRICAO = "Juntada dos documentos obtidos junto ao SAT Central (INSS/Dataprev)"
MOV_DESCRICAO_COMPLEMENTAR = ("Juntada complementar de documentos obtidos junto ao SAT "
                              "Central (INSS/Dataprev)")
TRAM_DESCRICAO = "Documentos do SAT Central juntados ao PAJ"
GRUPO_PADRAO = "01 - PREV_DIVPREV"
# Sentinela de "grupo" que representa a opção "Não tramitar" (só movimenta/anexa
# os documentos; pula a etapa de tramitação por completo).
NAO_TRAMITAR = "__nao_tramitar__"
# Estrutura de pastas (2026-11-10) — UMA SUBPASTA POR PESSOA dentro de "Arquivos SAT":
#   PAJ-…/Arquivos SAT/
#       Assistido1 <CPF> - <NOME>/     ← assistido do PAJ (PDFs + _log_download.json +
#                                         Excluídos/ + Complementar/ + Compilados (originais)/)
#       Assistido2 <CPF> - <NOME>/     ← outro assistido (CPF adicional)
#       Instituidor <CPF> - <NOME>/    ← instituidor (pensão por morte / auxílio-reclusão)
#       Terceiro <CPF> - <NOME>/       ← terceiro
#       _cpfs_extras.json              ← CPFs adicionais (com a pasta de cada um)
# Todas as pastas de pessoa têm a MESMA estrutura interna; a juntada pega os PDFs do
# primeiro nível de cada uma.
SUBPASTA_SAT = "Arquivos SAT"
SUBPASTA_COMPLEMENTAR = "Complementar"  # download complementar (dentro da pasta da pessoa)
# CPFs adicionais do PAJ (instituidor / outros assistidos / terceiro) cadastrados pelo "+"
# da fila automática: lista em _cpfs_extras.json, cada um com a sua pasta de pessoa; ao
# excluir o CPF, a pasta inteira dele vai para a Lixeira (nada dela é juntado).
PAPEIS_EXTRAS = ("Instituidor", "Outro assistido", "Terceiro")
# Nome de pasta de pessoa: "Assistido1 …", "Assistido2 …", "Instituidor …", "Terceiro …"
_RE_PASTA_PESSOA = re.compile(r"^(Assistido\d+|Instituidor|Terceiro)(?: |$)")
# Originais dos PDFs compilados por tipo (espelha sat_client.SUBPASTA_ORIGINAIS_COMPILADOS).
# Ficam fora da anexação; apagados só APÓS a conclusão da tramitação (ver processar_paj_stream).
SUBPASTA_ORIGINAIS_COMPILADOS = "Compilados (originais)"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip().lower()


def _extrair_pretensao_pura(pretensao: str) -> str:
    """Parte após ' >> ' da pretensão do SISDPU, removendo a área temática."""
    if not pretensao:
        return ""
    partes = pretensao.split(">>")
    return partes[1].strip() if len(partes) > 1 else pretensao.strip()


def _num_to_norm(paj: str) -> str:
    """'2026/029-01651' -> 'PAJ-2026-029-01651'."""
    m = re.match(r"(\d{4})/(\d{3})-(\d+)", paj or "")
    return f"PAJ-{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else (paj or "")


def _norm_to_num(paj_norm: str) -> str:
    """'PAJ-2026-029-01651' -> '2026/029-01651'."""
    m = re.match(r"PAJ-(\d{4})-(\d{3})-(\d+)", paj_norm or "")
    return f"{m.group(1)}/{m.group(2)}-{m.group(3)}" if m else (paj_norm or "")


def _pasta_paj(paj_norm: str) -> Path:
    return Path(config.PAJS_DIR) / paj_norm


def _ler_metadata(paj_norm: str) -> dict:
    p = _pasta_paj(paj_norm) / "metadata.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _cpf_do_paj(paj_norm: str) -> str:
    meta = _ler_metadata(paj_norm)
    raw = (meta.get("sisdpu_raw") or {})
    return (raw.get("cpf_assistido") or "").strip()


def _html_para_texto(h: str) -> str:
    """Converte o HTML da narrativa do SISDPU em texto limpo, preservando parágrafos."""
    if not h:
        return ""
    h = re.sub(r"(?i)<\s*br\s*/?>", "\n", h)
    h = re.sub(r"(?i)</\s*(p|div|tr|li)\s*>", "\n", h)
    h = re.sub(r"<[^>]+>", "", h)          # remove as tags restantes
    h = _html.unescape(h)
    h = h.replace("\xa0", " ")
    h = re.sub(r"[ \t]+", " ", h)
    h = re.sub(r" *\n *", "\n", h)
    h = re.sub(r"\n{3,}", "\n\n", h)
    return h.strip()


def narrativa_paj(paj_norm: str) -> dict:
    """Texto da narrativa do PAJ. Este painel não tem sincronização completa
    (sem metadata.json) — a narrativa vem do que foi capturado ao vivo na busca
    global do SISDPU quando o PAJ foi cadastrado manualmente."""
    meta = _ler_metadata(paj_norm)
    raw = (meta.get("sisdpu_raw") or {})
    texto = _html_para_texto(raw.get("narrativa") or "")
    if not texto:
        mp = next((m for m in _ler_pajs_manuais() if m.get("paj_norm") == paj_norm), None)
        texto = (mp or {}).get("narrativa", "")
    return {"paj_norm": paj_norm, "texto": texto}


def raiz_sat(paj_norm: str) -> Path:
    """Pasta 'Arquivos SAT' do PAJ (contém uma subpasta por pessoa)."""
    return _pasta_paj(paj_norm) / SUBPASTA_SAT


def _nome_limpo(nome: str) -> str:
    n = re.sub(r'[\\/:*?"<>|]+', " ", nome or "")
    return re.sub(r"\s+", " ", n).strip().strip(".").upper()


def nome_pasta_pessoa(classe: str, cpf: str, nome: str = "") -> str:
    """'Assistido1 111.222.333-44 - FULANO' (sem nome: 'Assistido1 111.222.333-44')."""
    dig = _so_digitos(cpf)
    base = classe + (f" {_fmt_cpf(dig)}" if dig else "")
    n = _nome_limpo(nome)
    return f"{base} - {n}" if n else base


def _achar_pasta_pessoa(paj_norm: str, classe: str, cpf: str = "") -> Path | None:
    """1ª subpasta de pessoa da classe (e do CPF, se dado) já existente em 'Arquivos SAT'."""
    raiz = raiz_sat(paj_norm)
    if not raiz.is_dir():
        return None
    alvo_cpf = _fmt_cpf(_so_digitos(cpf)) if cpf else ""
    for d in sorted(raiz.iterdir()):
        if d.is_dir() and (d.name == classe or d.name.startswith(classe + " ")):
            if alvo_cpf and alvo_cpf not in d.name:
                continue
            return d
    return None


def _cpf_nome_assistido(paj_norm: str) -> tuple[str, str]:
    """CPF/nome do assistido do PAJ pelo que o painel já conhece (manual, metadata, caixa)."""
    mp = next((m for m in _ler_pajs_manuais() if m.get("paj_norm") == paj_norm), None) or {}
    cpf, nome = mp.get("cpf", ""), mp.get("assistido", "")
    if not cpf:
        cpf = _cpf_do_paj(paj_norm)
    cache = (globals().get("_cache_detalhes_caixa") or {}).get(paj_norm)
    if cache:
        cpf = cpf or cache[1].get("cpf", "")
        nome = nome or cache[1].get("assistido", "")
    return cpf, nome


def pasta_arquivos_sat(paj_norm: str, cpf: str = "", nome: str = "") -> Path:
    """Pasta do ASSISTIDO do PAJ: 'Arquivos SAT/Assistido1 <CPF> - <NOME>'. Usa a já
    existente; senão monta o nome com o CPF/nome informados (ou conhecidos pelo painel)."""
    achada = _achar_pasta_pessoa(paj_norm, "Assistido1")
    if achada:
        return achada
    if not cpf:
        c, n = _cpf_nome_assistido(paj_norm)
        cpf, nome = c, (nome or n)
    return raiz_sat(paj_norm) / nome_pasta_pessoa("Assistido1", cpf, nome)


def listar_arquivos_sat(paj_norm: str) -> list[dict]:
    """PDFs já baixados na subpasta 'Arquivos SAT' (ignora o _LOG e não-PDF)."""
    pasta = pasta_arquivos_sat(paj_norm)
    if not pasta.exists():
        return []
    out = []
    for f in sorted(pasta.glob("*.pdf")):
        try:
            tam = f.stat().st_size
        except Exception:  # noqa: BLE001
            tam = 0
        out.append({"nome": f.name, "caminho": str(f), "tipo": "pdf", "tamanho": tam})
    return out


def pasta_instituidor(paj_norm: str, cpf: str = "", nome: str = "") -> Path:
    """Pasta do INSTITUIDOR: 'Arquivos SAT/Instituidor <CPF> - <NOME>'. Com CPF, a desse
    CPF; sem CPF, a do 1º instituidor cadastrado (ou a 1ª pasta 'Instituidor …')."""
    if cpf:
        achada = _achar_pasta_pessoa(paj_norm, "Instituidor", cpf)
        if achada:
            return achada
        dig = _so_digitos(cpf)
        x = next((x for x in listar_cpfs_extras(paj_norm) if _so_digitos(x.get("cpf", "")) == dig), None)
        if x and x.get("pasta"):
            return raiz_sat(paj_norm) / x["pasta"]
        return raiz_sat(paj_norm) / nome_pasta_pessoa("Instituidor", cpf, nome)
    x = next((x for x in listar_cpfs_extras(paj_norm) if x.get("papel") == "Instituidor"), None)
    if x:
        return pasta_cpf_extra(paj_norm, x)
    return _achar_pasta_pessoa(paj_norm, "Instituidor") or (raiz_sat(paj_norm) / "Instituidor")


def pastas_pessoas(paj_norm: str) -> list[Path]:
    """Todas as subpastas de pessoa existentes: Assistido1 primeiro, depois as demais."""
    raiz = raiz_sat(paj_norm)
    if not raiz.is_dir():
        return []
    ds = [d for d in raiz.iterdir() if d.is_dir() and _RE_PASTA_PESSOA.match(d.name)]

    def ordem(d: Path):
        m = re.match(r"Assistido(\d+)", d.name)
        if m:
            return (0, int(m.group(1)), d.name)
        return (1 if d.name.startswith("Instituidor") else 2, 0, d.name)
    return sorted(ds, key=ordem)


def rotulo_pessoa(pasta: Path) -> str:
    """'Assistido', 'Assistido 2', 'Instituidor', 'Terceiro' — a partir do nome da pasta."""
    m = re.match(r"Assistido(\d+)", pasta.name)
    if m:
        return "Assistido" if m.group(1) == "1" else f"Assistido {m.group(1)}"
    return "Instituidor" if pasta.name.startswith("Instituidor") else "Terceiro"


def listar_arquivos_instituidor(paj_norm: str) -> list[dict]:
    """PDFs baixados do instituidor (subpasta 'Instituidor')."""
    pasta = pasta_instituidor(paj_norm)
    if not pasta.exists():
        return []
    out = []
    for f in sorted(pasta.glob("*.pdf")):
        try:
            tam = f.stat().st_size
        except Exception:  # noqa: BLE001
            tam = 0
        out.append({"nome": f.name, "caminho": str(f), "tipo": "pdf", "tamanho": tam})
    return out


_STATUS_FALHA = {"falha", "antirobo", "indisponivel"}


# ---------------------------------------------------------------------------
# Lista de BATIMENTO (modelo 2026-11-12, pedido do usuário): na 1ª rodada de cada PAJ,
# um levantamento COMPLETO do SAT (todo documento existente para o CPF, independente da
# pretensão) é feito ANTES do download; cruzado com a pretensão (tipos autorizados) e o
# filtro de NB/protocolo do PAJ, vira esta lista — um item por documento, cada um com uma
# flag `incluir` (o que deve ser buscado) e o `status` da última tentativa. Da 2ª rodada
# em diante, NINGUÉM mais recalcula tipos/filtros: só esta lista manda. No modal, marcar
# ou desmarcar um item ainda não baixado só ajusta `incluir` — sempre reversível, nunca
# um bloqueio permanente (ao contrário do mecanismo de "ignorar" que existiu antes disso).
# ---------------------------------------------------------------------------

def _arq_batimento(pasta: Path) -> Path:
    return pasta / "_batimento.json"


def ler_batimento(pasta: Path) -> dict:
    """{"ts", "itens": [{key, sub, rotulo, incluir, status, nome, detalhe}, ...]} desta
    pasta (assistido/instituidor/CPF extra). {} se o PAJ ainda não passou pela 1ª rodada
    deste modelo (levantamento completo + batimento)."""
    try:
        return json.loads(_arq_batimento(pasta).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _salvar_batimento(pasta: Path, itens: list[dict], revisado: bool | None = None) -> None:
    """`revisado` None preserva o valor já gravado (ou False se não houver nenhum ainda);
    só quem de fato revisa a seleção (`sincronizar_incluir_batimento`, chamado por "Baixar
    mais"/"Salvar seleção") passa True explicitamente."""
    if revisado is None:
        revisado = bool(ler_batimento(pasta).get("revisado"))
    pasta.mkdir(parents=True, exist_ok=True)
    _arq_batimento(pasta).write_text(
        json.dumps({"ts": datetime.now().strftime("%d/%m/%Y %H:%M"), "itens": itens,
                    "revisado": revisado}, ensure_ascii=False, indent=2), encoding="utf-8")


def _pendentes_batimento(itens: list[dict]) -> list[dict]:
    """Itens com `incluir=True` e ainda não baixados — o que a próxima rodada deve
    buscar no SAT (e SÓ isso: a partir da 2ª rodada, pretensão/filtro não são
    reconsultados — a lista já é a decisão)."""
    return [it for it in itens if it.get("incluir") and it.get("status") != "baixado"]


def _alvos_pendentes_batimento(itens: list[dict]) -> tuple[list[str], list[str]]:
    """(tipos, alvos) dos pendentes — ver `_pendentes_batimento`."""
    pend = _pendentes_batimento(itens)
    alvos = [f"{it.get('key', '')}|{it.get('sub', '')}" for it in pend]
    tipos = sorted({it.get("key", "") for it in pend if it.get("key")})
    return tipos, alvos


def tem_pendencia_batimento(pasta: Path) -> bool:
    return bool(_pendentes_batimento(ler_batimento(pasta).get("itens") or []))


def _contar_pendentes_batimento(pasta: Path) -> int:
    return len(_pendentes_batimento(ler_batimento(pasta).get("itens") or []))


def _montar_batimento_inicial(pasta: Path, tipos_sel: list[str] | None, filtros: dict) -> list[dict]:
    """Monta a lista de batimento a partir do levantamento COMPLETO (`_levantamento.json`,
    já gravado por `sat_client.levantar_cidadao_stream`, que enumera TODO documento do
    SAT para o CPF, independente da pretensão) cruzado com `tipos_sel` (tipos autorizados
    pela pretensão/config — None = todos) e o filtro de NB/protocolo deste PAJ. Define a
    flag `incluir` inicial de cada item. Só é chamada na 1ª rodada."""
    from ingestao import sat_utils
    try:
        lev = json.loads((pasta / "_levantamento.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        lev = {}
    nb_filtro = {sat_utils.nb_digits(x) for x in _itens_filtro(filtros.get("nbs", ""))}
    nb_filtro.discard("")
    prot_filtro = {re.sub(r"\D", "", x) for x in _itens_filtro(filtros.get("protocolos", ""))}
    prot_filtro.discard("")
    itens: list[dict] = []
    for it in lev.get("itens") or []:
        key, sub, rotulo = it.get("key", ""), it.get("sub", ""), it.get("rotulo", "")
        status_lev = it.get("status", "")
        if status_lev in ("sem_dados", "indisponivel", "antirobo"):
            # Levantamento não revelou nenhuma linha concreta deste tipo (modal não abriu
            # ou tabela veio vazia) — nada a incluir, só registra o motivo. Além do status
            # genérico, acrescenta a mensagem EXATA reportada pelo SAT (quando houver), para
            # que o usuário veja o motivo real em vez de só "sem dados"/"indisponivel" —
            # pedido do usuário, 2026-11-12.
            msg_sat = (it.get("detalhe") or "").strip()
            detalhe = f"levantamento: {status_lev.replace('_', ' ')}"
            if msg_sat:
                detalhe += f". {msg_sat}"
            itens.append({"key": key, "sub": sub, "rotulo": rotulo, "incluir": False,
                         "status": "", "nome": "", "detalhe": detalhe})
            continue
        incluir, motivo = True, ""
        if tipos_sel is not None and key not in tipos_sel:
            incluir, motivo = False, "Download não previsto para essa pretensão"
        elif key == "pat" and prot_filtro and re.sub(r"\D", "", sub) not in prot_filtro:
            incluir, motivo = False, "fora do filtro de protocolo"
        elif key != "pat" and sub and nb_filtro and sat_utils.nb_digits(sub) not in nb_filtro:
            incluir, motivo = False, "fora do filtro de NB"
        itens.append({"key": key, "sub": sub, "rotulo": rotulo, "incluir": incluir,
                     "status": "", "nome": "", "detalhe": motivo})
    return itens


def _tem_levantamento_pendente(itens: list[dict]) -> bool:
    """True se algum item do batimento ficou sem levantamento de verdade na 1ª
    tentativa — bloqueado por antirrobô (claramente transitório). "sem_dados"/
    "indisponivel" não entram aqui: o SAT respondeu, só que vazio/indisponível —
    mais provável de ser definitivo do que um bloqueio de antirrobô."""
    return any((it.get("detalhe") or "").startswith("levantamento: antirobo") for it in itens)


def _mesclar_novo_levantamento_em_batimento(pasta: Path, tipos_sel: list[str] | None, filtros: dict) -> bool:
    """Refaz `_montar_batimento_inicial` sobre um `_levantamento.json` recém-atualizado
    (depois de uma nova chamada a `levantar_cidadao_stream`) e MESCLA o resultado na
    lista de batimento já existente: só acrescenta itens (key, sub) novos que vieram de
    um tipo que tinha ficado pendente por antirrobô, e só substitui o(s) placeholder(s)
    que ainda estavam "levantamento: antirobo" — nunca mexe no que já foi decidido
    (incluído/excluído, manual ou automaticamente) ou já baixado.

    Quando um tipo pendente é RESOLVIDO (a nova tentativa revela itens de verdade, em vez
    de continuar reportando antirrobô), o placeholder antigo "levantamento: antirobo"
    (key, sub="") é REMOVIDO da lista — senão ele fica marcado como pendente para sempre
    e `_tem_levantamento_pendente`/`_download_tem_falha` nunca voltam a False, mesmo
    depois de o usuário decidir e salvar a seleção dos itens de verdade (bug identificado
    2026-11-13: PAJ ficava "Incompleto" para sempre mesmo sem nada mais pendente). Retorna
    True se algo mudou."""
    bat = ler_batimento(pasta)
    itens = bat.get("itens") or []
    if not itens:
        return False
    idx = {(it.get("key", ""), it.get("sub", "")): i for i, it in enumerate(itens)}
    tipos_pendentes = {it.get("key", "") for it in itens
                       if (it.get("detalhe") or "").startswith("levantamento: antirobo")}
    novo_bruto = _montar_batimento_inicial(pasta, tipos_sel, filtros)
    chaves_novo = {(n.get("key", ""), n.get("sub", "")) for n in novo_bruto}
    mudou = False
    for novo in novo_bruto:
        chave = (novo.get("key", ""), novo.get("sub", ""))
        if chave in idx:
            atual = itens[idx[chave]]
            if (atual.get("detalhe") or "").startswith("levantamento: antirobo"):
                itens[idx[chave]] = novo
                mudou = True
        elif novo.get("key", "") in tipos_pendentes:
            itens.append(novo)
            idx[chave] = len(itens) - 1
            mudou = True
    for key in tipos_pendentes:
        chave_placeholder = (key, "")
        i = idx.get(chave_placeholder)
        if i is None or i >= len(itens) or itens[i] is None:
            continue
        if not (itens[i].get("detalhe") or "").startswith("levantamento: antirobo"):
            continue
        if chave_placeholder not in chaves_novo:  # tipo resolvido — não bloqueia mais
            itens[i] = None
            mudou = True
    if mudou:
        itens = [it for it in itens if it is not None]
        _salvar_batimento(pasta, itens)
    return mudou


def _mesclar_resultado_em_batimento(pasta: Path) -> None:
    """Depois de um download, atualiza status/nome/detalhe de cada item da lista de
    batimento que foi de fato tentado. A flag `incluir` normalmente NÃO é tocada aqui —
    só a montagem inicial ou o usuário (pelo modal) a definem — com UMA exceção abaixo
    ("sem_dados" descoberto só agora, na tentativa real de download).

    Duas proteções adicionadas em 2026-11-13, depois de dois PAJs reais reportarem
    "download incompleto" sem nenhum documento com falha de verdade:
    1. Um item já `baixado` NUNCA regride para outro status. Sem isto, uma rodada
       seguinte — que não seleciona mais esse tipo, por já estar pronto — gravava
       genericamente "nao_selecionado" no log, e essa mesclagem sobrescrevia o
       "baixado" correto, fazendo a lista achar pra sempre que ainda faltava baixar
       algo que já estava no disco.
    2. "sem_dados" (o SAT confirmou que o documento não existe, descoberto só agora —
       diferente do "sem_dados" do levantamento, que já vira `incluir=False` na
       montagem inicial) passa a ser tratado como resultado DEFINITIVO: `incluir` vira
       False, para não ficar "pendente" para sempre nem ser reconsultado a cada rodada
       por algo que nunca vai existir. Isto não é uma falha — `falha`/`antirobo`/
       `indisponivel` continuam retentáveis normalmente, sem mudança."""
    bat = ler_batimento(pasta)
    itens = bat.get("itens") or []
    if not itens:
        return
    idx = {(it.get("key", ""), it.get("sub", "")): i for i, it in enumerate(itens)}
    try:
        log = json.loads((pasta / "_log_download.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return
    for d in log.get("inventario") or []:
        chave = (d.get("key", ""), d.get("sub", ""))
        if chave not in idx:
            continue  # item fora da lista de batimento — nunca deveria ocorrer, ignora
        it = itens[idx[chave]]
        if it.get("status") == "baixado":
            continue  # já baixado — nunca regride, não importa o que esta rodada diga
        novo_status = d.get("status", it.get("status", ""))
        it["status"] = novo_status
        it["nome"] = d.get("nome", it.get("nome", ""))
        it["detalhe"] = d.get("detalhe") or it.get("detalhe", "")
        if d.get("rotulo"):
            it["rotulo"] = d["rotulo"]
        if d.get("nb"):
            it["nb"] = d["nb"]
        if novo_status == "sem_dados":
            it["incluir"] = False
    _salvar_batimento(pasta, itens)


def sincronizar_incluir_batimento(paj_norm: str, alvos_csv: str, escopo: str = "assistido") -> dict:
    """Sincroniza a flag `incluir` com o que está marcado no modal AGORA: os alvos
    enviados (key|sub) ficam incluir=True; todo o resto da lista (ainda não baixado) vira
    incluir=False. Sempre reversível — nenhuma ação aqui é permanente.

    Marca a lista inteira como `revisado=True`: o usuário acabou de olhar o inventário
    completo (todos os grupos) neste modal e confirmar o que quer baixar — mesmo que um
    grupo continue acima do limite de "Múltiplos Requerimentos", essa é uma escolha
    consciente, não a seleção padrão intocada. A partir daqui a fila para de pausar este
    PAJ por esse motivo e passa a baixar normalmente o que ficou `incluir=True` (pedido do
    usuário, 2026-11-13)."""
    pasta = _escopo_paths(paj_norm, escopo)["destino"]
    bat = ler_batimento(pasta)
    itens = bat.get("itens") or []
    if not itens:
        return {"ok": False, "erro": "Este PAJ ainda não tem lista de batimento (precisa "
                                     "passar pela 1ª rodada — levantamento + download)."}
    marcados = {a for a in (alvos_csv or "").split(",") if a}
    for it in itens:
        if it.get("status") == "baixado":
            continue  # já baixado: incluir não importa mais, não mexe
        it["incluir"] = f"{it.get('key', '')}|{it.get('sub', '')}" in marcados
    _salvar_batimento(pasta, itens, revisado=True)
    return {"ok": True, "tem_falha": tem_pendencia_batimento(pasta)}


def _download_tem_falha(paj_norm: str, pasta: Path | None = None) -> bool:
    """True se o PAJ (ou pasta de CPF extra) ainda tem documento pendente: na lista de
    batimento (modelo novo) ou, para PAJs que ainda não têm batimento (modelo antigo),
    no último log de download. Um levantamento ainda pendente por bloqueio de antirrobô
    TAMBÉM conta como falha/incompleto — mesmo sem nenhum item `incluir=True` pendente —
    senão o PAJ fica marcado como "pronto" para sempre e a nova tentativa de
    levantamento (ver `_tem_levantamento_pendente`) nunca chega a rodar de novo."""
    p = pasta or pasta_arquivos_sat(paj_norm)
    bat_itens = ler_batimento(p).get("itens")
    if bat_itens:
        return tem_pendencia_batimento(p) or _tem_levantamento_pendente(bat_itens)
    log_p = p / "_log_download.json"
    if not os.path.isfile(str(log_p)):
        return False
    try:
        d = json.loads(log_p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    # Log ainda "parcial" (rotina interrompida antes do fim — conexão perdida, exceção
    # inesperada etc.) NUNCA conta como completo, mesmo sem nenhum status de falha
    # explícito registrado — sem isto, um PAJ cuja rotina caiu bem no início (antes de
    # sequer tentar os documentos) podia ficar marcado "pronto para anexar" com ZERO
    # documentos baixados (identificado 2026-11-13, a partir do PAJ 04410).
    if d.get("parcial"):
        return True
    if any(it.get("status") in _STATUS_FALHA for it in (d.get("inventario") or [])):
        return True
    return any(ev.get("level") == "erro" for ev in (d.get("eventos") or []))


def _grupos_acima_do_limite(itens: list[dict], limiar: int) -> dict[str, int]:
    """{key: quantidade} dos grupos da lista de batimento com mais de `limiar` itens
    PENDENTES (incluir=True e ainda não baixados). Não precisa de uma lista fixa de
    "quais grupos contam" — um tipo de documento único (CNIS, Declaração de Benefícios
    etc.) nunca tem mais de 1 item por natureza, então nunca dispara sozinho; só tipos em
    grupo/tabela (Carta de Concessão, CRER, Avaliação Social, Laudo Médico, Tarefas PAT,
    Histórico de Empréstimos Consignados e qualquer outro que apareça em grupo) de fato
    acumulam vários itens e podem ultrapassar o limite — pedido do usuário, 2026-11-13."""
    from collections import Counter
    cont: Counter = Counter()
    for it in itens:
        if it.get("incluir") and it.get("status") != "baixado" and it.get("key"):
            cont[it["key"]] += 1
    return {k: n for k, n in cont.items() if n > limiar}


def _multiplos_requerimentos(pasta: Path) -> tuple[bool, str, bool]:
    """(tem_multiplos, detalhe, revisado) — se algum grupo da lista de batimento desta
    pasta tem mais documentos pendentes do que o limite configurado (Configurações ›
    Parâmetros). `tem_multiplos` é calculado na hora, sempre a partir do estado atual da
    lista (se a seleção cair abaixo do limite, vira False sozinho). `revisado` é a flag
    persistida gravada por `sincronizar_incluir_batimento` ("Baixar mais"/"Salvar
    seleção"): se o usuário já revisou e confirmou a seleção pelo menos uma vez — mesmo
    mantendo o grupo acima do limite, por decisão própria — a fila não deve mais pausar
    este PAJ por este motivo (pedido do usuário, 2026-11-13); quem chama decide o que
    fazer com isso (pausar só quando `tem_multiplos and not revisado`)."""
    from services import config_service as _cfg
    bat = ler_batimento(pasta)
    itens = bat.get("itens") or []
    revisado = bool(bat.get("revisado"))
    if not itens:
        return False, "", revisado
    limiar = _cfg.get_sat_central_defaults().get("limiar_multiplos", 5)
    grupos = _grupos_acima_do_limite(itens, limiar)
    if not grupos:
        return False, "", revisado
    from ingestao import sat_client
    rotulos = {t["key"]: t["label"] for t in sat_client.TIPOS_DOCUMENTOS}
    resumo = "; ".join(f"{rotulos.get(k, k)}: {n} pendente(s)" for k, n in
                       sorted(grupos.items(), key=lambda kv: -kv[1]))
    return True, resumo, revisado


def _so_digitos(cpf: str) -> str:
    return re.sub(r"\D", "", cpf or "")


def _fmt_cpf(dig: str) -> str:
    return f"{dig[:3]}.{dig[3:6]}.{dig[6:9]}-{dig[9:]}" if len(dig) == 11 else dig


def _arq_cpfs_extras(paj_norm: str) -> Path:
    return raiz_sat(paj_norm) / "_cpfs_extras.json"


def _arq_filtros(paj_norm: str) -> Path:
    return raiz_sat(paj_norm) / "_filtros.json"


def _itens_filtro(txt: str) -> list[str]:
    """'726.015.826-4, 1915479324' → ['726.015.826-4', '1915479324'] (vírgula/;/espaço)."""
    return [t for t in re.split(r"[\s,;]+", (txt or "").strip()) if re.sub(r"\D", "", t)]


def ler_filtros(paj_norm: str) -> dict:
    """Filtros do download do PAJ na fila automática: {nbs, protocolos} (texto do usuário)."""
    try:
        d = json.loads(_arq_filtros(paj_norm).read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        d = {}
    return {"nbs": str(d.get("nbs", "") or ""), "protocolos": str(d.get("protocolos", "") or "")}


def salvar_filtros(paj_norm: str, nbs: str, protocolos: str) -> dict:
    """Grava os filtros de NB / protocolo (PAT) do PAJ — normalizados como lista separada
    por vírgula. Vazio = sem filtro (baixa tudo o que a pretensão manda)."""
    d = {"nbs": ", ".join(_itens_filtro(nbs)), "protocolos": ", ".join(_itens_filtro(protocolos))}
    arq = _arq_filtros(paj_norm)
    arq.parent.mkdir(parents=True, exist_ok=True)
    arq.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, **d}


def _classe_de_papel(papel: str, n_assistido: int = 2) -> str:
    if papel == "Instituidor":
        return "Instituidor"
    if papel == "Terceiro":
        return "Terceiro"
    return f"Assistido{n_assistido}"


def listar_cpfs_extras(paj_norm: str) -> list[dict]:
    """CPFs extras do PAJ: [{cpf, nome, papel}] (papel em PAPEIS_EXTRAS)."""
    try:
        lst = json.loads(_arq_cpfs_extras(paj_norm).read_text(encoding="utf-8")) or []
    except Exception:  # noqa: BLE001
        return []
    return [x for x in lst if isinstance(x, dict) and len(_so_digitos(x.get("cpf", ""))) == 11]


def salvar_cpfs_extras(paj_norm: str, itens: list[dict]) -> dict:
    """Valida e grava a lista de CPFs extras do PAJ. Retorna {ok, itens} ou {ok: False, erro}."""
    antes = listar_cpfs_extras(paj_norm)
    pasta_antes = {_so_digitos(x.get("cpf", "")): x.get("pasta", "") for x in antes}
    # numeração dos "AssistidoN": continua a partir do maior já usado (pastas e cadastro)
    usados = {1}
    for nm in [x.get("pasta", "") for x in antes] + [d.name for d in pastas_pessoas(paj_norm)]:
        m = re.match(r"Assistido(\d+)", nm or "")
        if m:
            usados.add(int(m.group(1)))
    prox = max(usados) + 1
    out: list[dict] = []
    vistos: set[str] = set()
    for x in itens or []:
        dig = _so_digitos(str((x or {}).get("cpf", "")))
        if len(dig) != 11:
            return {"ok": False, "erro": f"CPF inválido: {(x or {}).get('cpf', '')!r}."}
        if dig in vistos:
            continue
        vistos.add(dig)
        papel = (x or {}).get("papel") or PAPEIS_EXTRAS[0]
        if papel not in PAPEIS_EXTRAS:
            papel = PAPEIS_EXTRAS[0]
        nome = str((x or {}).get("nome", "")).strip().upper()
        pasta = pasta_antes.get(dig, "")
        if not pasta:
            pasta = nome_pasta_pessoa(_classe_de_papel(papel, prox), dig, nome)
            if papel not in ("Instituidor", "Terceiro"):
                prox += 1
        out.append({"cpf": _fmt_cpf(dig), "nome": nome, "papel": papel, "pasta": pasta})
    arq = _arq_cpfs_extras(paj_norm)
    arq.parent.mkdir(parents=True, exist_ok=True)
    arq.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    # CPFs excluídos da lista: os arquivos já baixados deles vão para a Lixeira
    removidos = 0
    for pessoa in antes:
        if _so_digitos(pessoa.get("cpf", "")) not in vistos:
            removidos += remover_arquivos_cpf_extra(paj_norm, pessoa)
    return {"ok": True, "itens": out, "arquivos_removidos": removidos}


def pasta_cpf_extra(paj_norm: str, pessoa: dict) -> Path:
    """Pasta de um CPF adicional: 'Arquivos SAT/<Classe> <CPF> - <NOME>' (gravada no
    cadastro, para não mudar de nome depois)."""
    nome = pessoa.get("pasta") or nome_pasta_pessoa(_classe_de_papel(pessoa.get("papel", "")),
                                                    pessoa.get("cpf", ""), pessoa.get("nome", ""))
    return raiz_sat(paj_norm) / nome


def remover_arquivos_cpf_extra(paj_norm: str, pessoa: dict) -> int:
    """Manda para a Lixeira a pasta inteira de um CPF adicional (PDFs + log) — nada dela
    entra mais na juntada. Sai do lugar NA HORA (exclusao_suave.apagar_pasta renomeia).
    Retorna quantos PDFs havia."""
    from services import exclusao_suave
    pasta = pasta_cpf_extra(paj_norm, pessoa)
    if not pasta.is_dir():
        return 0
    n = len(list(pasta.glob("*.pdf")))
    exclusao_suave.apagar_pasta(pasta, "sat:cpf-extra-removido")
    return n


def listar_arquivos_outros_cpfs(paj_norm: str) -> list[dict]:
    """PDFs das pastas de pessoa que NÃO são a do assistido nem a do 1º instituidor
    (Assistido2…, outros instituidores, Terceiro) — entram na juntada."""
    fora = {pasta_arquivos_sat(paj_norm).resolve(), pasta_instituidor(paj_norm).resolve()}
    out = []
    for d in pastas_pessoas(paj_norm):
        if d.resolve() in fora:
            continue
        for f in sorted(d.glob("*.pdf")):
            try:
                tam = f.stat().st_size
            except Exception:  # noqa: BLE001
                tam = 0
            out.append({"nome": f.name, "caminho": str(f), "tipo": "pdf", "tamanho": tam})
    return out


def _extras_tem_falha(paj_norm: str) -> bool:
    """Algum CPF extra cujo último download (já tentado) teve falha?"""
    for pessoa in listar_cpfs_extras(paj_norm):
        pasta = pasta_cpf_extra(paj_norm, pessoa)
        if (pasta / "_log_download.json").exists() and _download_tem_falha(paj_norm, pasta):
            return True
    return False




def estado_download(paj_norm: str) -> dict:
    """{levantamento_em: str} — selo "levantado" da tabela de pendências."""
    pasta = pasta_arquivos_sat(paj_norm)
    out = {"levantamento_em": ""}
    with __import__("contextlib").suppress(Exception):
        lev = json.loads((pasta / "_levantamento.json").read_text(encoding="utf-8"))
        if lev.get("completo"):
            out["levantamento_em"] = lev.get("ts", "")
    return out


def inventario_paj(paj_norm: str, escopo: str = "assistido") -> dict:
    """Inventário dos documentos revelados no SAT + marcas ja_baixado/ja_anexado/incluir.
    `escopo` = 'assistido' ou 'instituidor'. Se o PAJ já tem lista de batimento (modelo
    novo, 2026-11-12), ela é a fonte — já traz `incluir` por item. Senão (PAJ ainda não
    passou pela 1ª rodada deste modelo), cai no comportamento antigo: `_log_download.json`
    + o que o levantamento revelou e o download ainda não registrou."""
    paths = _escopo_paths(paj_norm, escopo)
    pasta_base = paths["destino"]
    bat = ler_batimento(pasta_base)
    cpf, inv = "", []
    log_p = pasta_base / "_log_download.json"
    if os.path.isfile(str(log_p)):
        with __import__("contextlib").suppress(Exception):
            cpf = json.loads(log_p.read_text(encoding="utf-8")).get("cpf", "") or ""
    if bat.get("itens"):
        inv = [dict(it) for it in bat["itens"]]
    else:
        if os.path.isfile(str(log_p)):
            try:
                inv = list(json.loads(log_p.read_text(encoding="utf-8")).get("inventario") or [])
            except Exception:  # noqa: BLE001
                inv = []
    # Levantamento (lista do SAT sem baixar — sat_client.levantar_cidadao_stream): o que
    # ele revelou e o download ainda não registrou entra no inventário, para o "Baixar
    # mais" oferecer TUDO o que existe para o CPF.
    lev = {}
    with __import__("contextlib").suppress(Exception):
        lev = json.loads((pasta_base / "_levantamento.json").read_text(encoding="utf-8"))
    ja_no_inv = {(it.get("key", ""), it.get("sub", "")) for it in inv}
    for it in lev.get("itens") or []:
        chave = (it.get("key", ""), it.get("sub", ""))
        if chave not in ja_no_inv:
            inv.append(dict(it))
            ja_no_inv.add(chave)
    if not cpf:
        cpf = lev.get("cpf", "") or ""
    if not cpf and escopo == "assistido":
        cpf = _cpf_do_paj(paj_norm)
    if not cpf and escopo == "assistido":
        mp = next((m for m in _ler_pajs_manuais() if m.get("paj_norm") == paj_norm), None)
        cpf = (mp or {}).get("cpf", "")
    # já anexados: nome -> conjunto de movs em que foi anexado, fazendo MERGE de todas as
    # anexações registradas neste PAJ (movimentação principal + cada "anexar mais").
    anexado_movs: dict[str, set] = {}
    for r in historico_sat.listar():
        if r.get("paj_norm") != paj_norm:
            continue
        for a in (r.get("arquivos") or []):
            anexado_movs.setdefault(a, set()).add(r.get("mov_seq"))
        for m in (r.get("movs_extra") or []):
            for a in (m.get("arquivos") or []):
                anexado_movs.setdefault(a, set()).add(m.get("seq"))
    anexados: set[str] = set(anexado_movs.keys())
    # já baixados: arquivos presentes nas pastas (principal + instituidor + complementar)
    baixados: set[str] = set()
    for lst in (listar_arquivos_sat(paj_norm), listar_arquivos_instituidor(paj_norm),
                listar_arquivos_complementar(paj_norm)):
        for a in lst:
            baixados.add(a["nome"])
    # descartados manualmente (lixeira "Excluídos" DESTE escopo): o item tem status
    # "baixado" na lista de batimento (foi baixado de fato) mas o Defensor removeu o
    # arquivo pela tabela antes de anexar — diferente de "nunca foi baixado". Pedido do
    # usuário, 2026-11-13: mostrar essa distinção (baixado-e-anexado vs. baixado-e-
    # descartado) em vez de deixar a linha sem nenhuma indicação de status.
    excluidos: set[str] = set()
    pasta_excluidos = pasta_base / "Excluídos"
    if pasta_excluidos.exists():
        for f in pasta_excluidos.glob("*.pdf"):
            excluidos.add(f.name)
    # dedup por (key, sub) — a paginação do PAT pode revelar o mesmo protocolo 2x; itens
    # duplicados quebravam o x-for do "Baixar mais" (chave repetida). Preferência de status:
    # baixado > qualquer outro (mantém o registro mais informativo).
    _PRIO = {"baixado": 3}
    dedup: dict[tuple, dict] = {}
    for it in inv:
        chave = (it.get("key", ""), it.get("sub", ""))
        atual = dedup.get(chave)
        if atual is None:
            dedup[chave] = dict(it)
            continue
        # preserva o rótulo/serviço e o NB se o vencedor atual não tiver
        if not atual.get("rotulo") and it.get("rotulo"):
            atual["rotulo"] = it["rotulo"]
        if not atual.get("nb") and it.get("nb"):
            atual["nb"] = it["nb"]
        # troca o vencedor SÓ quando o novo tem status de prioridade MAIOR (baixado manda)
        if _PRIO.get(it.get("status", ""), 0) > _PRIO.get(atual.get("status", ""), 0):
            novo = dict(it)
            if not novo.get("rotulo") and atual.get("rotulo"):
                novo["rotulo"] = atual["rotulo"]
            if not novo.get("nb") and atual.get("nb"):
                novo["nb"] = atual["nb"]
            dedup[chave] = novo
    itens = []
    for it in dedup.values():
        nome = it.get("nome", "")
        key, sub = it.get("key", ""), it.get("sub", "")
        servico = it.get("rotulo", "")
        itens.append({
            "key": key, "sub": sub, "nome": nome, "servico": servico,
            "status": it.get("status", ""), "detalhe": it.get("detalhe", ""),
            "nb": it.get("nb", ""),
            "incluir": it.get("incluir", True),
            # O documento continua "baixado" mesmo depois de excluído manualmente (movido
            # para "Excluídos") — ele só deixa de ser anexado, nunca perde a condição de já
            # ter sido baixado (não deve reabilitar um novo download). Por isso aceita o
            # nome tanto na pasta ativa quanto na lixeira "Excluídos" (`excluidos`, abaixo)
            # — pedido do usuário, 2026-11-13.
            "ja_baixado": bool(it.get("status") == "baixado"
                               and (not nome or nome in baixados or nome in excluidos)),
            "ja_anexado": bool(nome and nome in anexados),
            "descartado_manual": bool(it.get("status") == "baixado" and nome and nome in excluidos),
            "anexado_movs": sorted(s for s in anexado_movs.get(nome, set()) if s is not None),
        })
    return {"paj_norm": paj_norm, "cpf": cpf, "itens": itens,
            "levantamento": ({"ts": lev.get("ts", ""), "completo": bool(lev.get("completo"))} if lev else None)}


def pasta_complementar(paj_norm: str) -> Path:
    """Subpasta do download COMPLEMENTAR (baixado depois, anexado por movimentação)."""
    return pasta_arquivos_sat(paj_norm) / SUBPASTA_COMPLEMENTAR


def listar_arquivos_complementar(paj_norm: str) -> list[dict]:
    """PDFs na subpasta 'Complementar'."""
    pasta = pasta_complementar(paj_norm)
    if not pasta.exists():
        return []
    out = []
    for f in sorted(pasta.glob("*.pdf")):
        try:
            tam = f.stat().st_size
        except Exception:  # noqa: BLE001
            tam = 0
        out.append({"nome": f.name, "caminho": str(f), "tipo": "pdf", "tamanho": tam})
    return out


def tem_instituidor(paj_norm: str) -> bool:
    """Há instituidor neste PAJ? (subpasta 'Instituidor' com log de download ou PDFs)."""
    pi = pasta_instituidor(paj_norm)
    return bool((pi / "_log_download.json").exists() or listar_arquivos_instituidor(paj_norm))


def _escopo_paths(paj_norm: str, escopo: str) -> dict:
    """Resolve pastas/rótulo/listagem conforme o escopo ('assistido' | 'instituidor').
    Assim o download/mescla complementar serve tanto o acervo do assistido (pasta principal)
    quanto o do instituidor (subpasta 'Instituidor')."""
    if escopo == "instituidor":
        return {
            "destino": pasta_instituidor(paj_norm),
            "complementar": pasta_instituidor(paj_norm) / SUBPASTA_COMPLEMENTAR,
            "listar": listar_arquivos_instituidor,
            "rotulo": "INSTITUIDOR - ",
        }
    return {
        "destino": pasta_arquivos_sat(paj_norm),
        "complementar": pasta_complementar(paj_norm),
        "listar": listar_arquivos_sat,
        "rotulo": "",
    }


def _cpf_de_log(pasta: Path) -> str:
    """Lê o CPF gravado no _log_download.json de uma pasta de download (assistido/instituidor)."""
    p = pasta / "_log_download.json"
    if not os.path.isfile(str(p)):
        return ""
    try:
        return (json.loads(p.read_text(encoding="utf-8")).get("cpf") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _listar_pdfs_pasta(pasta: Path, escopo: str, referencia: str, excluido: bool) -> list[dict]:
    out: list[dict] = []
    if not pasta.exists():
        return out
    for f in sorted(pasta.glob("*.pdf")):
        try:
            tam = f.stat().st_size
        except Exception:  # noqa: BLE001
            tam = 0
        out.append({"nome": f.name, "caminho": str(f), "tipo": "pdf", "tamanho": tam,
                    "excluido": excluido, "escopo": escopo, "referencia": referencia})
    return out


def _escopo_de_pasta(paj_norm: str, d: Path) -> str:
    """'assistido' / 'instituidor' (pastas com Baixar mais/complementar) ou o nome da pasta."""
    if d.resolve() == pasta_arquivos_sat(paj_norm).resolve():
        return "assistido"
    if d.resolve() == pasta_instituidor(paj_norm).resolve():
        return "instituidor"
    return d.name


def _pasta_do_escopo(paj_norm: str, escopo: str) -> Path | None:
    """Inverso de _escopo_de_pasta — recusa nomes que não sejam pasta de pessoa."""
    if escopo == "assistido":
        return pasta_arquivos_sat(paj_norm)
    if escopo == "instituidor":
        return pasta_instituidor(paj_norm)
    nome = Path(escopo or "").name
    if nome != escopo or not _RE_PASTA_PESSOA.match(nome):
        return None
    return raiz_sat(paj_norm) / nome


def listar_todos_arquivos_sat(paj_norm: str) -> list[dict]:
    """Tabela UNIFICADA para a UI: documentos de TODAS as pastas de pessoa (assistido,
    outros assistidos, instituidor, terceiro), ativos e excluídos, cada um com `escopo`,
    `referencia` ('Assistido - CPF X', 'Instituidor - CPF Y'…) e flag `excluido`."""
    ativos: list[dict] = []
    excluidos: list[dict] = []
    for d in pastas_pessoas(paj_norm):
        cpf = _cpf_de_log(d)
        if not cpf:
            m = re.search(r"\d{3}\.\d{3}\.\d{3}-\d{2}", d.name)
            cpf = m.group(0) if m else ""
        rot = rotulo_pessoa(d)
        ref = f"{rot} - CPF {cpf}" if cpf else rot
        esc = _escopo_de_pasta(paj_norm, d)
        ativos += _listar_pdfs_pasta(d, esc, ref, False)
        excluidos += _listar_pdfs_pasta(d / "Excluídos", esc, ref, True)
    return ativos + excluidos


def _mover_pdf(origem: Path, destino_dir: Path) -> None:
    destino_dir.mkdir(parents=True, exist_ok=True)
    destino = destino_dir / origem.name
    if destino.exists():
        destino.unlink()
    shutil.move(str(origem), str(destino))


def _contagens(paj_norm: str) -> dict:
    return {"n_arquivos": len(listar_arquivos_sat(paj_norm)),
            "n_instituidor": len(listar_arquivos_instituidor(paj_norm))}


def excluir_arquivo_sat(paj_norm: str, nome: str, escopo: str = "assistido") -> dict:
    """SOFT DELETE: move o documento (do assistido OU do instituidor, conforme `escopo`)
    para a respectiva lixeira 'Excluídos'. Não some do disco; pode ser recuperado."""
    pasta = _pasta_do_escopo(paj_norm, escopo)
    if pasta is None:
        return {"ok": False, "detalhe": "Pasta inválida.", **_contagens(paj_norm)}
    origem = pasta.resolve()
    destino = pasta / "Excluídos"
    alvo = (origem / Path(nome or "").name).resolve()
    if origem not in alvo.parents or not alvo.exists():
        return {"ok": False, "detalhe": "Arquivo não encontrado.", **_contagens(paj_norm)}
    try:
        _mover_pdf(alvo, destino)
        return {"ok": True, "detalhe": f"Removido da lista: {alvo.name}", **_contagens(paj_norm)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detalhe": str(e), **_contagens(paj_norm)}


def recuperar_arquivo_sat(paj_norm: str, nome: str, escopo: str = "assistido") -> dict:
    """Traz o documento de volta da lixeira para a pasta de origem (assistido/instituidor)."""
    pasta = _pasta_do_escopo(paj_norm, escopo)
    if pasta is None:
        return {"ok": False, "detalhe": "Pasta inválida.", **_contagens(paj_norm)}
    exc = (pasta / "Excluídos").resolve()
    destino = pasta
    alvo = (exc / Path(nome or "").name).resolve()
    if exc not in alvo.parents or not alvo.exists():
        return {"ok": False, "detalhe": "Arquivo não encontrado em Excluídos.", **_contagens(paj_norm)}
    try:
        _mover_pdf(alvo, destino)
        return {"ok": True, "detalhe": f"Recuperado: {alvo.name}", **_contagens(paj_norm)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detalhe": str(e), **_contagens(paj_norm)}


# ---------------------------------------------------------------------------
# PAJs adicionados MANUALMENTE — única forma de incluir PAJs neste painel (não
# há varredura automática da caixa de entrada).
# ---------------------------------------------------------------------------
_ARQ_MANUAIS = Path(config.OFICIO_GERAL) / "_pajs_manuais.json"


def _parse_paj_input(txt: str):
    """Aceita 'AAAA/UUU-NNNNN', 'AAAA-UUU-NNNNN', 'PAJ-AAAA-UUU-NNNNN' ou 12 dígitos.
    Retorna (paj_norm, ano, unidade, numero_seq) ou None se não reconhecer."""
    s = re.sub(r"\s+", "", txt or "")
    m = re.search(r"(\d{4})\D{0,3}(\d{3})\D{0,3}(\d{3,})", s)
    if not m:
        return None
    ano, uni, num = m.group(1), m.group(2), m.group(3)
    return (f"PAJ-{ano}-{uni}-{num}", ano, uni, num)


def _ler_pajs_manuais() -> list[dict]:
    if not _ARQ_MANUAIS.exists():
        return []
    try:
        return json.loads(_ARQ_MANUAIS.read_text(encoding="utf-8")) or []
    except Exception:  # noqa: BLE001
        return []


def _salvar_pajs_manuais(lst: list[dict]) -> None:
    _ARQ_MANUAIS.parent.mkdir(parents=True, exist_ok=True)
    _ARQ_MANUAIS.write_text(json.dumps(lst, ensure_ascii=False, indent=2), encoding="utf-8")


async def add_paj_manual(texto: str) -> dict:
    """Adiciona um PAJ à lista extraindo os dados AO VIVO do SISDPU (busca global,
    mesma sessão headless). O PAJ só é aceito se localizado e com CPF extraído.
    Retorna {ok, item} ou {ok:False, erro}."""
    parsed = _parse_paj_input(texto)
    if not parsed:
        return {"ok": False, "erro": f"Número de PAJ inválido: '{texto}'. Use AAAA/UUU-NNNNN."}
    paj_norm, ano, uni, num = parsed
    numero = f"{ano}/{uni}-{num}"
    from ingestao import sisdpu_client as _sc
    try:
        # forcar_sessao=False: reaproveita a sessão viva (login automático só se expirou),
        # em vez de fechar e relogar do zero a cada inclusão (que era lento e frágil).
        dados = await _sc.buscar_paj_global(num, ano, uni, forcar_sessao=False)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": f"Falha na autenticação/consulta ao SISDPU: {e}. "
                                     f"Verifique as credenciais do SISDPU no cofre (Configurações)."}
    if not isinstance(dados, dict) or dados.get("erro"):
        return {"ok": False, "erro": (dados or {}).get("erro") or "PAJ não localizado no SISDPU."}
    cpf = (dados.get("cpf_assistido") or "").strip()
    if not cpf:
        return {"ok": False, "erro": "PAJ localizado, mas não consegui extrair o CPF do assistido."}
    url = dados.get("url", "") or ""
    mid = re.search(r"[?&]id=(\d+)", url)
    id_processo = mid.group(1) if mid else ""
    pret = (dados.get("pretensao") or "").strip()
    pret = (_extrair_pretensao_pura(pret) or pret).strip()
    item = {
        "paj_norm": paj_norm, "numero": numero, "id_processo": id_processo,
        "cpf": cpf, "assistido": (dados.get("assistido") or "").strip(),
        "pretensao": pret, "narrativa": _html_para_texto(dados.get("narrativa") or ""),
        "url_detalhe": url,
    }
    lst = [x for x in _ler_pajs_manuais() if x.get("paj_norm") != paj_norm]
    lst.append(item)
    _salvar_pajs_manuais(lst)
    return {"ok": True, "item": item}


def del_paj_manual(paj_norm: str, apagar_arquivos: bool = True, registrar_historico: bool = True) -> dict:
    """Remove o PAJ da lista. Por padrão (uso pelo botão ✕) TAMBÉM apaga a pasta
    com os arquivos já baixados (PAJS_DIR/<paj_norm>/) e registra a exclusão em
    "PAJs Trabalhados" — ação irreversível, o front-end deve confirmar com o
    usuário antes de chamar com os padrões.

    Uso interno (limpeza automática da lista após "Anexar ao PAJ" ter sucesso,
    em processar_paj_stream) passa apagar_arquivos=False, registrar_historico=False:
    os arquivos já foram anexados ao PAJ (não faz sentido apagá-los aqui) e o
    próprio processamento já registrou seu resultado no histórico — registrar
    de novo como "exclusão manual" seria enganoso.

    Um PAJ cadastrado manualmente pode TAMBÉM estar na caixa de grupo da fila
    automática — nesse caso a pasta "Arquivos SAT" é COMPARTILHADA pelas duas abas.
    Remover da lista manual aqui NUNCA apaga os arquivos quando o PAJ também está na
    última sincronização da fila (`_ULTIMA_SINCRONIZACAO`) — só tira a entrada manual
    redundante, mantendo o que a fila automática já baixou (bug corrigido em
    2026-11-10: remover da aba Individual apagava, de brinde, o download em
    andamento/concluído da fila automática para o MESMO PAJ)."""
    lst = _ler_pajs_manuais()
    mp = next((x for x in lst if x.get("paj_norm") == paj_norm), None)
    _salvar_pajs_manuais([x for x in lst if x.get("paj_norm") != paj_norm])
    na_fila = any(i.get("paj_norm") == paj_norm and not i.get("manual")
                  for i in (_ULTIMA_SINCRONIZACAO.get("itens") or []))
    if na_fila:
        apagar_arquivos = False
    pasta = _pasta_paj(paj_norm)
    tinha_arquivos = pasta.exists()
    if apagar_arquivos and tinha_arquivos:
        from services import exclusao_suave  # exclusão suave (Lixeira, fora do processo do painel)
        exclusao_suave.apagar_pasta(pasta, "sat:paj-manual")
    if registrar_historico and mp:
        if na_fila:
            detalhe = ("PAJ removido da lista manual (também está na caixa de grupo da "
                       "fila automática — os arquivos já baixados foram MANTIDOS).")
        else:
            detalhe = ("PAJ removido manualmente da lista pelo usuário; os arquivos já "
                        "baixados do SAT foram excluídos." if tinha_arquivos else
                        "PAJ removido manualmente da lista pelo usuário (não havia arquivos baixados).")
        historico_sat.registrar({
            "paj": mp.get("numero") or _norm_to_num(paj_norm),
            "paj_norm": paj_norm,
            "id_processo": mp.get("id_processo", ""),
            "assistido": mp.get("assistido", ""),
            "cpf": mp.get("cpf", ""),
            "url_detalhe": mp.get("url_detalhe", ""),
            "escopo": "exclusao_manual",
            "mov_status": "nao_aplicavel",
            "tram_status": "nao_aplicavel",
            "concluido_status": "nao_aplicavel",
            "status": "excluido",
            "detalhe": detalhe,
            "log": [{"level": "muted", "msg": detalhe}],
        })
    return {"ok": True}


def apagar_arquivos_sat_paj(paj_norm: str) -> dict:
    """Remove a pasta 'Arquivos SAT' (assistido + Instituidor + Complementar + Excluídos —
    tudo aninhado ali) de um PAJ JÁ CONCLUÍDO, para liberar espaço em disco, E remove o(s)
    registro(s) desse PAJ do histórico — a linha some completamente de "PAJs Concluídos".
    NÃO desfaz nada no SISDPU: a anexação já tinha sido feita; é só limpeza local (disco +
    lista), ação irreversível confirmada pelo usuário antes de chamar esta função."""
    pasta = raiz_sat(paj_norm)
    tinha_arquivos = pasta.exists()
    if tinha_arquivos:
        from services import exclusao_suave  # exclusão suave (Lixeira, fora do processo do painel)
        exclusao_suave.apagar_pasta(pasta, "sat:paj-concluido")
    n_removidos = historico_sat.remover_por_paj(paj_norm)
    return {"ok": True, "tinha_arquivos": tinha_arquivos, "registros_removidos": n_removidos}


def _item_pendencia_manual(mp: dict) -> dict:
    """Monta o item da tabela a partir de um PAJ manual do store."""
    pn = mp.get("paj_norm", "")
    arquivos = listar_arquivos_sat(pn)
    return {
        "paj": mp.get("numero") or _norm_to_num(pn),
        "paj_norm": pn,
        "id_processo": mp.get("id_processo", ""),
        "id_tramite": "",
        "assistido": mp.get("assistido", ""),
        "cpf": mp.get("cpf", ""),
        "pretensao": mp.get("pretensao", ""),
        "descricao": "",
        "url_detalhe": mp.get("url_detalhe", ""),
        "n_arquivos": len(arquivos),
        "n_instituidor": len(listar_arquivos_instituidor(pn)),
        "tem_pasta": _pasta_paj(pn).exists(),
        "sincronizado": bool(_ler_metadata(pn)),
        "tem_log": (pasta_arquivos_sat(pn) / "_log_download.json").exists(),
        "tem_falha": _download_tem_falha(pn),
        "cpfs_extras": listar_cpfs_extras(pn),
        **{"filtro_" + k: v for k, v in ler_filtros(pn).items()},
        "extras_falha": _extras_tem_falha(pn),
        "ja_processado": False,
        "manual": True,
        "narrativa": mp.get("narrativa", ""),
        "levantamento_em": estado_download(pn).get("levantamento_em", ""),
    }


def listar_pendentes_manuais() -> dict:
    """Só os PAJs cadastrados manualmente (sem ler a caixa). Rápido e offline
    (não toca o SISDPU) — pré-carga ao abrir a página."""
    itens = [_item_pendencia_manual(mp) for mp in _ler_pajs_manuais() if mp.get("paj_norm")]
    return {"ok": True, "itens": itens}


# ---------------------------------------------------------------------------
# Caixa de grupo (fila automática): TODO PAJ que estiver na caixa de grupo
# configurada (Configurações › Parâmetros) entra na fila — sem exigir nenhuma
# descrição específica de trâmite.
# ---------------------------------------------------------------------------
_cache_detalhes_caixa: dict[str, tuple[float, dict]] = {}
_CACHE_DETALHES_SEG = 600  # 10 min — evita reconsultar o SISDPU a cada poll da tela


async def _detalhes_caixa(item: dict) -> dict:
    """{cpf, pretensao, assistido, narrativa} de um item da caixa. Reaproveita o PAJ
    manual já cadastrado (mais barato) ou, senão, lê o detalhamento pela MESMA sessão
    headless (ingestao.sisdpu_movtram.detalhes_paj) — com cache curto em memória."""
    paj_norm = _num_to_norm(item.get("paj", ""))
    agora = time.monotonic()
    cache = _cache_detalhes_caixa.get(paj_norm)
    if cache and agora - cache[0] < _CACHE_DETALHES_SEG:
        return cache[1]
    mp = next((m for m in _ler_pajs_manuais() if m.get("paj_norm") == paj_norm), None)
    if mp and mp.get("cpf"):
        det = {"cpf": mp["cpf"], "pretensao": mp.get("pretensao", ""),
               "assistido": mp.get("assistido", ""), "narrativa": mp.get("narrativa", ""), "erro": ""}
    else:
        erro = ""
        try:
            bruto = await movtram.detalhes_paj(item.get("url_detalhe", "")) or {}
        except Exception as e:  # noqa: BLE001 — falha pontual de rede/SISDPU ao ler ESTE PAJ
            bruto = {}
            erro = str(e) or e.__class__.__name__
            _log.warning("falha ao ler detalhes do PAJ %s na caixa de grupo: %s", paj_norm, erro)
        pret = (bruto.get("pretensao") or "").strip()
        det = {"cpf": (bruto.get("cpf") or "").strip(),
               "pretensao": (_extrair_pretensao_pura(pret) or pret).strip(),
               "assistido": (bruto.get("assistido") or "").strip(),
               "narrativa": _html_para_texto(bruto.get("narrativa") or ""),
               "erro": erro}
    # NÃO cacheia uma falha (só o sucesso) — senão uma retentativa, segundos depois,
    # devolveria o mesmo erro congelado em memória pelos próximos 10 minutos.
    if not det.get("erro"):
        _cache_detalhes_caixa[paj_norm] = (agora, det)
    return det


def _campos_locais(paj_norm: str, id_tramite: str = "") -> dict:
    """Campos de um item da fila que vêm SÓ do disco local/histórico — nunca do SISDPU
    (`id_tramite` é só a CHAVE de busca no histórico local; o valor em si já veio
    cacheado da última sincronização — nenhuma consulta nova ao SISDPU acontece aqui).
    Recalculados a cada leitura (ao vivo ou da última sincronização em cache)."""
    arquivos = listar_arquivos_sat(paj_norm)
    ja = historico_sat.por_id_tramite(id_tramite) if id_tramite else None
    pasta_principal = pasta_arquivos_sat(paj_norm)
    multiplos, multiplos_detalhe, multiplos_revisado = _multiplos_requerimentos(pasta_principal)
    return {
        "n_arquivos": len(arquivos),
        "n_instituidor": len(listar_arquivos_instituidor(paj_norm)),
        "tem_pasta": _pasta_paj(paj_norm).exists(),
        "tem_log": (pasta_principal / "_log_download.json").exists(),
        "tem_batimento": bool(ler_batimento(pasta_principal).get("itens")),
        "tem_falha": _download_tem_falha(paj_norm),
        "cpfs_extras": listar_cpfs_extras(paj_norm),
        **{"filtro_" + k: v for k, v in ler_filtros(paj_norm).items()},
        "extras_falha": _extras_tem_falha(paj_norm),
        "levantamento_em": estado_download(paj_norm).get("levantamento_em", ""),
        "ja_processado": bool(ja and ja.get("status") == "concluido"),
        "multiplos_requerimentos": multiplos,
        "multiplos_detalhe": multiplos_detalhe,
        "multiplos_revisado": multiplos_revisado,
    }


# Última leitura BEM-SUCEDIDA da caixa de grupo (via "Sincronizar caixa") — os campos
# que só o SISDPU sabe (cpf/pretensão/narrativa/id_tramite/…) ficam em cache aqui. A
# fila automática usa esse cache (ver `pendentes_sincronizados`) em vez de reler o
# SISDPU a cada vez — só os campos locais (`_campos_locais`, acima) são recalculados.
# PERSISTIDO EM DISCO (não só em memória): enquanto houver PAJ pendente/incompleto, o
# usuário NÃO deve ser obrigado a sincronizar de novo só porque o painel reiniciou (ex.:
# depois de uma atualização do código) — pedido explícito do usuário em 2026-11-10.
_ARQ_SINCRONIZACAO = config.OFICIO_GERAL / "_ultima_sincronizacao_caixa.json"


def _carregar_sincronizacao_disco() -> dict:
    try:
        d = json.loads(_ARQ_SINCRONIZACAO.read_text(encoding="utf-8"))
        return {"itens": d.get("itens") or [], "em": d.get("em", "")}
    except Exception:  # noqa: BLE001 — arquivo ausente/corrompido: parte vazio, sem quebrar
        return {"itens": [], "em": ""}


def _salvar_sincronizacao_disco() -> None:
    with __import__("contextlib").suppress(Exception):
        _ARQ_SINCRONIZACAO.parent.mkdir(parents=True, exist_ok=True)
        _ARQ_SINCRONIZACAO.write_text(
            json.dumps(_ULTIMA_SINCRONIZACAO, ensure_ascii=False, indent=2), encoding="utf-8")


_ULTIMA_SINCRONIZACAO: dict = _carregar_sincronizacao_disco()


def _remover_de_sincronizacao(paj_norm: str) -> None:
    """Tira um PAJ da lista cacheada da Fila Automática (some da tabela) sem mexer em
    arquivos — usado quando um PAJ "saiu da caixa" é anexado com sucesso (não tem
    `id_tramite` para `ja_processado` detectar sozinho) ou excluído pelo usuário."""
    itens = _ULTIMA_SINCRONIZACAO.get("itens") or []
    novos = [i for i in itens if i.get("paj_norm") != paj_norm]
    if len(novos) != len(itens):
        _ULTIMA_SINCRONIZACAO["itens"] = novos
        _salvar_sincronizacao_disco()


def excluir_saiu_da_caixa(paj_norm: str) -> dict:
    """Remove definitivamente da lista de pendentes da Fila Automática um PAJ marcado
    "saiu da caixa" e apaga os arquivos já baixados dele — ação irreversível pedida pelo
    usuário (2026-11-12); o front-end deve confirmar antes de chamar. Só age sobre um
    item com `saiu_da_caixa` True — nunca mexe num PAJ que ainda está na caixa."""
    itens = _ULTIMA_SINCRONIZACAO.get("itens") or []
    alvo = next((i for i in itens if i.get("paj_norm") == paj_norm and i.get("saiu_da_caixa")), None)
    if not alvo:
        return {"ok": False, "erro": "PAJ não está marcado como \"saiu da caixa\" (ou não foi encontrado)."}
    _remover_de_sincronizacao(paj_norm)
    pasta = _pasta_paj(paj_norm)
    tinha_arquivos = pasta.exists()
    if tinha_arquivos:
        from services import exclusao_suave  # exclusão suave (Lixeira, fora do processo do painel)
        exclusao_suave.apagar_pasta(pasta, "sat:saiu-da-caixa")
    caixa_origem = alvo.get("caixa_origem", "")
    detalhe = (f"PAJ removido da lista da Fila Automática — havia saído da caixa de grupo "
               f"\"{caixa_origem}\" e o usuário optou por excluir em vez de anexar. "
               + ("Os arquivos já baixados foram apagados." if tinha_arquivos
                  else "Não havia arquivos baixados."))
    historico_sat.registrar({
        "paj": alvo.get("paj") or _norm_to_num(paj_norm),
        "paj_norm": paj_norm,
        "id_processo": alvo.get("id_processo", ""),
        "assistido": alvo.get("assistido", ""),
        "cpf": alvo.get("cpf", ""),
        "url_detalhe": alvo.get("url_detalhe", ""),
        "escopo": "exclusao_manual",
        "mov_status": "nao_aplicavel",
        "tram_status": "nao_aplicavel",
        "concluido_status": "nao_aplicavel",
        "status": "excluido",
        "detalhe": detalhe,
        "log": [{"level": "muted", "msg": detalhe}],
    })
    return {"ok": True}


def definir_selecao_fila(paj_norm: str, selecionado: bool) -> dict:
    """Marca/desmarca um PAJ para entrar (ou não) na PRÓXIMA rodada da fila automática —
    sempre reversível, só decide o que a próxima rodada processa (não mexe em nada já
    baixado). Persistido no cache da sincronização, sobrevive a reinícios do painel —
    pedido do usuário, 2026-11-13."""
    itens = _ULTIMA_SINCRONIZACAO.get("itens") or []
    achou = False
    for it in itens:
        if it.get("paj_norm") == paj_norm:
            it["selecionado_fila"] = bool(selecionado)
            achou = True
    if achou:
        _salvar_sincronizacao_disco()
    return {"ok": achou}


def definir_selecao_fila_todos(selecionado: bool) -> dict:
    """Marca/desmarca TODOS os PAJs da lista de uma vez (botão "marcar todos")."""
    itens = _ULTIMA_SINCRONIZACAO.get("itens") or []
    for it in itens:
        it["selecionado_fila"] = bool(selecionado)
    _salvar_sincronizacao_disco()
    return {"ok": True, "n": len(itens)}


def pendentes_sincronizados() -> dict:
    """PAJs da ÚLTIMA sincronização bem-sucedida da caixa de grupo ("Sincronizar caixa"),
    SEM tocar o SISDPU de novo — só os campos locais (download/falha/concluído) são
    recalculados na hora. Usada pela fila automática: uma vez sincronizada a caixa, o
    download (100% SAT Central) não depende mais do SISDPU — só volta a precisar dele na
    juntada ("Anexar ao PAJ") ou numa nova sincronização manual."""
    cache = _ULTIMA_SINCRONIZACAO.get("itens") or []
    if not cache:
        return {"ok": False, "itens": [],
                "detalhe": "Nenhuma sincronização da caixa ainda — clique em "
                           "\"Sincronizar caixa\" antes de iniciar a fila automática."}
    itens = [dict(it, **_campos_locais(it["paj_norm"], it.get("id_tramite", ""))) for it in cache]
    em = _ULTIMA_SINCRONIZACAO.get("em", "")
    return {"ok": True, "itens": itens,
            "detalhe": f"{len(itens)} PAJ(s) da última sincronização" + (f" ({em})." if em else ".")}


async def listar_pendentes() -> dict:
    """Lê a caixa de entrada de GRUPO configurada (ex.: "1 - PREV - SAT" — NÃO a caixa
    pessoal do usuário) e devolve TODOS os PAJs que estiverem nela — estar na caixa já
    habilita o PAJ para a fila automática, sem exigir nenhuma descrição específica de
    trâmite —, somados aos PAJs incluídos manualmente que ainda não vieram da caixa
    (dedup por paj_norm). Retorna {ok, itens, detalhe}. Numa leitura bem-sucedida,
    atualiza o cache usado por `pendentes_sincronizados` (fila automática)."""
    from services import config_service as _cfg
    caixa_nome = _cfg.get_fila_caixa_origem()
    if not caixa_nome:
        itens = [_item_pendencia_manual(mp) for mp in _ler_pajs_manuais() if mp.get("paj_norm")]
        return {"ok": False, "itens": itens,
                "detalhe": "Nenhuma caixa de grupo configurada para a fila automática "
                           "(Configurações › Parâmetros)."}
    try:
        # enriquecer=False: sem filtro por descrição, não precisamos abrir o popup
        # "Detalhes do trâmite" de cada item (custa ~1-2s cada) — bem mais rápido.
        caixa = await movtram.listar_caixa_grupo(caixa_nome, enriquecer=False)
    except Exception as e:  # noqa: BLE001 — ex.: cofre trancado, sessão caiu, SISDPU fora do ar,
        # ou um soluço passageiro do navegador (ex.: "Execution context was destroyed" —
        # a página do SISDPU estava navegando bem na hora da leitura): nunca mostra o
        # texto técnico cru para o usuário, só o detalhe junto (pedido do usuário,
        # 2026-11-12 — ele apareceu sem explicação na barra de status da fila).
        caixa = {"ok": False, "detalhe": (
            "Falha temporária ao ler a caixa no SISDPU (a página pode ter navegado no "
            f"meio da leitura) — clique em \"Sincronizar caixa\" de novo. Detalhe técnico: {e}")}
    caixa_ok = bool(caixa.get("ok"))
    if caixa_ok:
        with __import__("contextlib").suppress(Exception):
            _cfg.set_fila_caixas_disponiveis(caixa.get("grupos_disponiveis") or [])
    # Seleção (checkbox) de cada PAJ para a PRÓXIMA rodada da fila automática: precisa
    # sobreviver a cada "Sincronizar caixa" — sem isto, a escolha do usuário seria
    # apagada e recomeçaria marcada toda vez (pedido do usuário, 2026-11-13). Ausente =
    # True (comportamento de sempre, nenhum PAJ fica de fora sem o usuário desmarcar).
    _sel_antiga = {i.get("paj_norm"): i.get("selecionado_fila", True)
                  for i in (_ULTIMA_SINCRONIZACAO.get("itens") or [])}
    itens: list[dict] = []
    for it in (caixa.get("itens", []) if caixa_ok else []):
        paj = it.get("paj", "")
        paj_norm = _num_to_norm(paj)
        det = await _detalhes_caixa(it)
        itens.append({
            "paj": paj,
            "paj_norm": paj_norm,
            "id_processo": it.get("id", ""),
            "id_tramite": it.get("id_tramite", ""),
            "assistido": it.get("assistido", "") or det.get("assistido", ""),
            "cpf": det.get("cpf", ""),
            "pretensao": det.get("pretensao", ""),
            "narrativa": det.get("narrativa", ""),
            "descricao": it.get("descricao", ""),
            "url_detalhe": it.get("url_detalhe", ""),
            "erro_leitura": det.get("erro", ""),
            "manual": False,
            "caixa_origem": caixa_nome,
            "saiu_da_caixa": False,
            "selecionado_fila": _sel_antiga.get(paj_norm, True),
            **_campos_locais(paj_norm, it.get("id_tramite", "")),
        })
    # PAJs que estavam sincronizados antes (vieram da caixa, não manuais) e sumiram desta
    # leitura ao vivo, mas ainda não foram concluídos pelo painel: NÃO descarta — mantém na
    # lista marcado "saiu da caixa", preservando o id_processo/id_tramite já conhecidos (só
    # o id_processo ainda serve para anexar/tramitar; a conclusão do trâmite original deixa
    # de ser possível). Sem essa marcação o PAJ simplesmente sumiria da tabela na próxima
    # sincronização, sem chance de o usuário decidir o que fazer — pedido do usuário,
    # 2026-11-12.
    # Confirmação em 2 sincronizações seguidas antes de marcar "saiu da caixa": uma
    # ÚNICA leitura vazia/incompleta da caixa (soluço passageiro do SISDPU) não pode
    # fazer todo mundo que não apareceu nela ser marcado de uma vez só — bug relatado
    # pelo usuário, 2026-11-13 (todos os PAJs viraram "saiu da caixa" numa sincronização
    # só). Na 1ª vez que um PAJ não aparece, só fica marcado como suspeito
    # (`_sumiu_1x`); só na 2ª vez seguida (ainda ausente) vira "saiu da caixa" de fato.
    if caixa_ok:
        presentes_vivo = {i["paj_norm"] for i in itens}
        for old in (_ULTIMA_SINCRONIZACAO.get("itens") or []):
            pn = old.get("paj_norm")
            if not pn or pn in presentes_vivo or old.get("manual"):
                continue
            campos = _campos_locais(pn, old.get("id_tramite", ""))
            if campos.get("ja_processado"):
                continue  # já concluído pelo painel — não precisa mais aparecer
            item = dict(old)
            item.update(campos)
            if old.get("saiu_da_caixa") or old.get("_sumiu_1x"):
                # já confirmado antes, ou essa é a 2ª vez seguida que não aparece —
                # confirma (ou mantém confirmado) "saiu da caixa".
                item["saiu_da_caixa"] = True
                item.pop("_sumiu_1x", None)
                item.setdefault("caixa_origem", caixa_nome)
            else:
                # 1ª vez que não aparece — só marca como suspeito, sem afirmar ainda.
                item["_sumiu_1x"] = True
            itens.append(item)
            presentes_vivo.add(pn)
    # PAJs adicionados manualmente que ainda não aparecem na caixa de grupo (ex.: ainda
    # não propagou, ou não foi tramitado para lá) continuam disponíveis à parte.
    presentes = {i["paj_norm"] for i in itens}
    for mp in _ler_pajs_manuais():
        pn = mp.get("paj_norm")
        if pn and pn not in presentes:
            item_manual = _item_pendencia_manual(mp)
            item_manual["selecionado_fila"] = _sel_antiga.get(pn, True)
            itens.append(item_manual)
    if caixa_ok:
        _ULTIMA_SINCRONIZACAO["itens"] = itens
        _ULTIMA_SINCRONIZACAO["em"] = datetime.now().strftime("%d/%m/%Y %H:%M")
        _salvar_sincronizacao_disco()
    if not caixa_ok:
        # A leitura da caixa de grupo falhou (ex.: conflito de sessão no SISDPU — outro
        # login simultâneo com o mesmo usuário). NÃO finge sucesso só porque sobraram PAJs
        # manuais: isso fazia a tela mostrar uma lista minúscula (só os manuais) como se
        # fosse o resultado normal, sem nenhum aviso — bug corrigido em 2026-11-10.
        detalhe = caixa.get("detalhe", "Falha ao ler a caixa.")
        if itens:
            detalhe = (f"Falha ao ler a caixa de grupo ({detalhe}) — mostrando só os "
                       f"{len(itens)} PAJ(s) cadastrado(s) manualmente.")
        return {"ok": False, "itens": itens, "detalhe": detalhe}
    return {"ok": True, "itens": itens, "detalhe": f"{len(itens)} pendência(s)."}


# ---------------------------------------------------------------------------
# processamento (SSE) — movimentar → tramitar (SEM conclusão: PAJ manual não
# tem trâmite originário na caixa para concluir)
# ---------------------------------------------------------------------------
def _ev(level: str, msg: str) -> dict:
    return {"level": level, "msg": msg}


def _passo_ev(etapa: str, ok: bool | None, detalhe: str) -> dict:
    icone = "✓" if ok else ("✗" if ok is False else "•")
    level = "ok" if ok else ("erro" if ok is False else "muted")
    return {"level": level, "msg": f"     {icone} {etapa}" + (f": {detalhe}" if detalhe else "")}


async def processar_paj_stream(
    paj_norm: str,
    grupo: str | None = None,
    prazo_dias: int = 0,
    dry_run: bool = False,
    manual: bool = True,
    origem: str = "individual",
) -> AsyncIterator[dict]:
    """Gera eventos SSE do processamento de UM PAJ: movimenta → tramita → (se houver
    trâmite na caixa) conclui. Assume os PDFs já baixados em 'Arquivos SAT'. `prazo_dias`
    > 0 define prazo de conclusão da tramitação (hoje + N dias); 0 = sem prazo.
    `manual` = True (padrão, compatível com o cadastro manual existente): localiza o PAJ
    SÓ no store local (sem id_tramite — não conclui, não há trâmite originário). False
    (fila automática): localiza o PAJ na caixa de grupo configurada (basta estar nela,
    sem exigir nenhuma descrição específica de trâmite) e, ao final, CONCLUI esse
    trâmite originário.
    `origem` = "individual" (padrão) ou "fila" — só identifica, para o histórico (coluna
    "Fluxo" de "PAJs Concluídos"), qual tela disparou o processamento; não afeta a lógica.
    Termina com {'done': True, 'resultado': {...}}."""
    nao_tramitar = (grupo == NAO_TRAMITAR)
    if not nao_tramitar:
        grupo = grupo or GRUPO_PADRAO
    prazo_data = None
    if prazo_dias and int(prazo_dias) > 0 and not nao_tramitar:
        prazo_data = (date.today() + timedelta(days=int(prazo_dias))).strftime("%d/%m/%Y")
    log: list[dict] = []

    def passo(etapa: str, ok: bool | None, detalhe: str) -> dict:
        log.append({"ts": datetime.now().isoformat(timespec="seconds"),
                    "etapa": etapa, "ok": (None if ok is None else bool(ok)),
                    "detalhe": str(detalhe or "")[:600]})
        return _passo_ev(etapa, ok, detalhe)

    num = _norm_to_num(paj_norm)
    prefixo = "[dry-run] " if dry_run else ""
    destino_txt = "sem tramitação" if nao_tramitar else f"grupo destino: {grupo}"
    yield _ev("novo", f"{prefixo}Processando {num} ({destino_txt})")

    id_tramite = ""
    saiu_da_caixa = False  # True = PAJ não está mais na caixa de grupo (ver abaixo)
    if manual:
        mp = next((m for m in _ler_pajs_manuais() if m.get("paj_norm") == paj_norm), None)
        if not mp or not mp.get("id_processo"):
            d = (f"PAJ {num} sem id do processo. Remova-o e adicione novamente "
                 f"(a extração do SISDPU obtém o id).")
            yield passo("Localização do PAJ", False, d)
            yield {"done": True, "resultado": {"ok": False, "status": "falha", "detalhe": d}}
            return
        id_proc = mp["id_processo"]
        item = {"id": id_proc, "id_tramite": "", "assistido": mp.get("assistido", "")}
        yield passo("Localização do PAJ", True, f"id_processo={id_proc}")
    else:
        yield _ev("info", "Localizando o trâmite na caixa de grupo...")
        from services import config_service as _cfg
        caixa_nome = _cfg.get_fila_caixa_origem()
        if not caixa_nome:
            d = "Nenhuma caixa de grupo configurada para a fila automática (Configurações › Parâmetros)."
            yield passo("Leitura da caixa", False, d)
            yield {"done": True, "resultado": {"ok": False, "status": "falha", "detalhe": d}}
            return
        try:
            caixa = await movtram.listar_caixa_grupo(caixa_nome, enriquecer=False)
        except Exception as e:  # noqa: BLE001
            caixa = {"ok": False, "detalhe": str(e)}
        if not caixa.get("ok"):
            d = caixa.get("detalhe", "Falha ao ler a caixa.")
            yield passo("Leitura da caixa", False, d)
            yield {"done": True, "resultado": {"ok": False, "status": "falha", "detalhe": d}}
            return
        achado = next((it for it in caixa.get("itens", [])
                       if _num_to_norm(it.get("paj", "")) == paj_norm), None)
        if achado:
            id_proc = achado.get("id", "")
            id_tramite = achado.get("id_tramite", "")
            item = {"id": id_proc, "id_tramite": id_tramite, "assistido": achado.get("assistido", "")}
            yield passo("Localização do trâmite", True, f"id_processo={id_proc} · id_tramite={id_tramite}")
        else:
            # PAJ não está mais na caixa (saiu/foi tratado por outra via) — não busca de
            # novo no SISDPU: o id_processo já é conhecido da última sincronização e não
            # muda. Movimentar e tramitar continuam normalmente; só a conclusão do
            # trâmite originário (que não existe mais ali) é pulada mais abaixo — pedido
            # do usuário, 2026-11-12.
            cache_item = next((i for i in (_ULTIMA_SINCRONIZACAO.get("itens") or [])
                               if i.get("paj_norm") == paj_norm), None)
            if not cache_item or not cache_item.get("id_processo"):
                d = (f"PAJ {num} não encontrado na caixa de grupo \"{caixa_nome}\" nem na "
                     "última sincronização — não há id do processo para prosseguir.")
                yield passo("Localização do trâmite", False, d)
                yield {"done": True, "resultado": {"ok": False, "status": "falha", "detalhe": d}}
                return
            saiu_da_caixa = True
            id_proc = cache_item["id_processo"]
            item = {"id": id_proc, "id_tramite": "", "assistido": cache_item.get("assistido", "")}
            d = (f"PAJ {num} não está mais na caixa \"{caixa_nome}\" — usando id_processo="
                 f"{id_proc} já conhecido. A conclusão do trâmite original será pulada "
                 "(movimentação e tramitação seguem normalmente).")
            yield passo("Localização do trâmite", True, d)

    # anexos = TUDO que estiver ATIVO na tabela: documentos do ASSISTIDO (pasta principal) +
    # do INSTITUIDOR (subpasta), numa única movimentação.
    anexos_assist = listar_arquivos_sat(paj_norm)
    anexos_inst = listar_arquivos_instituidor(paj_norm)
    anexos_outros = listar_arquivos_outros_cpfs(paj_norm)
    anexos = anexos_assist + anexos_inst + anexos_outros
    if not anexos:
        d = (f"Nenhum PDF em '{SUBPASTA_SAT}' para {num}. Baixe os documentos do SAT antes "
             f"de processar (pasta: {pasta_arquivos_sat(paj_norm)}).")
        yield passo("Documentos do SAT", False, d)
        yield {"done": True, "resultado": {"ok": False, "status": "falha", "detalhe": d}}
        return
    _det_anexos = f"{len(anexos)} arquivo(s) para juntar"
    if anexos_inst or anexos_outros:
        _det_anexos += (f" ({len(anexos_assist)} do assistido + {len(anexos_inst)} do instituidor"
                        + (f" + {len(anexos_outros)} de outros CPFs" if anexos_outros else "") + ")")
    yield passo("Documentos do SAT", True, _det_anexos)

    # "Levantamento SAT" — PDF anonimizado com tudo que o levantamento revelou no SAT
    # (baixado/anexado ou não), para quem for analisar o PAJ depois saber o que existe no
    # SAT sem precisar deste painel. Sempre o 1º anexo desta movimentação (pedido do
    # usuário, 2026-11-13). Falha ao gerar não impede a juntada dos documentos reais —
    # só segue sem ele.
    try:
        from services import levantamento_sat_service
        pdf_lev = levantamento_sat_service.gerar_pdf_levantamento_sat(paj_norm)
        nome_lev = levantamento_sat_service.nome_arquivo_levantamento(paj_norm)
        caminho_lev = _pasta_paj(paj_norm) / nome_lev
        caminho_lev.write_bytes(pdf_lev)
        anexos = [{"nome": nome_lev, "caminho": str(caminho_lev), "tipo": "pdf",
                  "tamanho": len(pdf_lev)}] + anexos
        yield passo("Levantamento SAT", True, f"Gerado e incluído como 1º anexo: {nome_lev}")
    except Exception as e:  # noqa: BLE001
        yield passo("Levantamento SAT", False, f"Falha ao gerar (seguindo sem ele): {e}")

    pajs = [{"id": id_proc, "numero": num}]
    resultado: dict[str, Any] = {"paj": num, "paj_norm": paj_norm, "grupo": grupo}

    # 1) MOVIMENTAR
    yield _ev("info", "Movimentando (juntada de documento)...")
    mv = await movtram.movimentar_paj(pajs, MOV_DESCRICAO, FASE_MOV, anexos, TIPO_ARQUIVO, dry_run)
    yield passo("Movimentação do PAJ (Juntada de documento)", mv.get("ok"), mv.get("detalhe", ""))
    resultado["mov_status"] = mv.get("status")
    resultado["mov_seq"] = mv.get("mov_seq")
    resultado["mov_detalhe"] = mv.get("detalhe", "")
    if not mv.get("ok"):
        _gravar_hist(paj_norm, item, anexos, ("" if nao_tramitar else grupo), resultado,
                     "falha", "Falha na movimentação.", log, origem)
        yield {"done": True, "resultado": {"ok": False, "status": "falha", "detalhe": mv.get("detalhe", "")}}
        return

    # 2) TRAMITAR (pulado se o usuário escolheu "Não tramitar")
    if nao_tramitar:
        yield passo("Tramitação", None,
                    "Não realizada — opção \"Não tramitar\" escolhida pelo usuário.")
        resultado["tram_status"] = "nao_tramitado"
        resultado["tram_caixa"] = ""
        resultado["tram_detalhe"] = "Tramitação não realizada — opção \"Não tramitar\" escolhida."
        tr_ok = True  # a movimentação (anexação) já teve sucesso; não há tramitação a avaliar
    else:
        prazo_txt = f"prazo até {prazo_data}" if prazo_data else "sem prazo"
        yield _ev("info", f"Tramitando para {grupo} ({prazo_txt})...")
        tr = await movtram.tramitar_paj(pajs, grupo=grupo, descricao=TRAM_DESCRICAO,
                                        prazo_data=prazo_data, dry_run=dry_run)
        yield passo(f"Tramitação para {grupo} ({prazo_txt})", tr.get("ok"), tr.get("detalhe", ""))
        resultado["tram_status"] = tr.get("status")
        resultado["tram_caixa"] = grupo
        resultado["tram_detalhe"] = tr.get("detalhe", "")
        tr_ok = tr.get("ok")
        if not tr_ok:
            _gravar_hist(paj_norm, item, anexos, grupo, resultado, "parcial",
                         "Movimentou, mas falhou a tramitação.", log, origem)
            yield {"done": True, "resultado": {"ok": False, "status": "parcial", "detalhe": tr.get("detalhe", "")}}
            return

    # 3) CONCLUIR o trâmite originário — SÓ se houver um (caixa). PAJ manual não tem
    # trâmite na caixa → nada a concluir (fluxo termina na tramitação, como sempre foi).
    if id_tramite:
        yield _ev("info", "Concluindo o trâmite originário...")
        cc = await movtram.concluir_paj([id_tramite], dry_run)
        yield passo("Conclusão do trâmite originário", cc.get("ok"), cc.get("detalhe", ""))
        resultado["concluido_status"] = cc.get("status")
        resultado["concluido_detalhe"] = cc.get("detalhe", "")
        anexacao_ok = cc.get("ok")
        status = "dry_run" if dry_run else ("concluido" if cc.get("ok") else "parcial")
        if dry_run:
            detalhe = "[dry-run] Simulação concluída — nada foi efetivado."
        elif not cc.get("ok"):
            detalhe = "Movimentou, mas a conclusão do trâmite falhou."
        elif nao_tramitar:
            detalhe = "Movimentado e trâmite concluído (sem tramitação — opção \"Não tramitar\")."
        else:
            detalhe = "Fluxo concluído: movimentado, tramitado e trâmite concluído."
    else:
        if saiu_da_caixa:
            resultado["concluido_status"] = "nao_aplicavel"
            resultado["concluido_detalhe"] = ("PAJ já havia saído da caixa de grupo — não há "
                                              "trâmite originário para concluir.")
            yield passo("Conclusão", None,
                        "PAJ já havia saído da caixa de grupo: não há trâmite para concluir — "
                        "etapa pulada (movimentação e tramitação feitas normalmente).")
        else:
            resultado["concluido_status"] = "nao_aplicavel"
            resultado["concluido_detalhe"] = "PAJ manual — sem trâmite na caixa para concluir."
            yield passo("Conclusão", None,
                        "PAJ incluído manualmente: não há trâmite na caixa para concluir — etapa ignorada.")
        anexacao_ok = tr_ok
        status = "dry_run" if dry_run else ("concluido" if tr_ok else "parcial")
        detalhe = ("[dry-run] Simulação concluída — nada foi efetivado." if dry_run
                   else ("Movimentado (sem tramitação — opção \"Não tramitar\")." if (nao_tramitar and tr_ok)
                         else (("Movimentado e tramitado (PAJ já havia saído da caixa — "
                                "conclusão do trâmite original pulada)." if saiu_da_caixa else
                                "Movimentado e tramitado (conclusão não se aplica a PAJ manual).")
                               if tr_ok else "Movimentou, mas falhou a tramitação.")))

    # Compilação: apagar os PDFs originais SÓ APÓS a anexação (o compilado já foi anexado na
    # movimentação; os originais ficaram numa subpasta não-anexada até aqui).
    for _pp in (pastas_pessoas(paj_norm) if (anexacao_ok and not dry_run) else []):
        orig = _pp / SUBPASTA_ORIGINAIS_COMPILADOS
        if orig.is_dir():
            n = len(list(orig.glob("*.pdf")))
            try:
                from services import exclusao_suave  # Lixeira, fora do processo do painel
                exclusao_suave.apagar_pasta(orig, "sat:compilados-originais")
                yield passo("Limpeza dos originais compilados", True,
                            f"{n} PDF(s) originais removidos após a anexação do(s) compilado(s).")
            except Exception as e:  # noqa: BLE001
                yield passo("Limpeza dos originais compilados", None, str(e))
    _gravar_hist(paj_norm, item, anexos, ("" if nao_tramitar else grupo), resultado, status, detalhe, log, origem)
    # PAJ manual processado com sucesso sai da lista de pendências (some do store). PAJ da
    # caixa não precisa disso: sai sozinho da caixa quando o trâmite é concluído. Um PAJ
    # "saiu da caixa" processado com sucesso também sai sozinho: como `id_tramite` fica
    # vazio para ele, `ja_processado` nunca ficaria True sem essa remoção explícita (ver
    # `_campos_locais`) e ele continuaria aparecendo pendente para sempre.
    if manual and status == "concluido" and not dry_run:
        with __import__("contextlib").suppress(Exception):
            del_paj_manual(paj_norm, apagar_arquivos=False, registrar_historico=False)
    elif saiu_da_caixa and status == "concluido" and not dry_run:
        with __import__("contextlib").suppress(Exception):
            _remover_de_sincronizacao(paj_norm)
    yield {"done": True, "resultado": {"ok": bool(dry_run or anexacao_ok), "status": status, "detalhe": detalhe}}


async def anexar_mais_stream(
    paj_norm: str, rec_id: str, id_processo: str, assistido: str, nomes: list[str],
) -> AsyncIterator[dict]:
    """Anexação COMPLEMENTAR num PAJ já concluído: nova movimentação (SÓ juntada) com os
    documentos escolhidos — SEM tramitar nem concluir. Complementa o registro `rec_id` do
    histórico (lista `movs_extra` + `log`) para exibir o badge da nova movimentação na aba
    'PAJs Trabalhados'."""
    num = _norm_to_num(paj_norm)
    log_ev: list[dict] = []

    def passo(etapa: str, ok, det: str) -> dict:
        log_ev.append({"ts": datetime.now().isoformat(timespec="seconds"), "etapa": etapa,
                       "ok": (None if ok is None else bool(ok)), "detalhe": str(det or "")[:600]})
        return _passo_ev(etapa, ok, det)

    yield _ev("novo", f"Anexação complementar em {num}…")
    # resolve os anexos escolhidos — TODOS os documentos baixados são passíveis de nova
    # anexação (ativos E excluídos da lixeira; assistido + instituidor).
    disponiveis = {d["nome"]: d for d in listar_todos_arquivos_sat(paj_norm)}
    anexos = [{"nome": n, "caminho": disponiveis[n]["caminho"], "tipo": "pdf"}
              for n in (nomes or []) if n in disponiveis]
    if not anexos:
        yield passo("Documentos", False, "Nenhum documento válido selecionado.")
        yield {"done": True, "resultado": {"ok": False, "detalhe": "Nenhum documento selecionado."}}
        return
    yield passo("Documentos", True, f"{len(anexos)} documento(s) para anexar")
    if not id_processo:
        yield passo("Movimentação", False, "Sem id do processo no registro.")
        yield {"done": True, "resultado": {"ok": False, "detalhe": "Sem id do processo."}}
        return

    # "Levantamento SAT" — recriado a CADA anexação complementar (nunca um arquivo
    # estático): mostra em card verde exatamente o que está sendo anexado NESTA
    # movimentação, mantém em cards cinza (datados) o que já tinha sido anexado antes, e
    # atualiza a lista do que ainda não foi anexado. Sempre o 1º anexo. Falha ao gerar não
    # impede a juntada dos documentos escolhidos — só segue sem ele (pedido do usuário,
    # 2026-11-13).
    try:
        from services import levantamento_sat_service
        pdf_lev = levantamento_sat_service.gerar_pdf_levantamento_sat(
            paj_norm, nomes_rodada_atual=[a["nome"] for a in anexos])
        nome_lev = levantamento_sat_service.nome_arquivo_levantamento(paj_norm)
        caminho_lev = _pasta_paj(paj_norm) / nome_lev
        caminho_lev.write_bytes(pdf_lev)
        anexos = [{"nome": nome_lev, "caminho": str(caminho_lev), "tipo": "pdf"}] + anexos
        yield passo("Levantamento SAT", True, f"Gerado e incluído como 1º anexo: {nome_lev}")
    except Exception as e:  # noqa: BLE001
        yield passo("Levantamento SAT", False, f"Falha ao gerar (seguindo sem ele): {e}")

    yield _ev("info", "Movimentando (anexação complementar)…")
    mv = await movtram.movimentar_paj([{"id": id_processo, "numero": num}],
                                      MOV_DESCRICAO_COMPLEMENTAR, FASE_MOV, anexos, TIPO_ARQUIVO, False)
    yield passo("Movimentação complementar", mv.get("ok"), mv.get("detalhe", ""))
    if not mv.get("ok"):
        yield {"done": True, "resultado": {"ok": False, "detalhe": mv.get("detalhe", "")}}
        return
    # complementa o registro existente (badge da nova mov + log)
    extra = {"seq": mv.get("mov_seq"), "ts": datetime.now().isoformat(timespec="seconds"),
             "n": len(anexos), "arquivos": [a["nome"] for a in anexos]}
    with __import__("contextlib").suppress(Exception):
        reg = next((r for r in historico_sat.listar() if r.get("id") == rec_id), None)
        movs = list((reg or {}).get("movs_extra") or []) + [extra]
        novo_log = list((reg or {}).get("log") or []) + log_ev
        historico_sat.atualizar(rec_id, {"movs_extra": movs, "log": novo_log})
    yield passo("Histórico atualizado", True,
                f"Mov {mv.get('mov_seq')} registrada como anexação complementar (sem tramitar/concluir)")
    yield {"done": True, "resultado": {"ok": True, "mov_seq": mv.get("mov_seq"), "n": len(anexos)}}


def mesclar_complementar_pendente(paj_norm: str, escopo: str = "assistido") -> dict:
    """Download complementar em PAJ AINDA NÃO ANEXADO: move os PDFs da subpasta
    'Complementar' para a pasta do escopo (assistido → principal; instituidor → 'Instituidor')
    e MESCLA o log/inventário no _log_download.json daquela pasta — upsert do inventário por
    (key, sub), salvos/pulados somados e eventos anexados com um marcador (log complementar
    visível no mesmo visualizador). SEM movimentação. Retorna {ok, movidos, n_arquivos}."""
    paths = _escopo_paths(paj_norm, escopo)
    comp = paths["complementar"]
    principal = paths["destino"]
    principal.mkdir(parents=True, exist_ok=True)
    movidos: list[str] = []
    if comp.is_dir():
        for f in sorted(comp.glob("*.pdf")):
            try:
                shutil.move(str(f), str(principal / f.name))
                movidos.append(f.name)
            except Exception:  # noqa: BLE001
                pass

    def _ler(p):
        try:
            return json.loads(p.read_text(encoding="utf-8")) if os.path.isfile(str(p)) else {}
        except Exception:  # noqa: BLE001
            return {}

    main = _ler(principal / "_log_download.json")
    cdata = _ler(comp / "_log_download.json")
    if cdata:
        salvos = list(main.get("salvos", []) or [])
        for s in cdata.get("salvos", []) or []:
            if s not in salvos:
                salvos.append(s)
        main["salvos"] = salvos
        # Merge do inventário SEM REBAIXAR: o download complementar marca os tipos NÃO
        # escolhidos como "nao_selecionado" — isso NÃO pode sobrescrever um "baixado"
        # anterior (senão os já baixados sumiriam do histórico). Prioridade: baixado > outros
        # status reais > nao_selecionado; e nunca introduzimos "nao_selecionado" como novidade.
        _PRIO = {"baixado": 3, "nao_selecionado": 0}
        _p = lambda st: _PRIO.get(st, 1)  # noqa: E731
        inv = list(main.get("inventario", []) or [])
        idx = {(it.get("key"), it.get("sub")): i for i, it in enumerate(inv)}
        for it in cdata.get("inventario", []) or []:
            chave = (it.get("key"), it.get("sub"))
            if chave in idx:
                if _p(it.get("status")) >= _p(inv[idx[chave]].get("status")):
                    inv[idx[chave]] = it   # só sobe/empata — nunca rebaixa um "baixado"
            elif it.get("status") != "nao_selecionado":
                inv.append(it); idx[chave] = len(inv) - 1  # ignora ruído de "não selecionado"
        main["inventario"] = inv
        main["pulados"] = list(main.get("pulados", []) or []) + list(cdata.get("pulados", []) or [])
        ev = list(main.get("eventos", []) or [])
        ev.append({"level": "novo",
                   "msg": f"── Download complementar (pendência) — "
                          f"{datetime.now():%d-%m-%Y %H:%M} ──"})
        ev.extend(cdata.get("eventos", []) or [])
        main["eventos"] = ev
        try:
            (principal / "_log_download.json").write_text(
                json.dumps(main, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    with __import__("contextlib").suppress(Exception):
        from services import exclusao_suave  # Lixeira, fora do processo do painel
        exclusao_suave.apagar_pasta(comp, "sat:complementar")
    # Se este PAJ já tem lista de batimento, puxa pra ela o resultado recém-mesclado no
    # log principal — assim o modal (e a fila automática) já enxergam o novo status.
    with __import__("contextlib").suppress(Exception):
        _mesclar_resultado_em_batimento(principal)
    return {"ok": True, "movidos": movidos, "n_arquivos": len(paths["listar"](paj_norm)),
            "tem_falha": _download_tem_falha(paj_norm, principal)}


def _gravar_hist(paj_norm, item, anexos, grupo, resultado, status, detalhe, log, origem="individual") -> None:
    if resultado.get("_dry"):  # nunca grava dry-run
        return
    if status == "dry_run":
        return
    historico_sat.registrar({
        "paj": _norm_to_num(paj_norm),
        "paj_norm": paj_norm,
        "fluxo": origem,
        "id_processo": item.get("id", ""),
        "id_tramite": item.get("id_tramite", ""),
        "assistido": item.get("assistido", ""),
        "cpf": _cpf_do_paj(paj_norm),
        "n_arquivos": len(anexos),
        "arquivos": [a["nome"] for a in anexos],
        "mov_status": resultado.get("mov_status"),
        "mov_seq": resultado.get("mov_seq"),
        "mov_detalhe": resultado.get("mov_detalhe", ""),
        "tram_status": resultado.get("tram_status"),
        "tram_caixa": grupo,
        "tram_detalhe": resultado.get("tram_detalhe", ""),
        "concluido_status": resultado.get("concluido_status"),
        "concluido_detalhe": resultado.get("concluido_detalhe", ""),
        "status": status,
        "detalhe": detalhe,
        "log": log,
    })


# ---------------------------------------------------------------------------
# Fila automática: varre a caixa (listar_pendentes) e, para CADA PAJ pendente, baixa os
# documentos do SAT (CAPTCHA automático pelo CapSolver, se ligado em Configurações) e
# processa (movimenta → tramita p/ 01 - PREV_DIVPREV → conclui o trâmite originário), um
# PAJ de cada vez. O "ritmo lento" entre PAJs é o MESMO intervalo entre CPFs do download
# (sat_client._aguardar_intervalo_cpf — sorteado entre Configurações › intervalo_cpf e
# intervalo_cpf_max) — não precisa de uma pausa própria aqui.
# Roda em segundo plano (asyncio task): sobrevive a reconexões da tela; a tela consulta
# o progresso por polling (estado_fila_automatica), não por SSE.
# ---------------------------------------------------------------------------
_FILA: dict[str, Any] = {
    "rodando": False, "parar": False, "paj_atual": "", "iniciado_em": "",
    "processados": [], "log": deque(maxlen=400), "erro": "",
}
_FILA_TASK: "asyncio.Task | None" = None


def _fila_log(msg: str, level: str = "info") -> None:
    _FILA["log"].append({"ts": datetime.now().strftime("%H:%M:%S"), "level": level, "msg": msg})


def estado_fila_automatica() -> dict:
    """Estado atual da fila automática, para a tela consultar por polling."""
    return {
        "rodando": _FILA["rodando"], "paj_atual": _FILA["paj_atual"],
        "iniciado_em": _FILA["iniciado_em"], "processados": list(_FILA["processados"]),
        "log": list(_FILA["log"]), "erro": _FILA["erro"],
    }


async def _rodar_fila_automatica() -> None:
    from ingestao import sat_client
    from services import config_service as _cfg

    _FILA.update(rodando=True, parar=False, paj_atual="", erro="")
    _FILA["iniciado_em"] = datetime.now().isoformat(timespec="seconds")
    _fila_log("Fila automática iniciada.")

    # A fila usa a ÚLTIMA SINCRONIZAÇÃO da caixa de grupo (feita por "Sincronizar caixa"
    # na tela) — NÃO lê o SISDPU de novo aqui. Uma vez sincronizada, a caixa de grupo não
    # é mais consultada até a próxima sincronização manual ou a juntada ("Anexar ao
    # PAJ"); o download de cada PAJ é 100% SAT Central. Isso elimina qualquer
    # interferência de conflito de sessão do SISDPU durante o download (bug corrigido em
    # 2026-11-10).
    pend = pendentes_sincronizados()
    if not pend.get("ok"):
        _FILA["erro"] = pend.get("detalhe", "Sincronize a caixa antes de iniciar a fila.")
        _fila_log(_FILA["erro"], "erro")
        _FILA.update(rodando=False, paj_atual="")
        _fila_log("Fila automática encerrada.")
        return

    itens_pend = pend.get("itens", [])
    # PAJ sem CPF do assistido principal na última sincronização (ex.: soluço de leitura na
    # hora do "Sincronizar caixa") só é avisado e pulado de vez quando TAMBÉM não tem nenhum
    # CPF extra (terceiro/instituidor) cadastrado — nesse caso não há nada que a fila consiga
    # processar. Quando há CPF extra, o PAJ continua na fila: a etapa do assistido principal é
    # pulada, mas os CPFs extras são processados normalmente por `_fila_baixar_cpfs_extras`,
    # que não depende do CPF do assistido principal (corrige bug onde um PAJ inteiro era
    # pulado por falta do CPF principal, mesmo com um terceiro com CPF cadastrado — pedido do
    # usuário, 2026-11-12). Não se tenta relê-lo no SISDPU agora: isso tocaria o SISDPU de novo
    # no meio do download, exatamente o que a fila deixou de fazer. Corrige-se sincronizando a
    # caixa de novo.
    sem_cpf_sem_extra = [i for i in itens_pend if not i.get("ja_processado") and not i.get("cpf")
                         and not listar_cpfs_extras(i["paj_norm"])]
    if sem_cpf_sem_extra:
        nomes = ", ".join(_norm_to_num(i["paj_norm"]) for i in sem_cpf_sem_extra)
        _fila_log(f"⚠ {len(sem_cpf_sem_extra)} PAJ(s) sem CPF (nem principal, nem extra) na "
                  f"última sincronização ({nomes}) — pulados nesta rodada. Clique em "
                  "\"Sincronizar caixa\" para tentar de novo.", "erro")

    # PAJs desmarcados pelo usuário (checkbox da tabela) não entram nesta rodada — escolha
    # sempre reversível, só decide o que ESTA rodada processa (pedido do usuário, 2026-11-13).
    nao_selecionados = [i for i in itens_pend if not i.get("ja_processado")
                        and (i.get("cpf") or listar_cpfs_extras(i["paj_norm"]))
                        and i.get("selecionado_fila", True) is False]
    if nao_selecionados:
        nomes = ", ".join(_norm_to_num(i["paj_norm"]) for i in nao_selecionados)
        _fila_log(f"ℹ {len(nao_selecionados)} PAJ(s) desmarcado(s) pelo usuário ({nomes}) — "
                  "não entram nesta rodada.", "muted")

    alvos = [i for i in itens_pend if not i.get("ja_processado")
             and (i.get("cpf") or listar_cpfs_extras(i["paj_norm"]))
             and i.get("selecionado_fila", True) is not False]
    if not alvos:
        _fila_log("Nenhum PAJ pendente (com CPF) na caixa — fila concluída.", "ok")
        _FILA.update(rodando=False, paj_atual="")
        _fila_log("Fila automática encerrada.")
        return

    try:
        for alvo in alvos:
            if _FILA["parar"]:
                _fila_log("Parada solicitada pelo usuário.", "muted")
                break
            paj_norm = alvo["paj_norm"]
            num = _norm_to_num(paj_norm)
            _FILA["paj_atual"] = paj_norm
            # Marca bem visível de onde começa o trabalho de CADA PAJ no log — cabeçalho
            # com fundo contrastante (nível próprio "paj-inicio", ver front-end) —
            # pedido do usuário, 2026-11-13.
            _fila_log(f"━━ {num} · {alvo.get('assistido', '')} (CPF {alvo.get('cpf') or '—'}) ━━", "paj-inicio")

            # Try/except POR PAJ: um erro inesperado (ex.: perda de conexão com o navegador
            # do SAT em pleno bloqueio antirobô) não pode derrubar a rodada inteira — os
            # demais PAJs da caixa devem continuar sendo processados normalmente. O PAJ que
            # falhou fica marcado como pendente e é tentado de novo na próxima rodada.
            try:
                ok_etapa, detalhe_etapa, parado, sessao_expirada, conexao_perdida = True, "", False, False, False
                pausado_multiplos = False
                if not alvo.get("cpf"):
                    # Assistido principal sem CPF (mas há ao menos um CPF extra — do
                    # contrário o PAJ nem entraria em `alvos`): pula a etapa dele e vai
                    # direto para os CPFs extras logo abaixo.
                    _fila_log("Assistido principal sem CPF cadastrado — pulando a etapa dele "
                              "e indo direto para os CPFs extras.", "muted")
                    ja_baixado = True
                else:
                    ja_baixado = bool(alvo.get("tem_log")) and not alvo.get("tem_falha")
                if alvo.get("cpf") and not ja_baixado:
                    defaults = _cfg.get_sat_central_defaults()
                    pasta_p = pasta_arquivos_sat(paj_norm, alvo["cpf"], alvo.get("assistido", ""))
                    pasta = str(pasta_p)
                    pat_ignorar: list[str] = []
                    with __import__("contextlib").suppress(Exception):
                        pat_ignorar = _cfg.get_sat_pat_ignorar()
                    pat_filtro = None
                    with __import__("contextlib").suppress(Exception):
                        pat_filtro = _cfg.resolver_sat_pat_filtro(alvo.get("pretensao", ""))
                    filtros = ler_filtros(paj_norm)
                    if filtros["nbs"] or filtros["protocolos"]:
                        _fila_log("Filtros deste PAJ — "
                                  + "; ".join(x for x in (
                                      f"NB: {filtros['nbs']}" if filtros["nbs"] else "",
                                      f"protocolo (PAT): {filtros['protocolos']}" if filtros["protocolos"] else "")
                                      if x), "info")

                    bat = ler_batimento(pasta_p)
                    if not bat.get("itens"):
                        # 1ª RODADA: levantamento COMPLETO do SAT (tudo que existe para o
                        # CPF, independente da pretensão) ANTES do download — monta a
                        # lista de batimento que vai nortear esta e TODAS as próximas
                        # rodadas. Da 2ª rodada em diante, pretensão e filtro nunca mais
                        # são reconsultados — só a lista de batimento manda (pedido do
                        # usuário, 2026-11-12).
                        _fila_log("1ª rodada — levantamento completo do SAT antes do "
                                  "download…", "etapa-lev")
                        res_lev: dict = {}
                        async for ev in sat_client.levantar_cidadao_stream(
                                alvo["cpf"], alvo.get("assistido", ""), pasta,
                                delay_s=defaults["tempo_entre"], bloqueio_s=defaults["espera_bloqueio"],
                                delay_max_s=defaults.get("tempo_entre_max")):
                            if _FILA["parar"]:
                                parado = True
                            if ev.get("done"):
                                res_lev = ev.get("resultado", {}) or {}
                            else:
                                _fila_log(ev.get("msg", ""), ev.get("level", "info"))
                        if not res_lev.get("ok"):
                            ok_etapa = False
                            parado = parado or bool(res_lev.get("encerrado"))
                            detalhe_etapa = (res_lev.get("detalhe")
                                             or "Falha no levantamento — PAJ mantido na caixa.")
                        else:
                            tipos_sel = None
                            with __import__("contextlib").suppress(Exception):
                                tipos_sel = _cfg.resolver_sat_tipos(
                                    alvo.get("pretensao", ""), sat_client.TIPOS_KEYS)
                            itens_bat = _montar_batimento_inicial(pasta_p, tipos_sel, filtros)
                            _salvar_batimento(pasta_p, itens_bat)
                            bat = {"itens": itens_bat}

                    if ok_etapa and not parado and _tem_levantamento_pendente(bat.get("itens") or []):
                        # Uma rodada anterior não conseguiu abrir o modal de algum tipo de
                        # documento (bloqueio de antirrobô) — a montagem inicial do batimento
                        # ficou com um "buraco" permanente, já que a lista de batimento manda
                        # a partir da 2ª rodada e o levantamento nunca mais seria refeito.
                        # Tenta de novo aqui, SEM afetar nada já decidido/baixado (pedido do
                        # usuário, 2026-11-13).
                        _fila_log("Levantamento anterior ficou pendente (bloqueio de antirrobô) "
                                  "— tentando novo levantamento antes do download…", "etapa-lev")
                        tipos_sel2 = None
                        with __import__("contextlib").suppress(Exception):
                            tipos_sel2 = _cfg.resolver_sat_tipos(
                                alvo.get("pretensao", ""), sat_client.TIPOS_KEYS)
                        res_lev2: dict = {}
                        async for ev in sat_client.levantar_cidadao_stream(
                                alvo["cpf"], alvo.get("assistido", ""), pasta,
                                delay_s=defaults["tempo_entre"], bloqueio_s=defaults["espera_bloqueio"],
                                delay_max_s=defaults.get("tempo_entre_max")):
                            if _FILA["parar"]:
                                parado = True
                            if ev.get("done"):
                                res_lev2 = ev.get("resultado", {}) or {}
                            else:
                                _fila_log(ev.get("msg", ""), ev.get("level", "info"))
                        if res_lev2.get("ok"):
                            if _mesclar_novo_levantamento_em_batimento(pasta_p, tipos_sel2, filtros):
                                _fila_log("Levantamento pendente resolvido — lista de batimento "
                                          "atualizada.", "ok")
                                bat = ler_batimento(pasta_p)
                            else:
                                _fila_log("Ainda bloqueado — sem novidade desta vez.", "muted")
                        elif not parado:
                            _fila_log("Nova tentativa de levantamento falhou — seguindo com a "
                                      "lista de batimento como estava.", "muted")

                    if ok_etapa and not parado:
                        multiplos, multiplos_detalhe, multiplos_revisado = _multiplos_requerimentos(pasta_p)
                        if multiplos and multiplos_revisado:
                            # Já foi revisado e salvo em "Baixar mais" pelo menos uma vez — o
                            # usuário escolheu conscientemente manter o grupo acima do limite.
                            # Não pausa mais por este motivo: segue para o download normal do
                            # que estiver `incluir=True` (pedido do usuário, 2026-11-13).
                            _fila_log(f"Múltiplos Requerimentos revisado e salvo — {multiplos_detalhe} — "
                                      "seguindo com o download normal.", "muted")
                        if multiplos and not multiplos_revisado:
                            # Grupo(s) com documentos pendentes demais (ex.: dezenas de CRER/
                            # Laudo Médico/Tarefas PAT) — não é produtivo baixar tudo sozinho.
                            # Pausa esta rodada (sem gastar nenhuma consulta no SAT) e pede
                            # revisão manual em "Baixar mais" antes de prosseguir — pedido do
                            # usuário, 2026-11-13. Não é falha: a fila não tenta de novo
                            # sozinha até a seleção ser revisada (ela sai do estado sozinha
                            # quando os pendentes do grupo caírem abaixo do limite, ou quando a
                            # seleção for salva em "Baixar mais" pelo menos uma vez).
                            detalhe_etapa = (f"Múltiplos Requerimentos — {multiplos_detalhe} — "
                                              "revise manualmente em \"Baixar mais\" antes de "
                                              "prosseguir com o download automático.")
                            _fila_log(detalhe_etapa, "erro")
                            ok_etapa = True
                            pausado_multiplos = True
                        else:
                            tipos, alvos_bat = _alvos_pendentes_batimento(bat.get("itens") or [])
                            if not alvos_bat:
                                _fila_log("Nada pendente na lista de batimento — já está tudo "
                                          "baixado ou dispensado.", "muted")
                            else:
                                _fila_log("Buscando no SAT só o que está marcado na lista de "
                                          "batimento, em: " + ", ".join(sorted(tipos)), "etapa-down")
                                res_baixar: dict = {}
                                try:
                                    async for ev in sat_client.baixar_cidadao_stream(
                                            alvo["cpf"], alvo.get("assistido", ""), pasta,
                                            delay_s=defaults["tempo_entre"], bloqueio_s=defaults["espera_bloqueio"],
                                            delay_max_s=defaults.get("tempo_entre_max"),
                                            tipos=tipos, alvos=alvos_bat, pretensao=alvo.get("pretensao", ""),
                                            alvos_revisados=bool(bat.get("revisado")),
                                            compilar=defaults["compilar"], compilar_limiar=defaults["compilar_limiar"],
                                            pat_ignorar=pat_ignorar, pat_filtro=pat_filtro,
                                            nbs=_itens_filtro(filtros["nbs"]) or None,
                                            protocolos=_itens_filtro(filtros["protocolos"]) or None):
                                        if _FILA["parar"]:
                                            parado = True
                                        if ev.get("done"):
                                            res_baixar = ev.get("resultado", {}) or {}
                                        else:
                                            _fila_log(ev.get("msg", ""), ev.get("level", "info"))
                                finally:
                                    # Mescla o que já foi baixado até aqui, MESMO que o laço
                                    # acima tenha sido interrompido por uma exceção (ex.: perda
                                    # de conexão com a janela do SAT no meio do caminho) — sem
                                    # isto, documentos já baixados com sucesso ficavam pendentes
                                    # na lista de batimento e eram baixados de novo à toa na
                                    # próxima rodada (identificado 2026-11-13).
                                    _mesclar_resultado_em_batimento(pasta_p)
                                ok_etapa = bool(res_baixar.get("ok"))
                                detalhe_etapa = res_baixar.get("detalhe", "")
                                if res_baixar.get("sessao_expirada"):
                                    sessao_expirada = True
                                    parado = True
                                # Download "ok" com documento(s) ainda pendentes na lista de
                                # batimento (falha de conexão/antirrobô/indisponível): NÃO
                                # anexa nem conclui o trâmite — o PAJ fica na caixa como
                                # "incompleto" e é tentado de novo na próxima rodada (só o que
                                # ainda estiver `incluir=True` e não baixado).
                                if ok_etapa and not parado and tem_pendencia_batimento(pasta_p):
                                    n_pend = _contar_pendentes_batimento(pasta_p)
                                    ok_etapa = False
                                    detalhe_etapa = (
                                        f"Download incompleto ({n_pend} documento(s) pendente(s) "
                                        "na lista de batimento) — nada foi anexado e o trâmite "
                                        "não foi concluído. O PAJ fica na caixa para nova tentativa.")
                elif alvo.get("cpf"):
                    _fila_log("Documentos já baixados anteriormente — indo direto para a juntada.", "muted")

                # CPFs extras (instituidor / outros assistidos) cadastrados no "+" da fila:
                # cada um tem sua consulta no SAT; qualquer falha deixa o PAJ "incompleto"
                # (nada é anexado nem concluído) e ele volta na próxima rodada.
                if ok_etapa and not parado:
                    ok_etapa, detalhe_extra, parado, sessao_expirada_extra = await _fila_baixar_cpfs_extras(paj_norm, alvo)
                    if detalhe_extra:
                        detalhe_etapa = detalhe_extra
                    if sessao_expirada_extra:
                        sessao_expirada = True

                if ok_etapa and not parado and not pausado_multiplos:
                    # A fila automática NÃO movimenta/tramita/conclui mais sozinha (Etapa 4 é
                    # sempre manual, por PAJ, via botão "Anexar ao PAJ" — mesmo mecanismo da
                    # aba Individual, com `manual=0` para concluir o trâmite originário da
                    # caixa de grupo). Isso elimina qualquer dependência do SISDPU durante o
                    # download (Etapa 2): a fila só toca o SAT Central.
                    detalhe_etapa = ("Documentos baixados — prontos para anexar ao PAJ no SISDPU "
                                      "(botão \"Anexar ao PAJ\").")
                    _fila_log(detalhe_etapa, "muted")
            except Exception as e:  # noqa: BLE001
                ok_etapa, sessao_expirada = False, False
                msg_erro = str(e)
                # "Connection closed..."/"connect_over_cdp" = a janela do SAT (o processo do
                # Chromium) caiu de vez no meio do caminho — reconectar não resolve sozinho, e
                # TODO PAJ seguinte falharia na mesma hora com o mesmo erro. Em vez de repetir
                # a mesma mensagem genérica PAJ a PAJ (confuso, como relatado pelo usuário,
                # 2026-11-13), detecta isso e para a rodada inteira com UM aviso claro.
                conexao_perdida = ("Connection closed" in msg_erro or "connect_over_cdp" in msg_erro)
                parado = conexao_perdida
                if conexao_perdida:
                    detalhe_etapa = ("Conexão com a janela do SAT foi perdida no meio do "
                                      "processamento — PAJ mantido na caixa para nova tentativa.")
                else:
                    detalhe_etapa = (f"Erro inesperado ({e}) — PAJ mantido na caixa para nova "
                                      "tentativa na próxima rodada.")
                _fila_log(f"✗ Erro inesperado neste PAJ — pulando para o próximo: {e}", "erro")

            _FILA["processados"].append({
                "paj": num, "paj_norm": paj_norm, "assistido": alvo.get("assistido", ""),
                "ok": ok_etapa, "detalhe": detalhe_etapa,
                "ts": datetime.now().isoformat(timespec="seconds"),
            })
            _fila_log(("✓ " if ok_etapa else "✗ ") + f"{num}: {detalhe_etapa}",
                      "ok" if ok_etapa else "erro")
            if sessao_expirada:
                _fila_log("Sessão do SAT expirada — fila interrompida para não repetir a mesma falha "
                          "nos demais PAJs. Clique no ícone de reconectar ao lado do indicador \"SAT\", "
                          "no topo da página, refaça o login e inicie a fila novamente.", "erro")
            if conexao_perdida:
                _fila_log("Conexão com a janela do SAT foi perdida — fila interrompida para não "
                          "repetir a mesma falha nos demais PAJs. Reabra a janela do SAT (ícone de "
                          "reconectar ao lado do indicador \"SAT\", no topo da página) e inicie a "
                          "fila automática novamente.", "erro")
            if parado:
                break
        else:
            _fila_log(f"Os {len(alvos)} PAJ(s) da caixa foram processados — fila concluída.", "ok")
    except Exception as e:  # noqa: BLE001
        _FILA["erro"] = str(e)
        _fila_log(f"Erro inesperado: {e}", "erro")
    finally:
        _FILA["paj_atual"] = ""
        _FILA["rodando"] = False
        _fila_log("Fila automática encerrada.")


async def _fila_baixar_cpfs_extras(paj_norm: str, alvo: dict) -> tuple[bool, str, bool, bool]:
    """Baixa, na fila automática, os documentos de cada CPF extra do PAJ que ainda não foi
    baixado (ou cujo último download teve falha). Retorna (ok, detalhe, parado, sessao_expirada)."""
    from ingestao import sat_client
    from services import config_service as _cfg
    defaults = _cfg.get_sat_central_defaults()
    n_outro = 1
    for pessoa in listar_cpfs_extras(paj_norm):
        if _FILA["parar"]:
            return True, "", True, False
        papel, cpf, nome = pessoa.get("papel", ""), pessoa.get("cpf", ""), pessoa.get("nome", "")
        instituidor = papel == "Instituidor"
        pasta = pasta_cpf_extra(paj_norm, pessoa)
        m_n = re.match(r"Assistido(\d+)", pasta.name)
        n_outro = int(m_n.group(1)) if m_n else n_outro
        if (pasta / "_log_download.json").exists() and not _download_tem_falha(paj_norm, pasta):
            _fila_log(f"{papel} (CPF {cpf}): documentos já baixados.", "muted")
            continue
        if instituidor:
            tipos_sel = _cfg.get_sat_tipos_instituidor()
            if tipos_sel is None:
                tipos_sel = list(sat_client.TIPOS_INSTITUIDOR_DEFAULT)
            pretensao, rotulo = "", "INSTITUIDOR - "
        else:
            tipos_sel = None
            with __import__("contextlib").suppress(Exception):
                tipos_sel = _cfg.resolver_sat_tipos(alvo.get("pretensao", ""), sat_client.TIPOS_KEYS)
            pretensao = alvo.get("pretensao", "")
            rotulo = "TERCEIRO - " if papel == "Terceiro" else f"ASSISTIDO {n_outro} - "
        pat_ignorar: list[str] = []
        with __import__("contextlib").suppress(Exception):
            pat_ignorar = _cfg.get_sat_pat_ignorar()
        pasta.mkdir(parents=True, exist_ok=True)
        parado = False

        bat = ler_batimento(pasta)
        if not bat.get("itens"):
            _fila_log(f"{papel} (CPF {cpf}): 1ª rodada — levantamento completo antes do "
                      "download…", "etapa-lev")
            res_lev: dict = {}
            async for ev in sat_client.levantar_cidadao_stream(
                    cpf, nome, str(pasta),
                    delay_s=defaults["tempo_entre"], bloqueio_s=defaults["espera_bloqueio"],
                    delay_max_s=defaults.get("tempo_entre_max")):
                if _FILA["parar"]:
                    parado = True
                if ev.get("done"):
                    res_lev = ev.get("resultado", {}) or {}
                else:
                    _fila_log(ev.get("msg", ""), ev.get("level", "info"))
            if parado:
                return True, "", True, False
            if not res_lev.get("ok"):
                return (False, (f"Falha no levantamento do {papel.lower()} (CPF {cpf}): "
                               f"{res_lev.get('detalhe', '')}"), False, False)
            itens_bat = _montar_batimento_inicial(pasta, tipos_sel, {"nbs": "", "protocolos": ""})
            _salvar_batimento(pasta, itens_bat)
            bat = {"itens": itens_bat}

        if _tem_levantamento_pendente(bat.get("itens") or []):
            # Mesma correção aplicada ao assistido principal, acima: um levantamento
            # bloqueado por antirrobô numa rodada anterior nunca seria refeito sozinho,
            # já que a lista de batimento manda a partir da 2ª rodada.
            _fila_log(f"{papel} (CPF {cpf}): levantamento anterior ficou pendente (bloqueio "
                      "de antirrobô) — tentando de novo…", "etapa-lev")
            res_lev2: dict = {}
            async for ev in sat_client.levantar_cidadao_stream(
                    cpf, nome, str(pasta),
                    delay_s=defaults["tempo_entre"], bloqueio_s=defaults["espera_bloqueio"],
                    delay_max_s=defaults.get("tempo_entre_max")):
                if _FILA["parar"]:
                    parado = True
                if ev.get("done"):
                    res_lev2 = ev.get("resultado", {}) or {}
                else:
                    _fila_log(ev.get("msg", ""), ev.get("level", "info"))
            if parado:
                return True, "", True, False
            if res_lev2.get("ok"):
                if _mesclar_novo_levantamento_em_batimento(pasta, tipos_sel, {"nbs": "", "protocolos": ""}):
                    _fila_log(f"{papel} (CPF {cpf}): levantamento pendente resolvido — lista de "
                              "batimento atualizada.", "ok")
                    bat = ler_batimento(pasta)
                else:
                    _fila_log(f"{papel} (CPF {cpf}): ainda bloqueado — sem novidade desta vez.", "muted")
            else:
                _fila_log(f"{papel} (CPF {cpf}): nova tentativa de levantamento falhou — "
                          "seguindo com a lista de batimento como estava.", "muted")

        multiplos, multiplos_detalhe, multiplos_revisado = _multiplos_requerimentos(pasta)
        if multiplos and multiplos_revisado:
            # Já revisado e salvo pelo menos uma vez — segue com o download normal
            # (mesmo critério do assistido principal, ver acima).
            _fila_log(f"{papel} (CPF {cpf}): Múltiplos Requerimentos revisado e salvo — "
                      f"{multiplos_detalhe} — seguindo com o download normal.", "muted")
        elif multiplos:
            # Mesmo critério do assistido principal — não baixa sozinho um grupo com
            # documentos demais; pausa este CPF extra e segue para o próximo, sem gastar
            # nenhuma consulta no SAT (pedido do usuário, 2026-11-13).
            _fila_log(f"{papel} (CPF {cpf}): Múltiplos Requerimentos — {multiplos_detalhe} — "
                      "revise manualmente em \"Baixar mais\" antes de prosseguir.", "erro")
            continue
        tipos, alvos_bat = _alvos_pendentes_batimento(bat.get("itens") or [])
        if not alvos_bat:
            _fila_log(f"{papel} (CPF {cpf}): nada pendente na lista de batimento.", "muted")
            continue
        _fila_log(f"Baixando os documentos do {papel.lower()} (CPF {cpf}{' · ' + nome if nome else ''})…", "etapa-down")
        res: dict = {}
        try:
            async for ev in sat_client.baixar_cidadao_stream(
                    cpf, nome, str(pasta),
                    delay_s=defaults["tempo_entre"], bloqueio_s=defaults["espera_bloqueio"],
                    delay_max_s=defaults.get("tempo_entre_max"),
                    tipos=tipos, alvos=alvos_bat, pretensao=pretensao, rotulo=rotulo,
                    alvos_revisados=bool(bat.get("revisado")),
                    compilar=defaults["compilar"], compilar_limiar=defaults["compilar_limiar"],
                    pat_ignorar=pat_ignorar):
                if _FILA["parar"]:
                    parado = True
                if ev.get("done"):
                    res = ev.get("resultado", {}) or {}
                else:
                    _fila_log(ev.get("msg", ""), ev.get("level", "info"))
        finally:
            # Mescla o que já foi baixado até aqui mesmo se o laço acima tiver sido
            # interrompido por uma exceção — mesmo motivo do ajuste em
            # `_rodar_fila_automatica` (2026-11-13).
            _mesclar_resultado_em_batimento(pasta)
        if parado:
            return False, f"Parado durante o download do {papel.lower()} (CPF {cpf}).", True, False
        if not res.get("ok"):
            return (False, (f"Falha ao baixar os documentos do {papel.lower()} (CPF {cpf}): "
                           f"{res.get('detalhe', '')} — nada foi anexado nem concluído."),
                    bool(res.get("sessao_expirada")), bool(res.get("sessao_expirada")))
        if tem_pendencia_batimento(pasta):
            return (False, (f"Download incompleto do {papel.lower()} (CPF {cpf}) — nada foi anexado "
                           "e o trâmite não foi concluído. O PAJ fica na caixa para nova tentativa."),
                    False, False)
    return True, "", False, False


async def iniciar_fila_automatica() -> dict:
    """Inicia a fila automática em segundo plano, se ainda não estiver rodando. Recusa se
    a janela do SAT já está em uso por uma ação manual (Baixar arquivos/Levantamento) —
    o painel continua plenamente utilizável de forma manual; as duas nunca disputam a
    MESMA janela ao mesmo tempo. Antes de disparar a fila, confere se a sessão do SAT está
    ativa; se não estiver, tenta reconectar sozinha (login com as credenciais do cofre +
    2FA por TOTP, se cadastrados) — só então inicia. Se mesmo assim não conseguir (sem
    credenciais no cofre, ou precisa de CAPTCHA/2FA manual), recusa com uma mensagem clara
    em vez de deixar a fila falhar PAJ a PAJ."""
    global _FILA_TASK
    if _FILA["rodando"]:
        return {"ok": False, "detalhe": "A fila automática já está rodando."}
    from services import config_service as _cfg
    if not _cfg.get_fila_caixa_origem():
        return {"ok": False, "detalhe": "Escolha, acima, a caixa de grupo que alimenta a fila automática."}
    if not _ULTIMA_SINCRONIZACAO.get("itens"):
        return {"ok": False, "detalhe": "Clique em \"Sincronizar caixa\" antes de iniciar a fila automática."}
    from ingestao import sat_client
    r = sat_client.rotina_ativa()
    if r:
        return {"ok": False, "detalhe": f"A janela do SAT está em uso por uma ação manual "
                                         f"({r.get('nome') or 'outro assistido'}). Tente de novo assim "
                                         f"que ela terminar."}
    d = await sat_client.peek()
    if not d.get("logado"):
        from services import cofre_service
        creds = cofre_service.get_credenciais("sat")
        usuario, senha = creds if creds else ("", "")
        _msg_sat_fechado = ("Não foi possível conectar ao SAT automaticamente — "
                            "abra a janela do SAT (ícone de reconectar ao lado do indicador \"SAT\", no "
                            "topo da página) e finalize o login (usuário/senha/2FA/CAPTCHA, se pedidos). "
                            "Depois, inicie a fila automática novamente.")
        try:
            d = await sat_client.status(usuario=usuario, senha=senha)
        except Exception:  # noqa: BLE001 — ex.: janela do SAT fechada/travada (CDP indisponível) —
            # sem isto, a exceção do Playwright subia crua até virar um 500 sem corpo JSON,
            # e o botão "Iniciar download" parecia não fazer nada (relatado pelo usuário,
            # 2026-11-12).
            return {"ok": False, "detalhe": _msg_sat_fechado}
        if not d.get("logado"):
            return {"ok": False, "detalhe": _msg_sat_fechado}
    _FILA["log"].clear()
    _FILA["processados"] = []
    _FILA_TASK = asyncio.ensure_future(_rodar_fila_automatica())
    return {"ok": True}


def parar_fila_automatica() -> dict:
    """Pede a parada da fila. Também pede o "Encerrar fluxo" do download/levantamento em
    curso (mesma janela do SAT) — parada quase imediata, não só "após o PAJ atual"."""
    if not _FILA["rodando"]:
        return {"ok": False, "detalhe": "A fila automática não está rodando."}
    _FILA["parar"] = True
    from ingestao import sat_client
    sat_client.pedir_encerramento()
    return {"ok": True, "detalhe": "Parando — encerra o PAJ em curso com segurança (mantendo o já baixado)."}
