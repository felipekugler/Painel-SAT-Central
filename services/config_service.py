"""Configurações persistidas do Painel SAT Central — parametrização das
consultas ao SAT e da tramitação padrão.

Armazenadas em painel_sat_config.json na raiz do projeto. Ausência do arquivo
equivale aos defaults de fábrica (comportamento original do PAINEL SISDPU).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_CONFIG_FILE = Path(__file__).parent.parent / "painel_sat_config.json"
_CONFIG_FILE_STR = str(_CONFIG_FILE)


def _ler_raw() -> dict:
    if os.path.isfile(_CONFIG_FILE_STR):
        try:
            return json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {}


def _salvar_raw(data: dict) -> None:
    _CONFIG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# --- Valores padrão da aba "Arquivos SAT Central" ---
# Caixa de tramitação padrão, modo/quantidade de prazo, tempo entre consultas e
# espera após bloqueio anti-robô. Todos assumem o valor de fábrica quando ausentes.
SAT_CAIXA_PADRAO_DEFAULT = "01 - PREV_DIVPREV"
SAT_PRAZO_MODO_DEFAULT = "sem_prazo"  # "sem_prazo" | "dias"
SAT_PRAZO_DIAS_DEFAULT = 15           # dias de prazo quando o modo é "dias"
SAT_TEMPO_ENTRE_DEFAULT = 5           # segundos entre consultas ao SAT (mínimo)
SAT_TEMPO_ENTRE_MAX_DEFAULT = 15      # máximo — cada pausa é sorteada entre mínimo e máximo
SAT_ESPERA_BLOQUEIO_DEFAULT = 45      # segundos de espera ao detectar antirobô
SAT_COMPILAR_DEFAULT = True           # compilar por tipo (CCON/CRER/Laudos) quando > limiar
SAT_COMPILAR_LIMIAR_DEFAULT = 3       # compilar só quando houver MAIS de N docs do tipo
SAT_CAPTCHA_AUTO_DEFAULT = False      # resolver o CAPTCHA da consulta por CPF pelo CapSolver
# Intervalo entre CPFs (cada PAJ da fila automática = 1 CPF): espera sorteada entre
# mínimo e máximo, entre o FIM da rotina de um CPF e a consulta do próximo (novo
# CAPTCHA) — é o que dá "ritmo lento" e margem de segurança contra o antirrobô na
# fila automática da caixa.
SAT_INTERVALO_CPF_DEFAULT = 30        # segundos (mínimo)
SAT_INTERVALO_CPF_MAX_DEFAULT = 60    # segundos (máximo)
# "Múltiplos Requerimentos": acima de quantos documentos PENDENTES (ainda não baixados)
# num MESMO grupo (ex.: CRER, Laudo Médico, Tarefas PAT, Histórico de Empréstimos
# Consignados — qualquer tipo que apareça como lista/tabela, nunca os de documento único
# como CNIS) o PAJ é pausado na fila automática para revisão manual, em vez de baixado
# tudo automaticamente — pedido do usuário, 2026-11-13.
SAT_LIMIAR_MULTIPLOS_DEFAULT = 5


def get_sat_central_defaults() -> dict:
    """Defaults invocados pelos controles da aba Arquivos SAT."""
    raw = _ler_raw()
    caixa = str(raw.get("sat_caixa_padrao") or "").strip() or SAT_CAIXA_PADRAO_DEFAULT
    modo = raw.get("sat_prazo_modo")
    if modo not in ("sem_prazo", "dias"):
        modo = SAT_PRAZO_MODO_DEFAULT
    try:
        dias = int(raw.get("sat_prazo_dias", SAT_PRAZO_DIAS_DEFAULT))
    except Exception:  # noqa: BLE001
        dias = SAT_PRAZO_DIAS_DEFAULT
    try:
        entre = int(raw.get("sat_tempo_entre", SAT_TEMPO_ENTRE_DEFAULT))
    except Exception:  # noqa: BLE001
        entre = SAT_TEMPO_ENTRE_DEFAULT
    entre = min(60, max(0, entre))
    try:
        entre_max = int(raw.get("sat_tempo_entre_max", SAT_TEMPO_ENTRE_MAX_DEFAULT))
    except Exception:  # noqa: BLE001
        entre_max = SAT_TEMPO_ENTRE_MAX_DEFAULT
    try:
        bloqueio = int(raw.get("sat_espera_bloqueio", SAT_ESPERA_BLOQUEIO_DEFAULT))
    except Exception:  # noqa: BLE001
        bloqueio = SAT_ESPERA_BLOQUEIO_DEFAULT
    compilar = raw.get("sat_compilar", SAT_COMPILAR_DEFAULT)
    try:
        limiar = int(raw.get("sat_compilar_limiar", SAT_COMPILAR_LIMIAR_DEFAULT))
    except Exception:  # noqa: BLE001
        limiar = SAT_COMPILAR_LIMIAR_DEFAULT
    captcha_auto = raw.get("sat_captcha_auto", SAT_CAPTCHA_AUTO_DEFAULT)
    try:
        interv = int(raw.get("sat_intervalo_cpf", SAT_INTERVALO_CPF_DEFAULT))
    except Exception:  # noqa: BLE001
        interv = SAT_INTERVALO_CPF_DEFAULT
    try:
        interv_max = int(raw.get("sat_intervalo_cpf_max", SAT_INTERVALO_CPF_MAX_DEFAULT))
    except Exception:  # noqa: BLE001
        interv_max = SAT_INTERVALO_CPF_MAX_DEFAULT
    try:
        limiar_mult = int(raw.get("sat_limiar_multiplos", SAT_LIMIAR_MULTIPLOS_DEFAULT))
    except Exception:  # noqa: BLE001
        limiar_mult = SAT_LIMIAR_MULTIPLOS_DEFAULT
    return {
        "caixa": caixa,
        "prazo_modo": modo,
        "prazo_dias": min(365, max(1, dias)),
        "tempo_entre": entre,
        "tempo_entre_max": min(60, max(entre, entre_max)),
        "espera_bloqueio": min(300, max(0, bloqueio)),
        "compilar": bool(compilar),
        "compilar_limiar": min(50, max(1, limiar)),
        "captcha_auto": bool(captcha_auto),
        "intervalo_cpf": min(600, max(0, interv)),
        "intervalo_cpf_max": min(600, max(min(600, max(0, interv)), interv_max)),
        "limiar_multiplos": min(50, max(1, limiar_mult)),
    }


def set_sat_central_defaults(d: dict) -> dict:
    """Grava apenas as chaves presentes em `d`. Retorna os defaults resultantes."""
    raw = _ler_raw()
    if "caixa" in d:
        raw["sat_caixa_padrao"] = str(d.get("caixa") or "").strip() or SAT_CAIXA_PADRAO_DEFAULT
    if "prazo_modo" in d:
        raw["sat_prazo_modo"] = "dias" if d.get("prazo_modo") == "dias" else "sem_prazo"
    if "prazo_dias" in d:
        try:
            raw["sat_prazo_dias"] = min(365, max(1, int(d.get("prazo_dias"))))
        except Exception:  # noqa: BLE001
            pass
    if "tempo_entre" in d:
        try:
            raw["sat_tempo_entre"] = min(60, max(0, int(d.get("tempo_entre"))))
        except Exception:  # noqa: BLE001
            pass
    if "tempo_entre_max" in d:
        try:
            raw["sat_tempo_entre_max"] = min(60, max(0, int(d.get("tempo_entre_max"))))
        except Exception:  # noqa: BLE001
            pass
    if "espera_bloqueio" in d:
        try:
            raw["sat_espera_bloqueio"] = min(300, max(0, int(d.get("espera_bloqueio"))))
        except Exception:  # noqa: BLE001
            pass
    if "compilar" in d:
        raw["sat_compilar"] = bool(d.get("compilar"))
    if "compilar_limiar" in d:
        try:
            raw["sat_compilar_limiar"] = min(50, max(1, int(d.get("compilar_limiar"))))
        except Exception:  # noqa: BLE001
            pass
    if "captcha_auto" in d:
        raw["sat_captcha_auto"] = bool(d.get("captcha_auto"))
    if "intervalo_cpf" in d:
        try:
            raw["sat_intervalo_cpf"] = min(600, max(0, int(d.get("intervalo_cpf"))))
        except Exception:  # noqa: BLE001
            pass
    if "intervalo_cpf_max" in d:
        try:
            raw["sat_intervalo_cpf_max"] = min(600, max(0, int(d.get("intervalo_cpf_max"))))
        except Exception:  # noqa: BLE001
            pass
    if "limiar_multiplos" in d:
        try:
            raw["sat_limiar_multiplos"] = min(50, max(1, int(d.get("limiar_multiplos"))))
        except Exception:  # noqa: BLE001
            pass
    _salvar_raw(raw)
    return get_sat_central_defaults()


# --- Caixa de GRUPO de ORIGEM da fila automática (ex.: "1 - PREV - SAT") — distinta da
# caixa de tramitação-DESTINO ("sat_caixa_padrao" acima). Mesmo padrão do Painel
# Comunicações: o usuário escolhe, entre as caixas de grupo descobertas no SISDPU, qual
# alimenta a fila automática; `sat_fila_caixas_disponiveis` é o cache da descoberta.
def get_fila_caixa_origem() -> str:
    raw = _ler_raw()
    return str(raw.get("sat_fila_caixa_origem") or "").strip()


def set_fila_caixa_origem(nome: str) -> None:
    raw = _ler_raw()
    raw["sat_fila_caixa_origem"] = str(nome or "").strip()
    _salvar_raw(raw)


def get_fila_caixas_disponiveis() -> list[str]:
    """Caixas de grupo descobertas na última sincronização/descoberta (cache)."""
    raw = _ler_raw()
    v = raw.get("sat_fila_caixas_disponiveis")
    return list(v) if isinstance(v, list) else []


def set_fila_caixas_disponiveis(grupos: list[str]) -> None:
    raw = _ler_raw()
    vistos: list[str] = []
    for g in (grupos or []):
        g = str(g or "").strip()
        if g and g not in vistos:
            vistos.append(g)
    raw["sat_fila_caixas_disponiveis"] = vistos
    _salvar_raw(raw)


# --- Espécies de PROCESSO ADMINISTRATIVO (PAT) a IGNORAR (não baixar) ---
SAT_PAT_IGNORAR_DEFAULT = [
    "Alterar Local ou Forma de Pagamento",
    "Atualizar Cadastro e/ou Benefício",
    "Bloquear/Desbloquear Benefício para Empréstimo Consignado",
]


def get_sat_pat_ignorar() -> list[str]:
    v = _ler_raw().get("sat_pat_ignorar")
    if isinstance(v, list):
        return [str(x) for x in v]
    return list(SAT_PAT_IGNORAR_DEFAULT)  # nunca configurado → sementes


def set_sat_pat_ignorar(itens: list[str]) -> list[str]:
    raw = _ler_raw()
    limpos: list[str] = []
    for x in (itens or []):
        s = str(x).strip()
        if s and s not in limpos:
            limpos.append(s)
    raw["sat_pat_ignorar"] = limpos
    _salvar_raw(raw)
    return limpos


# --- Tipos de documentos do SAT a baixar, POR PRETENSÃO ---
# Guarda `sat_tipos_por_pretensao` = {pretensao: [keys]} e `sat_tipos_padrao` = [keys]
# (aplicado às pretensões sem config). Padrão ausente = TODOS os tipos.
def get_sat_tipos_padrao() -> list[str] | None:
    v = _ler_raw().get("sat_tipos_padrao")
    return [str(x) for x in v] if isinstance(v, list) else None


def get_sat_tipos_por_pretensao() -> dict:
    v = _ler_raw().get("sat_tipos_por_pretensao", {})
    if not isinstance(v, dict):
        return {}
    return {str(k): [str(x) for x in (val or [])] for k, val in v.items()}


def get_sat_tipos_instituidor() -> list[str] | None:
    """Keys do que baixar do INSTITUIDOR (benefícios derivados). None = usar default."""
    v = _ler_raw().get("sat_tipos_instituidor")
    return [str(x) for x in v] if isinstance(v, list) else None


def set_sat_tipos(pretensao: str, keys: list[str]) -> dict:
    """Salva as keys de uma pretensão; '' ou '__padrao__' = PADRÃO; '__instituidor__' =
    seleção do instituidor. Retorna {padrao, por_pretensao, instituidor}."""
    raw = _ler_raw()
    keys = [str(k) for k in (keys or [])]
    pretensao = (pretensao or "").strip()
    if pretensao == "__instituidor__":
        raw["sat_tipos_instituidor"] = keys
    elif pretensao in ("", "__padrao__"):
        raw["sat_tipos_padrao"] = keys
    else:
        d = raw.get("sat_tipos_por_pretensao")
        if not isinstance(d, dict):
            d = {}
        # remove qualquer chave existente que seja a MESMA pretensão só com outra caixa
        # (maiúsculas/minúsculas) — evita acumular duplicatas (pedido do usuário,
        # 2026-11-17, mesmo caso do bug de `resolver_sat_tipos`).
        alvo = _norm_pretensao(pretensao)
        for k in [k for k in d if k != pretensao and _norm_pretensao(k) == alvo]:
            del d[k]
        d[pretensao] = keys
        raw["sat_tipos_por_pretensao"] = d
    _salvar_raw(raw)
    return {"padrao": raw.get("sat_tipos_padrao"),
            "por_pretensao": raw.get("sat_tipos_por_pretensao", {}),
            "instituidor": raw.get("sat_tipos_instituidor")}


def _norm_pretensao(s: str) -> str:
    """Normaliza para casar pretensões independente de caixa — o SISDPU nem sempre
    devolve a pretensão em MAIÚSCULAS (ex.: 'Aposentadoria por idade' em vez de
    'APOSENTADORIA POR IDADE'), e a comparação era sensível a caixa — bug identificado
    2026-11-17: PAJ com a pretensão em outra caixa caía no padrão (sem PAT) mesmo com
    associação cadastrada para aquela pretensão."""
    return (s or "").strip().upper()


def resolver_sat_tipos(pretensao: str, todos: list[str]) -> list[str]:
    """Keys a baixar p/ a pretensão: específica > padrão > todos. Lista vazia
    configurada (usuário desmarcou tudo) é respeitada. Casamento da pretensão é
    insensível a maiúsculas/minúsculas (ver `_norm_pretensao`)."""
    pretensao_norm = _norm_pretensao(pretensao)
    por = get_sat_tipos_por_pretensao()
    if pretensao_norm:
        for k, v in por.items():
            if _norm_pretensao(k) == pretensao_norm:
                return v
    pad = get_sat_tipos_padrao()
    return pad if pad is not None else list(todos)


# --- Catálogo de espécies de Tarefas PAT (2ª Via dos Processos) ---
# Lista "retro alimentável": cresce sozinha a cada consulta ao SAT que revele uma
# espécie (campo "Serviço" das tarefas do PAT) ainda não catalogada — ver
# `registrar_sat_pat_servico`, chamada pelo ingestao.sat_client (_pat_stream e o PAT do
# levantamento) a cada tarefa encontrada, baixada ou não. Semente = espécies já
# observadas nas consultas feitas até 10/11/2026 (PAINEL SISDPU 2.0 + Painel SAT
# Central) + as informadas pelo Defensor — mas é só ponto de partida: o catálogo real
# é o gravado em `sat_pat_catalogo` assim que a 1ª espécie nova aparecer.
SAT_PAT_CATALOGO_SEED = [
    "Acertos para análise", "Acertos para Análise - BI Urbano",
    "Acertos para Marcação de Perícia Médica", "Acertos pós-pericia SIBE - Urbano",
    "Acréscimo de 25%", "Aguardando Migração de Informações - CadÚnico", "Alta a Pedido",
    "Alterar Local ou Forma de Pagamento", "Análise de Acórdão",
    "Análise de Descontos de Entidades Associativas",
    "Aposentadoria da Pessoa com Deficiência por Idade",
    "Aposentadoria da Pessoa com Deficiência por Tempo de Contribuição",
    "Aposentadoria por Idade Rural", "Aposentadoria por Idade Urbana",
    "Aposentadoria por Tempo de Contribuição", "Atualizar Cadastro e/ou Benefício",
    "Atualizar Dados Cadastrais", "Atualizar Dados do Imposto de Renda",
    "Atualizar Procurador e Representante Legal",
    "Autorizacao de Pagamento - Cadastro e encaminhamento no APWeb",
    "Auxílio por incapacidade temporária - Análise Documental - AIT", "Auxílio-Acidente",
    "Auxílio-Doença - Urbano (Acerto Pós-perícia)", "Auxílio-Reclusão Rural",
    "Auxílio-Reclusão Urbano", "Avaliação Social BPC/LOAS - Inicial (presencial)",
    "Avaliação Social BPC/LOAS - Inicial (remota)",
    "Avaliação Social BPC/LOAS - Reavaliação (presencial)", "Benefício Assistencial ao Idoso",
    "Benefício Assistencial à Pessoa com Deficiência", "Benefício por Incapacidade",
    "Bloquear/Desbloquear Benefício para Empréstimo Consignado",
    "Bloqueio/desbloqueio de Mensalidade de Entidade Associativa ou Sindicato",
    "Cadastrar Consignação em Benefício por Ordem Judicial",
    "Cadastrar ou Atualizar Dependentes para Salário-Família",
    "Cadastrar/Alterar/Excluir Pensão Alimentícia",
    "Certidão de Inexistência de Dependentes Habilitados à Pensão por Morte",
    "Certidão de Tempo de Contribuição",
    "Contrarrazões do Recurso Especial/Incidente - INSS",
    "Cumprimento de Acórdão com Implantação de Benefício",
    "Cumprimento de Acórdão sem Implantação de Benefício", "Cópia de Processo",
    "Cópia de Processo - Entidade Conveniada",
    "Excluir Mensalidade de Associação ou Sindicato no Benefício",
    "Instrução de Processo de Recurso", "Pagamento de Benefício Não Recebido",
    "Pagamento de Valor não Recebido até a Data do Óbito do Beneficiário",
    "Parcela Única e Pensão Especial - Síndrome Congênita do Zika Vírus",
    "Parecer da Área Técnica - Sistemas SABI", "Parecer de Área Técnica - RD",
    "Pedido de Prorrogação de Benefício por Incapacidade",
    "Pensão Especial - Dependentes de Vítimas do Feminicídio",
    "Pensão Especial - Síndrome da Talidomida", "Pensão por Morte Rural",
    "Pensão por Morte Urbana", "Reavaliações de deficiência",
    "Recurso Especial ou Incidente (Alteração de Acórdão)", "Recurso Ordinário (Inicial)",
    "Renovar Declaração de Cárcere/Reclusão", "Revisão - Entidade Conveniada",
    "Revisão de Certidão de Tempo de Contribuição", "Salário-Maternidade Rural",
    "Salário-Maternidade Urbano", "Solicitar Contestação de NTEP",
    "Solicitar Desistência/Encerramento/Renúncia de Benefício",
    "Solicitar Emissão de Pagamento não Recebido",
    "Solicitar Ressarcimento de Despesas com Deslocamento para Avaliações Social e/ou "
    "Médico – BPC",
    "Solicitação de Correção de Erro Formal em Tarefa", "Suporte Técnico - AIS",
    "Suporte Técnico - RD",
    "Suspender o Benefício Assistencial à Pessoa com Deficiência para Inclusão no "
    "Mercado de Trabalho",
]


def _norm_pat(s: str) -> str:
    """Normaliza (sem acento, minúsculo) p/ comparar espécies de PAT — mesmo critério
    já usado pela lista "a ignorar" (casa por trecho lá; aqui é igualdade exata)."""
    import unicodedata
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).strip().lower()


def get_sat_pat_catalogo() -> list[str]:
    """Catálogo COMPLETO de espécies de Tarefas PAT já conhecidas (semente + as que
    foram sendo reveladas em consultas reais), em ordem alfabética."""
    v = _ler_raw().get("sat_pat_catalogo")
    base = [str(x) for x in v] if isinstance(v, list) and v else list(SAT_PAT_CATALOGO_SEED)
    vistos: dict[str, str] = {}
    for s in base:
        vistos.setdefault(_norm_pat(s), s)
    return sorted(vistos.values(), key=lambda s: s.lower())


def registrar_sat_pat_servico(servico: str) -> bool:
    """Acrescenta ao catálogo se for uma espécie NOVA (comparação sem acento/maiúsculas).
    True se acrescentou. Chamada a cada tarefa PAT revelada numa consulta ou levantamento
    (baixada, ignorada ou não) — é isso que torna o catálogo "retro alimentável"."""
    s = (servico or "").strip()
    if not s:
        return False
    raw = _ler_raw()
    atual = raw.get("sat_pat_catalogo")
    atual = [str(x) for x in atual] if isinstance(atual, list) and atual else list(SAT_PAT_CATALOGO_SEED)
    if any(_norm_pat(x) == _norm_pat(s) for x in atual):
        return False
    atual.append(s)
    raw["sat_pat_catalogo"] = atual
    _salvar_raw(raw)
    return True


def get_sat_pat_filtrada() -> list[str]:
    """Catálogo completo MENOS as espécies "a ignorar" (substring normalizada, mesmo
    critério do download) — é a lista oferecida para associar manualmente as tarefas
    PAT de cada pretensão em Configurações."""
    ignoradas = [_norm_pat(x) for x in get_sat_pat_ignorar() if str(x).strip()]
    return [s for s in get_sat_pat_catalogo()
            if not any(i in _norm_pat(s) for i in ignoradas)]


def get_sat_pat_por_pretensao() -> dict:
    """{pretensao: [espécies selecionadas]} — associação manual feita em Configurações
    ("Tarefas PAT (2ª Via dos Processos)" dentro de Tipos de documentos a baixar)."""
    v = _ler_raw().get("sat_pat_por_pretensao", {})
    if not isinstance(v, dict):
        return {}
    return {str(k): [str(x) for x in (val or [])] for k, val in v.items()}


def set_sat_pat_por_pretensao(pretensao: str, servicos: list[str]) -> dict:
    """Salva a seleção de espécies de PAT para 1 pretensão. Lista vazia = sem associação
    própria (some do dict) — nesse caso baixa TODAS as espécies não ignoradas (ver
    `resolver_sat_pat_filtro`), igual ao comportamento de hoje."""
    raw = _ler_raw()
    d = raw.get("sat_pat_por_pretensao")
    if not isinstance(d, dict):
        d = {}
    pretensao = (pretensao or "").strip()
    servicos = [str(s).strip() for s in (servicos or []) if str(s).strip()]
    if pretensao:
        # remove qualquer chave existente que seja a MESMA pretensão só com outra caixa —
        # evita acumular duplicatas (pedido do usuário, 2026-11-17).
        alvo = _norm_pretensao(pretensao)
        for k in [k for k in d if k != pretensao and _norm_pretensao(k) == alvo]:
            del d[k]
        if servicos:
            d[pretensao] = servicos
        else:
            d.pop(pretensao, None)
    raw["sat_pat_por_pretensao"] = d
    _salvar_raw(raw)
    return d


def resolver_sat_pat_filtro(pretensao: str) -> list[str] | None:
    """Espécies de PAT a baixar p/ a pretensão informada. None = sem associação própria
    configurada — baixa TODAS as espécies não ignoradas (comportamento de hoje,
    inalterado enquanto o Defensor não associar nada para essa pretensão). Casamento da
    pretensão é insensível a maiúsculas/minúsculas (ver `_norm_pretensao`)."""
    pretensao_norm = _norm_pretensao(pretensao)
    por = get_sat_pat_por_pretensao()
    if pretensao_norm:
        for k, v in por.items():
            if _norm_pretensao(k) == pretensao_norm and v:
                return v
    return None
