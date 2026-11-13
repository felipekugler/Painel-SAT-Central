"""Rotas do Painel SAT Central — feature "Arquivos SAT".

- GET  /                             — página (tabela de PAJs manuais + histórico).
- GET  /configuracoes                — parametrização das consultas e da tramitação.
- POST /api/sat/pendentes/manual     — adiciona 1+ PAJs à lista (busca global no SISDPU).
- POST /api/sat/pendentes/manual/del — remove um PAJ manual da lista.
- GET  /api/sat/pendentes/manuais    — lista os PAJs cadastrados (única fonte —
                                       este painel não varre a caixa de entrada).
- GET  /api/sat/{paj_norm}/processar — SSE: movimenta (assistido+instituidor juntos)
                                       → tramita (query `grupo`, `prazo`, `dry`). Sem
                                       conclusão — o PAJ é manual, não há trâmite
                                       originário na caixa.
- GET  /api/sat/{paj_norm}/anexar-mais — SSE: anexação complementar (nova movimentação
                                       só de juntada) num PAJ já processado.
- GET  /api/sat/historico            — registros dos PAJs já trabalhados.
- GET  /api/sat/grupos               — caixas de grupo da unidade.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from sse_starlette.sse import EventSourceResponse
from starlette.background import BackgroundTask

from ingestao import sisdpu_movtram as movtram
from services import cofre_service
from services import config_service as _cfg
from services import historico_sat, sat_service

router = APIRouter()


def _credenciais_sat() -> tuple[str, str]:
    """(usuario, senha) do GERID do cofre unificado (sistema "sat"); ("","") se o
    cofre estiver trancado ou sem credencial cadastrada — login fica manual."""
    creds = cofre_service.get_credenciais("sat")
    return creds if creds else ("", "")

BASE_DIR = Path(__file__).resolve().parent.parent
PID_FILE = BASE_DIR / ".server.pid"


@router.get("/", response_class=HTMLResponse)
async def pagina_sat(request: Request):
    template = request.app.state.jinja.get_template("index.html")
    return HTMLResponse(template.render(request=request))


@router.get("/configuracoes", response_class=HTMLResponse)
async def pagina_config(request: Request):
    template = request.app.state.jinja.get_template("configuracoes.html")
    return HTMLResponse(template.render(request=request))


@router.get("/api/sat/pendentes/manuais", response_class=JSONResponse)
async def api_pendentes_manuais():
    """Só os PAJs cadastrados manualmente (sem varredura de caixa) — pré-carga rápida."""
    return JSONResponse(sat_service.listar_pendentes_manuais())


@router.get("/api/sat/pendentes", response_class=JSONResponse)
async def api_pendentes():
    """TODOS os PAJs da caixa de grupo configurada (Configurações › Parâmetros) + PAJs
    cadastrados manualmente que ainda não vieram de lá. Fonte da fila automática."""
    return JSONResponse(await sat_service.listar_pendentes())


@router.post("/api/sat/pendentes/manual", response_class=JSONResponse)
async def api_add_manual(request: Request):
    """Adiciona 1+ PAJs à lista, extraindo os dados AO VIVO do SISDPU.
    Body: {paj: "AAAA/UUU-NNNNN"} ou {pajs: [...]}. Retorna {ok, itens, erros}."""
    body = await request.json()
    entradas = body.get("pajs") or ([body.get("paj")] if body.get("paj") else [])
    itens, erros = [], []
    for txt in entradas:
        r = await sat_service.add_paj_manual(str(txt))
        if r.get("ok"):
            itens.append(r["item"])
        else:
            erros.append({"paj": str(txt), "erro": r.get("erro", "falha")})
    return JSONResponse({"ok": not erros, "itens": itens, "erros": erros})


@router.get("/api/sat/{paj_norm}/cpfs-extras", response_class=JSONResponse)
async def api_cpfs_extras_get(paj_norm: str):
    """CPFs extras do PAJ (instituidor / outros assistidos) para a fila automática."""
    return JSONResponse({"ok": True, "itens": sat_service.listar_cpfs_extras(paj_norm),
                         "papeis": list(sat_service.PAPEIS_EXTRAS)})


@router.post("/api/sat/{paj_norm}/cpfs-extras", response_class=JSONResponse)
async def api_cpfs_extras_set(paj_norm: str, request: Request):
    """Grava a lista de CPFs extras do PAJ. Body: {itens: [{cpf, nome, papel}]}."""
    body = await request.json()
    r = sat_service.salvar_cpfs_extras(paj_norm, body.get("itens") or [])
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@router.post("/api/sat/{paj_norm}/filtros", response_class=JSONResponse)
async def api_filtros_set(paj_norm: str, request: Request):
    """Grava os filtros de download do PAJ (fila automática). Body: {nbs, protocolos}."""
    body = await request.json()
    return JSONResponse(sat_service.salvar_filtros(
        paj_norm, str(body.get("nbs", "") or ""), str(body.get("protocolos", "") or "")))


@router.post("/api/sat/pendentes/manual/del", response_class=JSONResponse)
async def api_del_manual(request: Request):
    """Remove um PAJ manual da lista. Body: {paj_norm}."""
    body = await request.json()
    return JSONResponse(sat_service.del_paj_manual(body.get("paj_norm", "")))


@router.post("/api/sat/pendentes/saiu-da-caixa/del", response_class=JSONResponse)
async def api_del_saiu_da_caixa(request: Request):
    """Remove da lista da Fila Automática um PAJ marcado "saiu da caixa" e apaga os
    arquivos já baixados dele. Body: {paj_norm}. Ação irreversível — o front-end confirma
    com o usuário antes de chamar."""
    body = await request.json()
    return JSONResponse(sat_service.excluir_saiu_da_caixa(body.get("paj_norm", "")))


@router.post("/api/sat/pendentes/selecao", response_class=JSONResponse)
async def api_pendentes_selecao(request: Request):
    """Marca/desmarca um PAJ para entrar (ou não) na PRÓXIMA rodada da fila automática
    (checkbox da linha). Body: {paj_norm, selecionado}."""
    body = await request.json()
    return JSONResponse(sat_service.definir_selecao_fila(
        body.get("paj_norm", ""), bool(body.get("selecionado", True))))


@router.post("/api/sat/pendentes/selecao-todos", response_class=JSONResponse)
async def api_pendentes_selecao_todos(request: Request):
    """Marca/desmarca TODOS os PAJs de uma vez (checkbox do cabeçalho). Body: {selecionado}."""
    body = await request.json()
    return JSONResponse(sat_service.definir_selecao_fila_todos(bool(body.get("selecionado", True))))


@router.get("/api/sat/abrir", response_class=JSONResponse)
async def api_sat_abrir():
    """Abre a janela do SAT e tenta logar sozinho: usuário/senha do cofre
    (sistema "sat") + 2FA (seed TOTP). Se o cofre estiver trancado/sem
    credencial, login continua manual na janela; CAPTCHA é sempre manual."""
    from ingestao import sat_client
    usuario, senha = _credenciais_sat()
    return JSONResponse(await sat_client.abrir(usuario=usuario, senha=senha))


@router.get("/api/sat/sessao", response_class=JSONResponse)
async def api_sat_sessao():
    """Verifica se a sessão do SAT está pronta (logado, na página de consulta).
    Também tenta logar sozinho (ver `/api/sat/abrir`)."""
    from ingestao import sat_client
    usuario, senha = _credenciais_sat()
    return JSONResponse(await sat_client.status(usuario=usuario, senha=senha))


@router.get("/api/sat/sessao-leve", response_class=JSONResponse)
async def api_sat_sessao_leve():
    """Status LEVE p/ polling automático: só lê a URL (não navega, não abre a janela)."""
    from ingestao import sat_client
    return JSONResponse(await sat_client.peek())


@router.get("/api/sat/historico", response_class=JSONResponse)
async def api_historico():
    regs = historico_sat.listar()
    # URL do detalhamento do PAJ no SISDPU (link do nº do PAJ), a partir do id_processo.
    for r in regs:
        if not r.get("url_detalhe") and r.get("id_processo"):
            r["url_detalhe"] = (f"{movtram.SISDPU_URL}/pages/atendimento/"
                                f"detalhamentoProcesso.xhtml?id={r['id_processo']}")
    return JSONResponse({"registros": regs})


@router.post("/api/sat/{paj_norm}/apagar-arquivos-sat", response_class=JSONResponse)
async def apagar_arquivos_sat(paj_norm: str):
    """Remove a pasta 'Arquivos SAT' de um PAJ já concluído (libera espaço em disco) E
    remove o(s) registro(s) desse PAJ do histórico — a linha some de "PAJs Concluídos".
    NÃO desfaz a anexação já feita no SISDPU."""
    return JSONResponse(sat_service.apagar_arquivos_sat_paj(paj_norm))


# ---------------------------------------------------------------------------
# Caixa de GRUPO de origem da fila automática (ex.: "1 - PREV - SAT") — distinta da
# caixa de tramitação-destino. O usuário escolhe, entre as caixas de grupo descobertas
# no SISDPU, qual alimenta a fila (mesmo padrão do Painel Comunicações).
# ---------------------------------------------------------------------------
@router.get("/api/sat/caixas", response_class=JSONResponse)
async def api_caixas_get():
    """Caixa de grupo atualmente escolhida para a fila automática + as descobertas
    numa sincronização/descoberta anterior (cache — atualize com o botão "Atualizar
    lista")."""
    return JSONResponse({
        "atual": _cfg.get_fila_caixa_origem(),
        "disponiveis": _cfg.get_fila_caixas_disponiveis(),
    })


@router.post("/api/sat/caixas/atualizar", response_class=JSONResponse)
async def api_caixas_atualizar():
    """Relista TODAS as caixas de grupo EXISTENTES na unidade — escolha "em abstrato":
    lê a tela de tramitação em modo bloco (`listar_grupos_unidade`, a mesma fonte da
    "Caixa de tramitação"), que traz toda caixa da unidade, mesmo as vazias. NÃO usa a
    aba "Caixa de Entrada do Grupo" (essa só lista caixas com PAJ pendente agora —
    "em concreto"; essa leitura "em concreto" é feita depois, ao Sincronizar caixa)."""
    from ingestao import sisdpu_movtram as movtram
    grupos = await movtram.listar_grupos_unidade()
    if not grupos:
        return JSONResponse({"ok": False, "detalhe": "Nenhuma caixa retornada (verifique a sessão do SISDPU)."},
                             status_code=502)
    _cfg.set_fila_caixas_disponiveis(grupos)
    return JSONResponse({"ok": True, "disponiveis": grupos})


@router.post("/api/sat/caixas/escolher", response_class=JSONResponse)
async def api_caixas_escolher(request: Request):
    """Salva a caixa de grupo escolhida para alimentar a fila automática. Body: {grupo}."""
    body = await request.json()
    grupo = str(body.get("grupo", "") or "").strip()
    _cfg.set_fila_caixa_origem(grupo)
    return JSONResponse({"ok": True, "atual": grupo})


# ---------------------------------------------------------------------------
# Fila automática: varre a caixa e processa SÓ o download (SAT Central) dos PAJs
# pendentes, um a um, em segundo plano, com o ritmo configurado em Configurações
# (CAPTCHA automático pelo CapSolver + intervalo entre CPFs). NÃO movimenta/tramita/
# conclui no SISDPU — isso é feito manualmente, por PAJ, via "Anexar ao PAJ".
# ---------------------------------------------------------------------------
@router.post("/api/sat/fila/iniciar", response_class=JSONResponse)
async def api_fila_iniciar():
    return JSONResponse(await sat_service.iniciar_fila_automatica())


@router.post("/api/sat/fila/parar", response_class=JSONResponse)
async def api_fila_parar():
    return JSONResponse(sat_service.parar_fila_automatica())


@router.get("/api/sat/fila/estado", response_class=JSONResponse)
async def api_fila_estado():
    """Estado da fila automática — a tela consulta por polling (não é SSE)."""
    return JSONResponse(sat_service.estado_fila_automatica())


async def _stream_processar(paj_norm: str, grupo: str, dry: bool, prazo: int, manual: bool = True):
    async for ev in sat_service.processar_paj_stream(
        paj_norm, grupo=grupo or None, prazo_dias=prazo, dry_run=dry, manual=manual
    ):
        if ev.get("done"):
            yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
        else:
            yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}


def _parse_nbs(nbs: str) -> list[str]:
    """Divide a lista de NBs informada pelo usuário (separada por vírgula/espaço/;/quebra
    de linha) numa lista de strings; a normalização p/ dígitos é feita no sat_client."""
    import re as _re
    return [t for t in _re.split(r"[\s,;]+", (nbs or "").strip()) if t]


def _fila_ocupando_janela() -> str:
    """'' se a janela do SAT está livre para uma ação manual; senão, mensagem de recusa.
    A fila automática da caixa e uma ação manual (Baixar arquivos/Levantamento) nunca
    disputam a MESMA janela do SAT ao mesmo tempo — o painel continua plenamente
    utilizável de forma manual: pare a fila (ela encerra em segundos) e tente de novo."""
    if sat_service.estado_fila_automatica().get("rodando"):
        return ("A fila automática da caixa está em execução e usando a janela do SAT. "
                "Clique em \"Parar\" na fila e tente de novo em alguns segundos.")
    return ""


async def _stream_baixar(paj_norm: str, cpf: str, nome: str, delay: float,
                         bloqueio: float, pretensao: str, nbs: str = "", protocolos: str = "",
                         somente_pat: bool = False, delay_max: float | None = None):
    from ingestao import sat_client
    recusa = _fila_ocupando_janela()
    if recusa:
        yield {"event": "done", "data": json.dumps({"ok": False, "detalhe": recusa}, ensure_ascii=False)}
        return
    pasta = str(sat_service.pasta_arquivos_sat(paj_norm, cpf, nome))
    if somente_pat:
        # Pedido explícito de baixar SÓ o(s) processo(s) administrativo(s) (PAT) — pula
        # Grupo 1 e Grupo 2 por completo, ignorando a config de tipos por pretensão.
        tipos = ["pat"]
    else:
        # resolve os tipos de documentos a baixar pela PRETENSÃO do PAJ (config por pretensão);
        # sem config → todos os tipos (comportamento padrão).
        try:
            tipos = _cfg.resolver_sat_tipos(pretensao, sat_client.TIPOS_KEYS)
        except Exception:  # noqa: BLE001
            tipos = None
    defaults = _cfg.get_sat_central_defaults()
    compilar, compilar_limiar = defaults["compilar"], defaults["compilar_limiar"]
    pat_ignorar = _cfg.get_sat_pat_ignorar()
    pat_filtro = _cfg.resolver_sat_pat_filtro(pretensao)
    try:
        async for ev in sat_client.baixar_cidadao_stream(
                cpf, nome, pasta, delay_s=delay, bloqueio_s=bloqueio, delay_max_s=delay_max,
                tipos=tipos, pretensao=pretensao, nbs=_parse_nbs(nbs),
                protocolos=_parse_nbs(protocolos),
                compilar=compilar, compilar_limiar=compilar_limiar,
                pat_ignorar=pat_ignorar, pat_filtro=pat_filtro):
            if ev.get("done"):
                yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
            else:
                yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}
    except Exception as e:  # noqa: BLE001
        yield {"event": "log", "data": json.dumps({"level": "erro", "msg": str(e)}, ensure_ascii=False)}
        yield {"event": "done", "data": json.dumps({"ok": False, "detalhe": str(e)}, ensure_ascii=False)}


@router.get("/api/sat/{paj_norm}/arquivos", response_class=JSONResponse)
async def api_arquivos(paj_norm: str):
    arqs = sat_service.listar_todos_arquivos_sat(paj_norm)
    return JSONResponse({"arquivos": [
        {"nome": a["nome"], "tamanho": a.get("tamanho", 0), "excluido": a.get("excluido", False),
         "escopo": a.get("escopo", "assistido"), "referencia": a.get("referencia", "")}
        for a in arqs]})


@router.get("/api/sat/{paj_norm}/arquivo")
async def abrir_arquivo(paj_norm: str, nome: str):
    """Serve um PDF do SAT (ativo ou na lixeira 'Excluídos') INLINE, para abrir em nova
    aba do navegador. `nome` é o nome do arquivo (basename; sem travessia de caminho)."""
    base = Path(nome).name  # anti path-traversal
    # procura em todas as pastas de pessoa (assistido, outros, instituidor, terceiro) + Excluídos
    pastas = []
    for d in sat_service.pastas_pessoas(paj_norm):
        pastas += [d, d / "Excluídos"]
    for pasta in pastas:
        p = pasta / base
        if p.exists() and p.is_file():
            return FileResponse(
                str(p), media_type="application/pdf",
                headers={"Content-Disposition": f'inline; filename="{base}"'})
    return JSONResponse({"erro": "arquivo não encontrado"}, status_code=404)


@router.post("/api/sat/{paj_norm}/excluir-arquivo", response_class=JSONResponse)
async def api_excluir_arquivo(paj_norm: str, request: Request):
    """Move o documento para 'Excluídos' (soft delete). `escopo` = assistido|instituidor."""
    body = await request.json()
    return JSONResponse(sat_service.excluir_arquivo_sat(
        paj_norm, body.get("nome", ""), body.get("escopo", "assistido")))


@router.post("/api/sat/{paj_norm}/recuperar-arquivo", response_class=JSONResponse)
async def api_recuperar_arquivo(paj_norm: str, request: Request):
    """Traz o documento de volta de 'Excluídos' para a lista. `escopo` = assistido|instituidor."""
    body = await request.json()
    return JSONResponse(sat_service.recuperar_arquivo_sat(
        paj_norm, body.get("nome", ""), body.get("escopo", "assistido")))


@router.get("/api/sat/{paj_norm}/narrativa", response_class=JSONResponse)
async def api_narrativa(paj_norm: str):
    """Narrativa do PAJ (texto) para a linha expansível da coluna Pretensão."""
    return JSONResponse(sat_service.narrativa_paj(paj_norm))


@router.get("/api/sat/{paj_norm}/inventario", response_class=JSONResponse)
async def api_inventario(paj_norm: str):
    """Inventário dos documentos revelados no SAT (1º download) + marcas ja_baixado/
    ja_anexado + CPF — para o modal "Baixar mais". Inclui o catálogo p/ rótulos/grupos."""
    from ingestao import sat_client
    inv = sat_service.inventario_paj(paj_norm)
    inv["tipos"] = sat_client.TIPOS_DOCUMENTOS
    # havendo instituidor, oferece também o inventário dele (mesmo catálogo de tipos) para
    # que o modal "Baixar mais" permita baixar documentos complementares do instituidor.
    if sat_service.tem_instituidor(paj_norm):
        inv_i = sat_service.inventario_paj(paj_norm, escopo="instituidor")
        inv["instituidor"] = {"itens": inv_i.get("itens", []), "cpf": inv_i.get("cpf", "")}
    return JSONResponse(inv)


@router.get("/api/sat/{paj_norm}/log-download", response_class=JSONResponse)
async def log_download(paj_norm: str):
    """Log da última rotina de download (Baixar arquivos do SAT Central) do PAJ."""
    p = sat_service.pasta_arquivos_sat(paj_norm) / "_log_download.json"
    if not p.exists():
        return JSONResponse({"existe": False, "eventos": []})
    try:
        return JSONResponse({"existe": True, **json.loads(p.read_text(encoding="utf-8"))})
    except Exception:  # noqa: BLE001
        return JSONResponse({"existe": False, "eventos": []})


@router.get("/api/sat/{paj_norm}/baixar")
async def baixar(paj_norm: str, cpf: str, nome: str = "", delay: float = 5.0,
                 bloqueio: float = 45.0, pretensao: str = "", nbs: str = "", protocolos: str = "",
                 somente_pat: int = 0, delay_max: float | None = None):
    """SSE do DOWNLOAD dos documentos do SAT (consulta + Grupos 1 e 2 + PAT). O CAPTCHA é
    resolvido automaticamente pelo CapSolver (se ligado em Configurações) ou pelo usuário
    na janela do SAT. Salva na pasta do PAJ. `delay` = ritmo mínimo entre consultas;
    `delay_max` = máximo (cada pausa sorteada entre os dois; ausente = intervalo fixo).
    `nbs` = filtro por NB (Grupo 2 + PAT); `protocolos` = filtro por protocolo (só PAT);
    `somente_pat` = 1 pula Grupo 1 e Grupo 2 por completo (baixa só o PAT) — vale para
    QUALQUER consulta, com ou sem protocolo indicado."""
    return EventSourceResponse(
        _stream_baixar(paj_norm, cpf, nome, delay, bloqueio, pretensao, nbs, protocolos,
                        bool(somente_pat), delay_max))


@router.post("/api/sat/encerrar-fluxo", response_class=JSONResponse)
async def encerrar_fluxo():
    """Encerra a rotina do SAT em curso (download/levantamento, manual OU da fila
    automática) mantendo o que já foi baixado."""
    from ingestao import sat_client
    return JSONResponse({"ok": sat_client.pedir_encerramento()})


async def _stream_levantar(paj_norm: str, cpf: str, nome: str, delay: float,
                          bloqueio: float, delay_max: float | None = None):
    from ingestao import sat_client
    recusa = _fila_ocupando_janela()
    if recusa:
        yield {"event": "done", "data": json.dumps({"ok": False, "detalhe": recusa}, ensure_ascii=False)}
        return
    pasta = str(sat_service.pasta_arquivos_sat(paj_norm, cpf, nome))
    try:
        async for ev in sat_client.levantar_cidadao_stream(cpf, nome, pasta, delay_s=delay,
                                                           bloqueio_s=bloqueio, delay_max_s=delay_max):
            if ev.get("done"):
                yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
            else:
                yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}
    except Exception as e:  # noqa: BLE001
        yield {"event": "log", "data": json.dumps({"level": "erro", "msg": str(e)}, ensure_ascii=False)}
        yield {"event": "done", "data": json.dumps({"ok": False, "detalhe": str(e)}, ensure_ascii=False)}


@router.get("/api/sat/{paj_norm}/levantar")
async def levantar(paj_norm: str, cpf: str, nome: str = "", delay: float = 5.0,
                   bloqueio: float = 45.0, delay_max: float | None = None, substituir: int = 0):
    """SSE do LEVANTAMENTO: lista o que existe no SAT para o CPF, SEM baixar (CAPTCHA
    automático pelo CapSolver se ligado, senão o usuário resolve na janela). Grava
    `_levantamento.json` na pasta do PAJ; o "Baixar mais" passa a oferecer tudo o que foi
    encontrado. Já havendo levantamento completo, só roda com `substituir=1` (confirmado
    pelo usuário)."""
    ja = sat_service.estado_download(paj_norm).get("levantamento_em")
    if ja and not substituir:
        async def _recusa():
            yield {"event": "done", "data": json.dumps(
                {"ok": False, "detalhe": f"Já existe levantamento completo deste PAJ ({ja}) — "
                                         "confirme para fazer outro."}, ensure_ascii=False)}
        return EventSourceResponse(_recusa())
    return EventSourceResponse(_stream_levantar(paj_norm, cpf, nome, delay, bloqueio, delay_max))


async def _stream_baixar_instituidor(paj_norm: str, cpf: str, nome: str,
                                     delay: float, bloqueio: float, delay_max: float | None = None):
    from ingestao import sat_client
    # o instituidor baixado aqui também passa a constar nos CPFs adicionais do PAJ (mesma
    # pasta "Instituidor <CPF> - <NOME>" usada pela fila automática)
    extras = sat_service.listar_cpfs_extras(paj_norm)
    if not any(sat_service._so_digitos(x.get("cpf", "")) == sat_service._so_digitos(cpf) for x in extras):
        sat_service.salvar_cpfs_extras(paj_norm, extras + [{"cpf": cpf, "nome": nome, "papel": "Instituidor"}])
    pasta = str(sat_service.pasta_instituidor(paj_norm, cpf, nome))
    tipos = _cfg.get_sat_tipos_instituidor()
    if tipos is None:
        tipos = list(sat_client.TIPOS_INSTITUIDOR_DEFAULT)
    defaults = _cfg.get_sat_central_defaults()
    try:
        async for ev in sat_client.baixar_cidadao_stream(
                cpf, nome, pasta, delay_s=delay, bloqueio_s=bloqueio, delay_max_s=delay_max,
                tipos=tipos, pretensao="", rotulo="INSTITUIDOR - ",
                compilar=defaults["compilar"], compilar_limiar=defaults["compilar_limiar"]):
            if ev.get("done"):
                yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
            else:
                yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}
    except Exception as e:  # noqa: BLE001
        yield {"event": "log", "data": json.dumps({"level": "erro", "msg": str(e)}, ensure_ascii=False)}
        yield {"event": "done", "data": json.dumps({"ok": False, "detalhe": str(e)}, ensure_ascii=False)}


@router.get("/api/sat/{paj_norm}/baixar-instituidor")
async def baixar_instituidor(paj_norm: str, cpf: str, nome: str = "", delay: float = 5.0,
                             bloqueio: float = 45.0, delay_max: float | None = None):
    """SSE do DOWNLOAD dos documentos do INSTITUIDOR (2ª consulta, CPF do instituidor;
    novo CAPTCHA na janela). Usado em pretensões derivadas (pensão por morte / auxílio-
    reclusão). Salva na MESMA pasta do PAJ com prefixo 'INSTITUIDOR - '."""
    return EventSourceResponse(_stream_baixar_instituidor(paj_norm, cpf, nome, delay, bloqueio, delay_max))


@router.get("/api/sat/pat-ignorar", response_class=JSONResponse)
async def api_pat_ignorar_get():
    """Lista de espécies de processo administrativo (PAT) a ignorar (não baixar)."""
    return JSONResponse({"itens": _cfg.get_sat_pat_ignorar()})


@router.post("/api/sat/pat-ignorar", response_class=JSONResponse)
async def api_pat_ignorar_set(request: Request):
    """Salva a lista de espécies de PAT a ignorar."""
    body = await request.json()
    itens = body.get("itens") or []
    if not isinstance(itens, list):
        itens = []
    itens = _cfg.set_sat_pat_ignorar([str(x) for x in itens])
    return JSONResponse({"ok": True, "itens": itens})


@router.get("/api/sat/pat-catalogo", response_class=JSONResponse)
async def api_pat_catalogo_get():
    """Catálogo de espécies de Tarefas PAT: `completa` (tudo já conhecido — cresce
    sozinho a cada consulta), `ignoradas` (lista "a ignorar") e `filtrada` (completa
    menos as ignoradas — é a oferecida para associar por pretensão)."""
    return JSONResponse({
        "completa": _cfg.get_sat_pat_catalogo(),
        "ignoradas": _cfg.get_sat_pat_ignorar(),
        "filtrada": _cfg.get_sat_pat_filtrada(),
    })


@router.get("/api/sat/pat-por-pretensao", response_class=JSONResponse)
async def api_pat_por_pretensao_get():
    """{pretensao: [espécies de PAT associadas]} — ausente/vazio = sem associação
    própria (baixa todas as não ignoradas)."""
    return JSONResponse({"por_pretensao": _cfg.get_sat_pat_por_pretensao()})


@router.post("/api/sat/pat-por-pretensao", response_class=JSONResponse)
async def api_pat_por_pretensao_set(request: Request):
    """Salva a associação de espécies de PAT de 1 pretensão. Body: {pretensao, servicos}."""
    body = await request.json()
    pretensao = str(body.get("pretensao", "") or "")
    servicos = body.get("servicos") or []
    if not isinstance(servicos, list):
        servicos = []
    por_pretensao = _cfg.set_sat_pat_por_pretensao(pretensao, [str(s) for s in servicos])
    return JSONResponse({"ok": True, "por_pretensao": por_pretensao})


@router.get("/api/sat/tipos", response_class=JSONResponse)
async def api_tipos_get():
    """Catálogo de tipos de documentos + seleção salva (padrão + por pretensão)."""
    from ingestao import sat_client
    return JSONResponse({
        "tipos": sat_client.TIPOS_DOCUMENTOS,
        "padrao": _cfg.get_sat_tipos_padrao(),  # None = todos
        "por_pretensao": _cfg.get_sat_tipos_por_pretensao(),
        "instituidor": _cfg.get_sat_tipos_instituidor(),  # None = usar o default abaixo
        "instituidor_default": sat_client.TIPOS_INSTITUIDOR_DEFAULT,
    })


@router.post("/api/sat/tipos", response_class=JSONResponse)
async def api_tipos_set(request: Request):
    """Salva as keys de uma pretensão (pretensão vazia = PADRÃO)."""
    body = await request.json()
    pretensao = str(body.get("pretensao", "") or "")
    keys = body.get("keys") or []
    if not isinstance(keys, list):
        keys = []
    res = _cfg.set_sat_tipos(pretensao, [str(k) for k in keys])
    return JSONResponse({"ok": True, **res})


@router.get("/api/sat/{paj_norm}/processar")
async def processar(paj_norm: str, grupo: str = "", dry: int = 0, prazo: int = 0, manual: int = 1):
    """SSE do processamento: movimenta → tramita → conclui (se houver trâmite na
    caixa). `dry=1` => simulação; `prazo` = dias de prazo de conclusão da tramitação
    (0 = sem prazo); `manual` = 1 (padrão, PAJ cadastrado à mão — sem conclusão) ou 0
    (PAJ veio da caixa — conclui o trâmite originário ao final)."""
    return EventSourceResponse(_stream_processar(paj_norm, grupo, bool(dry), int(prazo), bool(manual)))


async def _stream_baixar_complementar(paj_norm: str, cpf: str, nome: str, delay: float,
                                      bloqueio: float, alvos_csv: str, escopo: str = "assistido",
                                      delay_max: float | None = None):
    from ingestao import sat_client
    paths = sat_service._escopo_paths(paj_norm, escopo)
    pasta = paths["complementar"]
    with __import__("contextlib").suppress(Exception):
        from services import exclusao_suave  # Lixeira, fora do processo do painel
        exclusao_suave.apagar_pasta(pasta, "sat:complementar")  # isola esta rodada
    alvos = [a for a in (alvos_csv or "").split(",") if a] or None  # "key|sub"
    tipos = sorted({a.split("|", 1)[0] for a in alvos}) if alvos else None
    # acervo = pasta do escopo (principal/instituidor): verificação prévia pula o que já foi
    # baixado (retomada de download interrompido, sem re-baixar os já presentes).
    acervo = str(paths["destino"])
    try:
        async for ev in sat_client.baixar_cidadao_stream(
                cpf, nome, str(pasta), delay_s=delay, bloqueio_s=bloqueio, delay_max_s=delay_max,
                tipos=tipos, pretensao="", rotulo=paths["rotulo"], alvos=alvos,
                pasta_acervo=acervo):
            if ev.get("done"):
                yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
            else:
                yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}
    except Exception as e:  # noqa: BLE001
        yield {"event": "log", "data": json.dumps({"level": "erro", "msg": str(e)}, ensure_ascii=False)}
        yield {"event": "done", "data": json.dumps({"ok": False, "detalhe": str(e)}, ensure_ascii=False)}


@router.get("/api/sat/{paj_norm}/baixar-complementar")
async def baixar_complementar(paj_norm: str, cpf: str, nome: str = "", delay: float = 5.0,
                              bloqueio: float = 45.0, alvos: str = "", escopo: str = "assistido",
                              delay_max: float | None = None):
    """SSE do DOWNLOAD COMPLEMENTAR (nova consulta ao SAT no PAJ já concluído). `alvos` =
    "key|sub" separados por vírgula (subitens escolhidos). `escopo` = assistido|instituidor
    (instituidor usa CPF do instituidor, rótulo 'INSTITUIDOR - ' e subpasta 'Instituidor').
    Salva na subpasta complementar do escopo (limpa antes)."""
    return EventSourceResponse(
        _stream_baixar_complementar(paj_norm, cpf, nome, delay, bloqueio, alvos, escopo, delay_max))


async def _stream_anexar_mais(paj_norm: str, rec: str, id_processo: str, assistido: str, nomes_csv: str):
    # filenames sanitizados não contêm '|' → separador seguro para a lista.
    nomes = [n for n in (nomes_csv or "").split("|") if n]
    async for ev in sat_service.anexar_mais_stream(paj_norm, rec, id_processo, assistido, nomes):
        if ev.get("done"):
            yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
        else:
            yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}


@router.get("/api/sat/{paj_norm}/anexar-mais")
async def anexar_mais(paj_norm: str, rec: str = "", id: str = "", assistido: str = "", nomes: str = ""):
    """SSE da ANEXAÇÃO COMPLEMENTAR (nova movimentação só de juntada, sem tramitar/concluir)
    num PAJ já processado. `rec` = id do registro do histórico a complementar; `nomes` =
    nomes dos arquivos escolhidos, separados por '|'."""
    return EventSourceResponse(_stream_anexar_mais(paj_norm, rec, id, assistido, nomes))


@router.post("/api/sat/{paj_norm}/batimento/incluir", response_class=JSONResponse)
async def batimento_incluir(paj_norm: str, alvos: str = "", escopo: str = "assistido"):
    """Sincroniza a flag `incluir` da lista de batimento deste PAJ/escopo com o que está
    marcado no modal "Baixar mais" agora: os alvos enviados (key|sub, separados por
    vírgula) ficam incluir=True; o resto da lista (ainda não baixado) vira incluir=False.
    Sempre reversível — nunca um bloqueio permanente. Retorna {ok, tem_falha} (ou
    {ok:False, erro} se o PAJ ainda não tiver lista de batimento)."""
    return JSONResponse(sat_service.sincronizar_incluir_batimento(paj_norm, alvos, escopo))


@router.get("/api/sat/{paj_norm}/estado-local", response_class=JSONResponse)
async def estado_local(paj_norm: str):
    """Campos recalculados na hora, só a partir do disco local (sem tocar o SISDPU):
    tem_log/tem_falha/extras_falha/n_arquivos/etc. Usado pelo front-end para atualizar, no
    lugar, a linha de um PAJ já carregada nas tabelas da Fila Automática/Individual depois de
    uma ação puramente local (ex.: salvar a seleção no modal "Baixar mais") — sem precisar
    resincronizar a caixa inteira com o SISDPU."""
    return JSONResponse(sat_service._campos_locais(paj_norm))


@router.post("/api/sat/{paj_norm}/mesclar-complementar", response_class=JSONResponse)
async def mesclar_complementar(paj_norm: str, escopo: str = "assistido"):
    """Download complementar em PAJ pendente (ainda NÃO anexado): move os PDFs para a pasta
    do escopo (assistido → principal; instituidor → 'Instituidor') e mescla o log/inventário
    — SEM movimentação. Os arquivos entram no acervo para a futura 'Anexar ao PAJ'."""
    return JSONResponse(sat_service.mesclar_complementar_pendente(paj_norm, escopo))


@router.get("/api/sat/grupos", response_class=JSONResponse)
async def api_grupos(id: str = ""):
    """Lista caixas de grupo da unidade — lê a tela de tramitação em modo bloco (sem
    PAJ), pelo link "Tramitação" direto da caixa de entrada; não depende de haver
    PAJ pendente/cadastrado. `id` (id_processo), se informado, abre no contexto dele."""
    grupos = await movtram.listar_grupos_unidade(id)
    return JSONResponse({"grupos": grupos, "padrao": sat_service.GRUPO_PADRAO})


# ---------------------------------------------------------------------------
# configurações — parametrização das consultas e da tramitação
# ---------------------------------------------------------------------------
@router.get("/api/config/sat-central", response_class=JSONResponse)
async def get_sat_central():
    return JSONResponse(_cfg.get_sat_central_defaults())


@router.post("/api/config/sat-central", response_class=JSONResponse)
async def set_sat_central(request: Request):
    body = await request.json()
    defaults = _cfg.set_sat_central_defaults(body)
    return JSONResponse({"ok": True, "defaults": defaults})


@router.get("/api/capsolver/saldo", response_class=JSONResponse)
async def api_capsolver_saldo():
    """Testa a chave de API do CapSolver cadastrada no cofre e devolve o saldo."""
    from ingestao import captcha_solver
    return JSONResponse(await captcha_solver.saldo())


# ---------------------------------------------------------------------------
# segurança — status/reconexão da sessão do SISDPU (selo da barra superior)
# ---------------------------------------------------------------------------
@router.get("/api/seguranca/status", response_class=JSONResponse)
async def api_seguranca_status():
    from services import conexao_service
    return JSONResponse(conexao_service.status())


@router.post("/api/seguranca/reconectar", response_class=JSONResponse)
async def api_seguranca_reconectar():
    """Reconecta a sessão do SISDPU (recupera de conflito de sessão / sessão
    derrubada). Fecha o navegador headless e refaz o login com as credenciais do cofre."""
    from ingestao import sisdpu_client
    try:
        res = await sisdpu_client.reconectar()
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "status": "erro", "detalhe": str(e)}
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


# ---------------------------------------------------------------------------
# encerrar painel
# ---------------------------------------------------------------------------
def _encerrar_servidor() -> None:
    with __import__("contextlib").suppress(Exception):
        PID_FILE.unlink(missing_ok=True)
    time.sleep(0.4)
    os._exit(0)


@router.post("/api/encerrar", response_class=JSONResponse)
async def api_encerrar():
    return JSONResponse({"ok": True}, background=BackgroundTask(_encerrar_servidor))


@router.get("/api/sat/{paj_norm}/paj-completo")
async def api_paj_completo(paj_norm: str):
    """Gera sob demanda (ao vivo, sem sincronização prévia persistida) um PDF
    único com capa, narrativa, histórico de movimentações e TODOS os anexos
    deste PAJ — feature "Baixar PAJ Completo" (ícone na coluna Ação). Sempre
    completo, sem opção de recorte/arquivos crus (pedido do usuário,
    2026-11-13). Pode demorar ~10-60s dependendo de quantas movimentações
    tiverem anexo — ver `services/paj_completo_service.py`."""
    from services import paj_completo_service
    try:
        pdf_bytes, nome_arquivo = await paj_completo_service.gerar_pdf_paj_completo(paj_norm)
    except paj_completo_service.PajCompletoErro as e:
        return JSONResponse({"erro": str(e)}, status_code=502)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"erro": f"Erro inesperado ao gerar o PDF: {e}"}, status_code=500)
    from urllib.parse import quote
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(nome_arquivo)}"},
    )
