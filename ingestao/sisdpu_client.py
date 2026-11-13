"""Cliente SISDPU via Playwright headless (async).

Mantido como funcoes modulo-level com estado global de browser/page para
simplificar uso.

Credenciais vem do COFRE cifrado por senha-mestra (services.cofre_service) —
sem fallback via .env.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import re
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, Page, async_playwright
from playwright.async_api import TimeoutError as PWTimeoutError
import contextlib


SISDPU_URL = "https://sisdpu.dpu.def.br/sisdpu"

# Há UMA única page/sessão headless do SISDPU, compartilhada por toda a aplicação
# (fila automática, "Sincronizar caixa" manual, "Adicionar PAJ", Configurações…).
# Duas operações concorrentes (ex.: a fila lendo a caixa enquanto o usuário clica
# "Sincronizar caixa" na tela) disputavam a MESMA page sem nenhuma exclusão mútua —
# uma podia navegar a page para outra URL bem no meio da leitura da outra, lendo um
# DOM pela metade (ex.: a tabela de uma caixa de grupo aparecendo com só 1 de 5 PAJs,
# sem nenhum erro). PAGE_LOCK serializa as operações que leem/navegam a page
# compartilhada — ver uso em ingestao/sisdpu_movtram.py (bug corrigido 2026-11-10).
PAGE_LOCK = asyncio.Lock()

# Logger dedicado — os logs `[busca-paj]` vao para logs/app.log (handler
# configurado em app.py com rotacao diaria). Tambem aparecem no console
# quando o painel e' iniciado num terminal visivel.
_log = logging.getLogger("sisdpu.busca")

# Diretorio onde despejamos screenshot + HTML quando a busca via campo global
# falha — facilita o diagnostico (DOM real do SISDPU vs nossa heuristica).
_DEBUG_DIR = Path(__file__).resolve().parents[1] / "logs" / "sisdpu_debug"

# Retencao do diretorio de debug: pra nao acumular indefinidamente (cada dump
# tem screenshot+HTML+body, ~1MB), mantem soh os N mais recentes. Suficiente
# pra investigar a janela ativa de problemas sem inflar o repositorio.
_DEBUG_DIR_RETER = 20


def _purgar_debug_dir_antigos(reter: int = _DEBUG_DIR_RETER) -> None:
    """Remove subpastas mais antigas de _DEBUG_DIR, mantendo apenas as `reter`
    mais recentes (por mtime). Best-effort — qualquer erro e' suprimido pra
    nao quebrar o fluxo principal."""
    if not _DEBUG_DIR.exists():
        return
    with contextlib.suppress(Exception):
        import shutil

        subpastas = [p for p in _DEBUG_DIR.iterdir() if p.is_dir()]
        if len(subpastas) <= reter:
            return
        subpastas.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for antigo in subpastas[reter:]:
            with contextlib.suppress(Exception):
                shutil.rmtree(antigo)


class CredenciaisInvalidas(Exception):
    """SISDPU rejeitou usuario/senha no login."""

    pass


_playwright: Any = None
_browser: Browser | None = None
_page: Page | None = None


# JS que espera o PrimeFaces terminar todos os AJAX pendentes.
_WAIT_PF_AJAX = """
() => new Promise((resolve) => {
    const check = () => {
        if (typeof PrimeFaces === 'undefined'
            || !PrimeFaces.ajax
            || !PrimeFaces.ajax.Queue
            || PrimeFaces.ajax.Queue.isEmpty()) {
            resolve(true);
        } else {
            setTimeout(check, 100);
        }
    };
    check();
})
"""


def _sel(element_id: str) -> str:
    """Gera seletor CSS para IDs JSF (que contem ':')."""
    return f'[id="{element_id}"]'


async def _get_page() -> Page:
    """Retorna a page Playwright, criando browser se necessario.

    O Chromium e' iniciado com flags que FORCAM download de PDFs em vez de
    abrir o preview interno — importante pro `listar_arquivos_paj`, ja que
    queremos capturar o arquivo bruto via evento `download`, nao previsualizar.
    """
    global _playwright, _browser, _page
    if _page is not None:
        try:
            # Acessa atributo so para verificar que a page ainda esta viva;
            # se foi fechada, levanta exception e caimos no reset abaixo.
            _ = _page.url
            return _page
        except Exception:
            _page = None
            _browser = None

    if _playwright is None:
        _playwright = await async_playwright().start()

    _browser = await _playwright.chromium.launch(
        headless=True,
        args=[
            # Desabilita o PDF viewer interno do Chromium: PDFs viram download
            "--disable-features=PDFViewerUpdate,PdfUnseasoned",
        ],
    )
    context = await _browser.new_context(
        viewport={"width": 1280, "height": 1024},
        accept_downloads=True,
    )
    _page = await context.new_page()
    return _page


async def _wait_pf_ajax(page: Page, timeout: int = 10000) -> None:
    """Espera o PrimeFaces terminar AJAX pendente, com timeout de seguranca."""
    try:
        await page.evaluate(_WAIT_PF_AJAX, timeout=timeout)
    except Exception:
        await page.wait_for_timeout(1000)


def _obter_credenciais() -> tuple[str, str]:
    """Credenciais do SISDPU: SEMPRE do cofre cifrado (senha-mestra). Levanta
    RuntimeError com mensagem clara se o cofre não estiver configurado/destrancado."""
    from services import cofre_service
    creds = cofre_service.get_credenciais("sisdpu")
    if creds:
        return creds
    if cofre_service.configurado() and not cofre_service.destrancado():
        raise RuntimeError(
            "Cofre de credenciais bloqueado — desbloqueie com a senha-mestra "
            "para acessar o SISDPU.")
    raise RuntimeError(
        "Credenciais do SISDPU não configuradas — cadastre-as no cofre "
        "(Configurações).")


async def _login() -> None:
    """Faz login no SISDPU."""
    user, senha = _obter_credenciais()
    page = await _get_page()
    await page.goto(f"{SISDPU_URL}/login.xhtml", wait_until="domcontentloaded")
    await page.wait_for_selector(_sel("frmLogin:epaj_input_usuario"), timeout=15000)
    await page.fill(_sel("frmLogin:epaj_input_usuario"), user)
    await page.fill(_sel("frmLogin:epaj_input_senha"), senha)
    await page.evaluate('PrimeFaces.ab({s:"frmLogin:loginButton",f:"frmLogin"})')
    try:
        await page.wait_for_url("**/caixaEntrada**", timeout=15000)
    except PWTimeoutError:
        # Nao chegou na caixa — checar se SISDPU recusou as credenciais.
        # PrimeFaces exibe erros em .ui-messages-error, .mensagem-erro, ou
        # um <div class="ui-growl-message"> com texto de usuario/senha.
        msg_recusa = await page.evaluate(
            """() => {
                const seletores = [
                    '.ui-messages-error-detail',
                    '.ui-messages-error',
                    '.mensagem-erro',
                    '.ui-growl-message',
                    '#frmLogin .ui-message-error',
                ];
                for (const s of seletores) {
                    const el = document.querySelector(s);
                    if (el && el.innerText && el.innerText.trim()) {
                        return el.innerText.trim();
                    }
                }
                // fallback: varrer body por frase caracteristica
                const body = (document.body.innerText || '').toLowerCase();
                const pistas = [
                    'usu\u00e1rio ou senha', 'usuario ou senha',
                    'senha incorreta', 'senha inv\u00e1lida', 'senha invalida',
                    'credenciais inv\u00e1lidas', 'credenciais invalidas',
                    'login inv\u00e1lido', 'login invalido',
                ];
                for (const p of pistas) {
                    const i = body.indexOf(p);
                    if (i >= 0) return body.substr(Math.max(0, i - 20), 120);
                }
                return null;
            }"""
        )
        if msg_recusa:
            with contextlib.suppress(Exception):
                from services import conexao_service
                conexao_service.marcar_erro("O SISDPU recusou o usuário/senha.")
            raise CredenciaisInvalidas(msg_recusa[:200]) from None
        # Nao foi login fail explicito — repropaga como timeout.
        raise
    # Login OK — sessão saudável; limpa qualquer conflito/erro anterior.
    with contextlib.suppress(Exception):
        from services import conexao_service
        conexao_service.marcar_conectado()


async def _ensure_logged_in() -> None:
    """Garante que estamos logados. Refaz login se sessao expirou."""
    global _browser, _page
    page = await _get_page()
    try:
        url = page.url
        if "login" in url or not url.startswith("http"):
            await _login()
            return
        await page.goto(
            f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml",
            wait_until="domcontentloaded",
            timeout=10000,
        )
        await page.wait_for_timeout(500)
        if "login" in page.url:
            await _login()
    except Exception:
        try:
            if _browser:
                await _browser.close()
        except Exception:
            pass
        _browser = None
        _page = None
        await _login()


async def caixa_de_entrada() -> dict:
    """Navega para a caixa de entrada e extrai os itens listados."""
    await _ensure_logged_in()
    page = await _get_page()
    await page.goto(
        f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml",
        wait_until="domcontentloaded",
    )
    await page.wait_for_timeout(2000)
    await _wait_pf_ajax(page, timeout=10000)

    body = await page.inner_text("body")

    table_data = await page.evaluate(
        """
        () => {
            const tables = document.querySelectorAll(
                '.ui-datatable-data tr, .ui-datalist-content .ui-datalist-item'
            );
            if (tables.length > 0) {
                return Array.from(tables).map(tr => tr.innerText.trim())
                    .filter(t => t.length > 0);
            }
            const allTr = document.querySelectorAll('table tbody tr');
            return Array.from(allTr).map(tr => tr.innerText.trim())
                .filter(t => t.length > 0);
        }
        """
    )

    return {
        "url": page.url,
        "itens_tabela": table_data or [],
        "texto_pagina": body[:5000],
    }


# JS que dispara a busca rapida do SISDPU. Descobertas chave (vistas no
# page.html do debug):
#  - O botao tem onmouseup="buscarProcesso(event)" (funcao JS customizada).
#  - O resultado abre em POPUP via redirecionar(url, idProcesso) ->
#    window.open(...). Por isso a URL da janela atual nunca muda.
#  - O ano/unidade da busca vem dos hidden inputs anoAtualParaPesquisaRapida
#    e codigoUnidadeParaPesquisaRapida (defaults para a unidade do usuario).
#    Para buscar PAJ de outro ano/unidade temos que alterar esses hidden.
_JS_PESQUISAR_PAJ = """
(args) => {
    const { termo, ano, unidade } = args;
    const debug = {};

    // ----- 1) Hidden inputs ano/unidade — sobrescreve para o PAJ desejado -----
    const anoHidden = document.getElementById('anoAtualParaPesquisaRapida');
    const unidHidden = document.getElementById('codigoUnidadeParaPesquisaRapida');
    if (anoHidden) {
        debug.anoAntes = anoHidden.value;
        anoHidden.value = ano;
        debug.anoDepois = anoHidden.value;
    }
    if (unidHidden) {
        debug.unidAntes = unidHidden.value;
        unidHidden.value = unidade;
        debug.unidDepois = unidHidden.value;
    }

    // ----- 2) Input do numero -----
    const input = document.getElementById('processoPesquisaRapidaNumeroProcesso');
    if (!input) return { ok: false, erro: 'input processoPesquisaRapidaNumeroProcesso nao encontrado', debug };
    debug.inputId = input.id;

    const btn = document.getElementById('botaoPesquisarPesquisaRapida');
    if (!btn) return { ok: false, erro: 'botao botaoPesquisarPesquisaRapida nao encontrado', debug };
    debug.btnId = btn.id;

    input.focus();
    input.value = termo;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));

    // ----- 3) Aciona buscarProcesso(event) — o handler do onmouseup -----
    try {
        if (typeof buscarProcesso === 'function') {
            const ev = new MouseEvent('mouseup', { bubbles: true, cancelable: true });
            Object.defineProperty(ev, 'target', { value: btn, writable: false });
            buscarProcesso(ev);
            debug.metodo = 'buscarProcesso(event)';
            return { ok: true, debug };
        }
        debug.buscarProcesso_disponivel = false;
    } catch (e) {
        debug.busca_erro = e.message;
    }

    // Fallback: dispara mouseup real no botao
    try {
        btn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
        debug.metodo = (debug.metodo || '') + '+mouseup';
    } catch (e) {
        debug.mouseup_erro = e.message;
    }

    return { ok: true, debug };
}
"""


async def _dump_debug_paj(
    page: Page,
    paj_texto: str,
    motivo: str,
    extras: dict | None = None,
    extra_pages: dict[str, Page] | None = None,
) -> Path:
    """Salva screenshot + HTML + body.innerText + info da pagina atual em
    logs/sisdpu_debug/ para diagnostico. Usado tanto em falhas quanto em
    sucessos durante a fase de afinacao dos seletores. Devolve a pasta criada.

    `extra_pages={"popup": page2, ...}` dumpa tambem essas paginas extras com
    sufixo no nome do arquivo (popup_screenshot.png, popup_body.txt, etc.) —
    util pra capturar popups que foram abertos mas nao bateram a verificacao.
    """
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = paj_texto.replace("/", "-").replace(" ", "_")
    pasta = _DEBUG_DIR / f"{ts}_{slug}"
    pasta.mkdir(parents=True, exist_ok=True)

    # Purga subpastas antigas pra evitar acumulo (cada dump pesa ~1MB).
    _purgar_debug_dir_antigos()

    with contextlib.suppress(Exception):
        await page.screenshot(path=str(pasta / "screenshot.png"), full_page=True)
    with contextlib.suppress(Exception):
        html = await page.content()
        (pasta / "page.html").write_text(html, encoding="utf-8")
    with contextlib.suppress(Exception):
        texto = await page.inner_text("body")
        (pasta / "body.txt").write_text(texto, encoding="utf-8")

    info = [
        f"timestamp: {ts}",
        f"paj_texto: {paj_texto}",
        f"motivo: {motivo}",
        f"url: {page.url}",
    ]
    if extras:
        for k, v in extras.items():
            info.append(f"{k}: {v}")

    if extra_pages:
        for nome, pg in extra_pages.items():
            with contextlib.suppress(Exception):
                await pg.screenshot(path=str(pasta / f"{nome}_screenshot.png"), full_page=True)
            with contextlib.suppress(Exception):
                (pasta / f"{nome}_page.html").write_text(await pg.content(), encoding="utf-8")
            with contextlib.suppress(Exception):
                (pasta / f"{nome}_body.txt").write_text(
                    await pg.inner_text("body"), encoding="utf-8"
                )
            with contextlib.suppress(Exception):
                info.append(f"{nome}_url: {pg.url}")

    (pasta / "info.txt").write_text("\n".join(info) + "\n", encoding="utf-8")
    return pasta


async def buscar_paj_global(
    numero: str,
    ano: str,
    unidade: str = "44",
    forcar_sessao: bool = True,
    esperas_curtas: bool = False,
    somente_url: bool = False,
) -> dict:
    """Busca um PAJ via campo de pesquisa global do SISDPU (header da caixa).

    Diferente de `movimentacoes_paj`, esta funcao NAO depende do PAJ estar na
    caixa de entrada do defensor — usa o campo "Pesquisar" que aceita qualquer
    PAJ da unidade. Extrai os mesmos dados de cabecalho e movimentacoes.

    `forcar_sessao` (padrao True): fecha o navegador e refaz o login do zero
    (estado 100% limpo, porem LENTO). Passe False para REAPROVEITAR a sessao ja
    aberta — `_ensure_logged_in` verifica a autenticacao e so refaz o login se a
    sessao tiver expirado (muito mais rapido; usado pela inclusao manual de PAJ).

    Retorna `{"erro": "..."}` se a busca falhar. Em caso de falha, salva
    screenshot + HTML + info em `logs/sisdpu_debug/` para diagnostico.
    """
    paj_texto = f"{ano}/{unidade.zfill(3)}-{numero}"
    _log.info("INICIO termo='%s' forcar_sessao=%s", paj_texto, forcar_sessao)

    # Autenticacao: com forcar_sessao=True, fecha e reloga do zero (estado limpo,
    # lento). Com False, apenas garante login (reaproveita a sessao viva; so reloga
    # se expirou) — checagem de auth + login automatico, sem o custo de reabrir tudo.
    if forcar_sessao:
        _log.info("forcando nova sessao (fechar+login)...")
        with contextlib.suppress(Exception):
            await fechar()
    await _ensure_logged_in()
    page = await _get_page()
    _log.info("logado, url pos-login: %s", page.url)

    await page.goto(
        f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml",
        wait_until="domcontentloaded",
    )
    await page.wait_for_timeout(1500)
    await _wait_pf_ajax(page, timeout=10000)
    _log.info("caixa carregada url=%s", page.url)

    # ---------- Verificacoes de sessao viva ----------
    # 1) URL nao caiu pra tela de login (redirect silencioso por sessao expirada).
    if "login" in page.url.lower():
        msg = "apos login, ainda na tela de login — sessao nao autenticada"
        with contextlib.suppress(Exception):
            pasta = await _dump_debug_paj(page, paj_texto, msg)
            _log.warning("FIM(login-nao-efetivo) debug salvo em: %s", pasta)
        return {"erro": msg}

    # 2) Estamos de fato na area logada: precisa ter o botao "Pesquisar" do
    #    header (visivel em todas as telas autenticadas) E o titulo da unidade
    #    ("Unidade DPU - ..."). Se nao aparecerem, a sessao esta "morta"
    #    (logado mas sem direitos / popup bloqueando / pagina parcial).
    sessao_viva = await page.evaluate(
        """() => {
            const corpo = (document.body.innerText || '').toLowerCase();
            const temUnidade = corpo.includes('unidade dpu');
            const cands = document.querySelectorAll(
                'button, input[type="submit"], input[type="button"], input[type="image"], a, span[role="button"]'
            );
            let temBtnPesquisar = false;
            for (const el of cands) {
                const txt = ((el.textContent || el.value || el.title || el.getAttribute('aria-label') || '') + '').trim().toLowerCase();
                if (txt === 'pesquisar' || txt === 'buscar' || txt.includes('pesquis')) {
                    temBtnPesquisar = true; break;
                }
            }
            return {
                ok: temUnidade && temBtnPesquisar,
                temUnidade, temBtnPesquisar,
                trecho_inicio_body: corpo.substring(0, 200)
            };
        }"""
    )
    _log.info("sessao_viva=%s", sessao_viva)
    if not sessao_viva.get("ok"):
        msg = (
            f"sessao SIS-DPU nao confirmada como ativa — "
            f"temUnidade={sessao_viva.get('temUnidade')} "
            f"temBtnPesquisar={sessao_viva.get('temBtnPesquisar')}"
        )
        with contextlib.suppress(Exception):
            pasta = await _dump_debug_paj(
                page,
                paj_texto,
                msg,
                extras={"sessao": sessao_viva},
            )
            _log.warning("FIM(sessao-morta) debug salvo em: %s", pasta)
        return {"erro": msg}

    # Diagnostico inicial — lista os inputs de texto e botoes com texto
    # "Pesquisar"/"Buscar" presentes na pagina. Util pra entender se a
    # heuristica esta achando o elemento errado ou nada.
    inventario = await page.evaluate(
        """() => {
            const inputs = Array.from(document.querySelectorAll('input')).map(el => ({
                id: el.id || '',
                type: el.type || '',
                name: el.name || '',
                placeholder: el.placeholder || '',
                ariaLabel: el.getAttribute('aria-label') || ''
            })).filter(i => i.type === 'text' || i.type === '' || i.type === 'search');

            const cands = document.querySelectorAll(
                'button, input[type="submit"], input[type="button"], input[type="image"], a, span[role="button"]'
            );
            const botoes = [];
            for (const el of cands) {
                const txt = ((el.textContent || el.value || el.title || el.getAttribute('aria-label') || '') + '').trim();
                if (txt.toLowerCase().includes('pesquis') || txt.toLowerCase().includes('buscar')) {
                    botoes.push({ tag: el.tagName, id: el.id || '', texto: txt.substring(0, 40) });
                }
            }
            return {
                n_inputs_texto: inputs.length,
                inputs_amostra: inputs.slice(0, 12),
                botoes_pesquisar: botoes.slice(0, 8),
                titulo: document.title || ''
            };
        }"""
    )
    _log.info("inventario da pagina: %s", inventario)

    # Regra confirmada pelo usuario: o campo de pesquisa SEMPRE exige o numero
    # completo do PAJ (12 digitos no total: 4 ano + 3 unidade + 5 numero).
    # Caracteres especiais (`/` e `-`) sao opcionais. Pesquisar so pelo numero
    # final (ex: '00090' ou '90') NAO funciona — o SIS-DPU exige ano+unidade
    # tambem. Por isso so' tentamos as 2 formas validas:
    #   1) canonico com separadores  — `2024/003-00512`
    #   2) 12 digitos puros          — `202400300512`
    numero_z = numero.zfill(5)
    unidade_z = unidade.zfill(3)
    tentativas = [
        f"{ano}/{unidade_z}-{numero_z}",  # 2024/003-00512 (formato PAJ canonico)
        f"{ano}{unidade_z}{numero_z}",  # 202400300512 (12 digitos puros)
    ]
    vistos: set[str] = set()
    tentativas = [t for t in tentativas if not (t in vistos or vistos.add(t))]

    # Page do detalhamento — pode ser a propria janela atual (se buscarProcesso
    # navegar) ou um POPUP novo (descobrimos no markup que redirecionar() faz
    # window.open). Comeca como None — a primeira tentativa que abrir popup
    # OU navegar a janela atual sera capturada.
    page_detalhe: Page | None = None
    sucesso = False
    erro_ultimo = ""
    debug_acumulado: list[dict] = []
    # Regex que confirma "estou na tela de detalhamento do PAJ". O SIS-DPU
    # tem mais de uma variante visual de cabecalho — aceitamos qualquer pista:
    #  - "AAAA/UUU-NNNNN - PAJ STATUS" (formato compacto, presente em algumas paginas)
    #  - "Numero do PAJ\tAAAA/UUU-NNNNN" (formato tabular, mais comum na tela atual)
    #  - "Detalhamento do Processo" (titulo do breadcrumb — pista robusta)
    re_pag_detalhe = re.compile(
        r"\d{4}/\d{3}-\d+\s*-\s*PAJ\s"
        r"|N[úu]mero\s+do\s+PAJ\s+\d{4}/\d{3}-\d+"
        r"|Detalhamento\s+do\s+Processo"
    )
    contexto = page.context

    for termo in tentativas:
        _log.info("tentando termo='%s'", termo)

        # Escuta um novo "page" (popup) sendo aberto no contexto. Se a
        # buscarProcesso navegar na janela atual, esse expect_page nao captura
        # nada e cai no fallback abaixo.
        popup_capturado: Page | None = None
        try:
            async with contexto.expect_page(timeout=12000) as popup_info:
                res = await page.evaluate(
                    _JS_PESQUISAR_PAJ,
                    {"termo": termo, "ano": ano, "unidade": unidade_z},
                )
                if not isinstance(res, dict) or not res.get("ok"):
                    erro_ultimo = (res or {}).get("erro", "falha ao localizar campo de pesquisa")
                    debug_acumulado.append({"termo": termo, "res": res})
                    _log.warning("  FALHA: %s debug=%s", erro_ultimo, res)
                    if "campo" in erro_ultimo or "botao" in erro_ultimo:
                        # nao adianta tentar outros termos
                        raise StopIteration
            popup_capturado = await popup_info.value
            _log.info("  popup capturado url=%s", popup_capturado.url)
        except StopIteration:
            break
        except PWTimeoutError:
            _log.info("  nenhum popup aberto neste termo (timeout do expect_page)")
        except Exception as e:
            _log.warning("  erro ao esperar popup: %s: %s", type(e).__name__, e)

        # Se capturou popup, estrategia em 2 etapas:
        #
        # ETAPA 1 — tentar usar o popup direto. Espera load + polling do regex.
        # ETAPA 2 (fallback) — pega URL do popup e NAVEGA a janela principal
        # pra ela. Isso funciona porque `redirecionar(url, idProcesso)` no
        # SIS-DPU e' so' `window.open(url + idProcesso)` — sem state JSF, e a
        # URL e' totalmente stateless (so' precisa de cookie de sessao).
        # Como a janela principal ja tem PrimeFaces carregado da caixa, o load
        # completa de forma previsivel.
        # JS que detecta "estou na tela de detalhamento do PAJ" — aceita 3
        # padroes de cabecalho que o SIS-DPU usa em telas diferentes (mesmos
        # padroes do regex Python `re_pag_detalhe` acima). Compartilhado entre
        # ETAPA 1 (popup) e ETAPA 2 (janela principal pos-goto).
        js_detect_header = r"""() => {
            const txt = (document.body && document.body.innerText) || '';
            return /\d{4}\/\d{3}-\d+\s*-\s*PAJ\s/.test(txt)
                || /N[úu]mero\s+do\s+PAJ\s+\d{4}\/\d{3}-\d+/.test(txt)
                || /Detalhamento\s+do\s+Processo/.test(txt);
        }"""

        if popup_capturado is not None:
            popup_url_inicial = popup_capturado.url
            with contextlib.suppress(Exception):
                await popup_capturado.wait_for_load_state(
                    "domcontentloaded",
                    timeout=15000,
                )

            tem_header = False
            # ETAPA 1: tentar ler do popup (10s)
            with contextlib.suppress(PWTimeoutError, Exception):
                await popup_capturado.wait_for_function(
                    js_detect_header,
                    timeout=10000,
                    polling=250,
                )
                tem_header = True

            if tem_header:
                # Popup funcionou — usa ele direto
                with contextlib.suppress(Exception):
                    await _wait_pf_ajax(popup_capturado, timeout=10000)
                await popup_capturado.wait_for_timeout(500)
                _log.info(
                    "  popup OK url=%s",
                    popup_capturado.url,
                )
                page_detalhe = popup_capturado
                sucesso = True
                break

            # ETAPA 2: fallback — navega janela principal pra popup_url
            popup_url_final = popup_capturado.url
            url_alvo = (
                popup_url_final if "detalhamentoProcesso" in popup_url_final else popup_url_inicial
            )
            _log.info(
                "  popup nao renderizou em 10s — fallback: navegar janela principal pra %s",
                url_alvo,
            )

            # Fecha o popup antes de mexer na janela principal
            with contextlib.suppress(Exception):
                await popup_capturado.close()

            if not url_alvo or "detalhamentoProcesso" not in url_alvo:
                erro_ultimo = f"popup '{termo}' nao renderizou e URL inesperada: {url_alvo!r}"
                debug_acumulado.append({"termo": termo, "popup_url": url_alvo})
                continue

            try:
                await page.goto(url_alvo, wait_until="domcontentloaded", timeout=20000)
            except Exception as e:
                _log.warning("  fallback goto falhou: %s: %s", type(e).__name__, e)
                erro_ultimo = f"popup '{termo}' + fallback navegacao falhou: {e}"
                debug_acumulado.append({"termo": termo, "popup_url": url_alvo})
                continue

            with contextlib.suppress(Exception):
                await _wait_pf_ajax(page, timeout=15000)
            # Polling do cabecalho na janela principal — 15s
            # Usa o mesmo `js_detect_header` (3 padroes aceitos)
            with contextlib.suppress(PWTimeoutError, Exception):
                await page.wait_for_function(
                    js_detect_header,
                    timeout=15000,
                    polling=250,
                )
                tem_header = True
            await page.wait_for_timeout(500)

            if tem_header:
                _log.info(
                    "  fallback OK — janela principal carregou detalhamento de %s",
                    page.url,
                )
                page_detalhe = page
                sucesso = True
                break

            # Mesmo o fallback falhou — dumpa tudo pra diagnostico
            with contextlib.suppress(Exception):
                pasta_dbg = await _dump_debug_paj(
                    page,
                    paj_texto,
                    f"popup '{termo}' nao renderizou + fallback nao carregou cabecalho",
                    extras={
                        "termo": termo,
                        "popup_url_inicial": popup_url_inicial,
                        "popup_url_final": popup_url_final,
                        "url_alvo": url_alvo,
                        "page_url_pos_goto": page.url,
                    },
                )
                _log.warning("  dump salvo em %s", pasta_dbg)

            erro_ultimo = (
                f"popup '{termo}' nao renderizou e fallback (goto {url_alvo}) "
                f"tambem nao mostrou cabecalho"
            )
            debug_acumulado.append({"termo": termo, "popup_url": url_alvo})
            # Volta pra caixa antes de proxima tentativa
            with contextlib.suppress(Exception):
                await page.goto(
                    f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml",
                    wait_until="domcontentloaded",
                )
                await _wait_pf_ajax(page, timeout=10000)
            continue

        # Fallback: a janela atual pode ter navegado para o detalhamento
        # (caso raro, mas defensivo). Da uns segundos pro PrimeFaces AJAX.
        with contextlib.suppress(Exception):
            await _wait_pf_ajax(page, timeout=15000)
        await page.wait_for_timeout(2000)
        body = ""
        with contextlib.suppress(Exception):
            body = await page.inner_text("body")
        tem_header_detalhe = bool(re_pag_detalhe.search(body))
        _log.info(
            "  pos-pesquisa (janela atual) url=%s tem_header_detalhe=%s",
            page.url,
            tem_header_detalhe,
        )
        if tem_header_detalhe:
            page_detalhe = page
            sucesso = True
            break
        erro_ultimo = (
            f"pesquisa por '{termo}' nao abriu popup nem navegou — "
            f"PAJ pode nao existir nessa unidade/ano"
        )
        debug_acumulado.append({"termo": termo, "url": page.url})

        # Volta pra caixa antes da proxima tentativa
        with contextlib.suppress(Exception):
            await page.goto(
                f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml",
                wait_until="domcontentloaded",
            )
            await _wait_pf_ajax(page, timeout=10000)

    if not sucesso or page_detalhe is None:
        msg = erro_ultimo or "PAJ nao encontrado via pesquisa global"
        with contextlib.suppress(Exception):
            pasta = await _dump_debug_paj(
                page,
                paj_texto,
                msg,
                extras={"tentativas": debug_acumulado},
            )
            _log.warning("FIM(falha) debug salvo em: %s", pasta)
        return {"erro": msg}

    # A partir daqui a extracao roda em `page_detalhe`, que pode ser a janela
    # atual OU um popup aberto por window.open. Substituimos a referencia
    # `page` localmente para que o resto do codigo continue limpo.
    page = page_detalhe

    # Caso o popup tenha sido capturado, promove ele a "page global" — assim
    # operacoes posteriores que dependem de `_get_page()` (ex: listar_arquivos_paj
    # → baixar anexos) atuam no detalhamento do PAJ, e nao na caixa de entrada
    # antiga. Sem isso, sincronizar um PAJ da watchlist via busca global nao
    # conseguiria baixar anexos. Quando `page_detalhe` ja e' a propria `_page`
    # (busca navegou a janela atual em vez de abrir popup), a atribuicao e' no-op.
    global _page
    _page = page_detalhe

    # Atalho: quem só quer a URL de detalhe (ex.: montar link para abrir o PAJ no
    # navegador do usuário) retorna aqui, ANTES da extração pesada de cabeçalho e
    # movimentações. A URL do `detalhamentoProcesso` é stateless (só precisa do
    # cookie de sessão), então funciona em qualquer navegador logado no SISDPU.
    if somente_url:
        _log.info("FIM(somente_url) url=%s", page_detalhe.url)
        return {"url": page_detalhe.url, "paj": paj_texto}

    # Extrai os campos do cabecalho do detalhamento. Estrategia hibrida:
    # (1) DOM-based para label/valor (mais robusto que regex no innerText)
    # (2) regex residual para padroes posicionais (numero CNJ, linha PAJ).
    # (3) filtros para descartar valores que sao claramente cabecalhos de tabela.
    dados = await page.evaluate(
        """
        () => {
            const r = {};
            const body = document.body.innerText;

            // ---------- Helper: encontra valor adjacente a um label ----------
            // O regex inclui o ':' ASCII E o U+FF1A (fullwidth colon) porque labels
            // do SISDPU aparecem com qualquer um dos dois, dependendo da fonte.
            const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim()
                                          .toLowerCase().replace(/[:：]$/, '');

            // Lixo conhecido — cabecalhos de tabelas do SISDPU que aparecem
            // proximos a labels e foram capturados por engano antes.
            const LIXO_TABELA = /^(data de envio|remetente|prazo|descri[çc][ãa]o|tipo|status|data|seq|usu[áa]rio|fases|movimenta[çc][ãa]o|seq\\.?)\\b/i;

            function valorParaLabel(rotulos) {
                const alvos = rotulos.map(norm);
                const candidatos = document.querySelectorAll(
                    'td, th, label, span, dt, dd, li, b, strong, div, p'
                );
                for (const el of candidatos) {
                    const txt = norm(el.textContent || '');
                    if (!alvos.includes(txt)) continue;

                    // 1) Proximo sibling com texto util.
                    let next = el.nextElementSibling;
                    while (next) {
                        const v = (next.textContent || '').trim();
                        if (v) {
                            const linha = v.split('\\n')[0].trim();
                            if (linha && !LIXO_TABELA.test(linha) && linha.length < 250) {
                                return linha;
                            }
                        }
                        next = next.nextElementSibling;
                    }

                    // 2) Pai contendo label e valor juntos no innerText.
                    const pai = el.parentElement;
                    if (pai) {
                        const fullPai = (pai.textContent || '').trim();
                        const idx = fullPai.toLowerCase().indexOf(txt);
                        if (idx >= 0) {
                            const depois = fullPai.substring(idx + (el.textContent || '').length).trim();
                            const linha = depois.split('\\n')[0].trim().replace(/^[:\\-\\s]+/, '');
                            if (linha && !LIXO_TABELA.test(linha) && linha.length < 250) {
                                return linha;
                            }
                        }
                    }
                }
                return '';
            }

            // ---------- Campos do cabecalho (todos os que aparecem na aba Resumo) ----------
            r.assistido = valorParaLabel(['Assistido(s)', 'Assistido', 'Assistidos']);

            // Extrai CPF da tabela "Assistido(s) Pessoa(s) Física:"
            // Seletor: primeira linha de dados, terceira célula (coluna CPF)
            const cpfCell = document.querySelector(
                '#detalhamentoForm\\\\:dataTableListaAssistidosPF tbody tr td:nth-child(3)'
            );
            if (cpfCell) {
                const cpfText = cpfCell.textContent.trim().split('\\n')[0];
                if (cpfText && /\\d{3}\\.\\d{3}\\.\\d{3}-\\d{2}/.test(cpfText)) {
                    r.cpf_assistido = cpfText;
                }
            }

            r.pretensao = valorParaLabel(['Pretensão', 'Pretensao']);
            r.data_abertura = valorParaLabel([
                'Data de Abertura', 'Dt. Abertura', 'Dt Abertura', 'Dt.Abertura', 'Abertura'
            ]);
            r.oficio = valorParaLabel([
                'Ofício', 'Oficio', 'Ofício Responsável', 'Oficio Responsavel',
                'Ofício responsável', 'Ofício atual'
            ]);
            r.juizo = valorParaLabel([
                'Juízo/Orgão Julgador', 'Juizo/Orgao Julgador',
                'Juízo/Órgão Julgador', 'Juízo', 'Juizo',
                'Órgão Julgador', 'Orgao Julgador'
            ]);
            r.foro_detalhado = valorParaLabel([
                'Foro Detalhado', 'Foro detalhado', 'Foro'
            ]);
            r.decurso = valorParaLabel([
                'Decurso', 'Decurso agendado', 'Decurso Agendado'
            ]);

            // ---------- PAJ + status — duas variantes da tela ----------
            //  a) Formato compacto: "AAAA/UUU-NNNNN - PAJ STATUS"
            //  b) Formato tabular:  "Número do PAJ\tAAAA/UUU-NNNNN"  (nesta tela,
            //     o status vem em outra linha, ex: "Urgente" / "Tutela Coletiva".
            //     Deixamos status vazio se nao houver formato compacto.)
            let pajMatch = body.match(/(\\d{4}\\/\\d{3}-\\d+)\\s*-\\s*PAJ\\s+([^\\n]+)/);
            if (pajMatch) {
                r.paj = pajMatch[1];
                r.status_paj = pajMatch[2].trim();
            } else {
                pajMatch = body.match(/N[úu]mero\\s+do\\s+PAJ[\\s\\t]+(\\d{4}\\/\\d{3}-\\d+)/);
                if (pajMatch) {
                    r.paj = pajMatch[1];
                    r.status_paj = '';
                }
            }

            // ---------- Numero do processo judicial (padrao CNJ) ----------
            const procMatch = body.match(
                /(\\d{7}-\\d{2}\\.\\d{4}\\.\\d\\.\\d{2}\\.\\d{4})/
            );
            if (procMatch) r.processo_judicial = procMatch[1];

            // ---------- Movimentacoes (tabela; mesma logica de antes) ----------
            const movs = [];
            const movRows = document.querySelectorAll('table tbody tr');
            for (const tr of movRows) {
                const cells = tr.querySelectorAll('td');
                if (cells.length >= 8) {
                    const seq = (cells[0]?.textContent || '').trim();
                    const anexo = (cells[1]?.textContent || '').trim();
                    const dataHora = (cells[2]?.textContent || '').trim();
                    const movimentacao = (cells[3]?.textContent || '').trim();
                    const fases = (cells[4]?.textContent || '').trim();
                    const textoCompleto = cells[5]?.innerHTML || '';
                    const usuario = (cells[6]?.textContent || '').trim();

                    if (seq && /^\\d{1,4}$/.test(seq)
                        && (textoCompleto.trim() || fases)) {

                        // descricao_retorno será extraído em segunda passagem (após cliques)
                        let descricao_retorno = '';

                        movs.push({
                            seq: seq,
                            anexo: anexo,
                            data: dataHora,
                            movimentacao: movimentacao,
                            fases: fases,
                            descricao: textoCompleto,
                            links_decisao: [],
                            usuario: usuario,
                            descricao_retorno: descricao_retorno
                        });
                    }
                }
            }
            if (movs.length > 0) r.movimentacoes = movs.slice(0, 500);

            // ---------- Filtro final: descarta qualquer campo que tenha vazado lixo ----------
            for (const k of ['assistido', 'pretensao', 'oficio', 'juizo', 'foro_detalhado', 'decurso', 'data_abertura']) {
                if (r[k] && LIXO_TABELA.test(r[k])) {
                    r[k] = '';
                }
            }

            return r;
        }
        """
    )

    if not dados:
        dados = {}

    n_movs = len(dados.get("movimentacoes", []) or [])
    _log.info(
        "extracao: assistido=%s paj=%s movs=%d",
        bool(dados.get("assistido")),
        dados.get("paj", "?"),
        n_movs,
    )

    # Validacao rigorosa: precisa do `paj` extraido pelo regex
    # "AAAA/UUU-NNNNN - PAJ STATUS", que so' aparece na tela de detalhamento.
    # Sem ele, ainda estamos na caixa ou em pagina inesperada.
    if not dados.get("paj"):
        msg = (
            "extracao falhou — cabecalho do PAJ nao encontrado na pagina "
            "(provavelmente nao estamos na tela de detalhamento)"
        )
        with contextlib.suppress(Exception):
            pasta = await _dump_debug_paj(
                page,
                paj_texto,
                msg,
                extras={"dados_parciais": {k: v for k, v in dados.items() if k != "movimentacoes"}},
            )
            _log.warning("FIM(sem-paj) debug salvo em: %s", pasta)
        return {"erro": msg}

    dados["paj_numero"] = numero
    dados["paj_ano"] = ano
    dados["paj_unidade"] = unidade
    dados["url"] = page.url

    # ===== SEGUNDA PASSAGEM: Extrair descricao_retorno para "Atendimento de retorno" =====
    movimentacoes = dados.get("movimentacoes", []) or []

    # 1. Encontrar e clicar em TODOS os toggles de "Atendimento de retorno"
    await page.evaluate("""
        () => {
            const allTrs = document.querySelectorAll('table tbody tr');
            for (const tr of allTrs) {
                const cells = tr.querySelectorAll('td');
                const fases = (cells[4]?.textContent || '').trim();

                if (fases.includes('Atendimento de retorno')) {
                    const toggler = tr.querySelector('.ui-row-toggler');
                    if (toggler && toggler.getAttribute('aria-expanded') !== 'true') {
                        toggler.click();
                    }
                }
            }
        }
    """)

    # 2. Aguardar que as requisições AJAX completem
    await _wait_pf_ajax(page, timeout=5000)

    # 3. Extrair seq -> conteúdo direto em JavaScript
    mapa_conteudos = await page.evaluate("""
        () => {
            const resultado = {};
            const allTrs = document.querySelectorAll('table tbody tr');

            for (let i = 0; i < allTrs.length; i++) {
                const tr = allTrs[i];
                const cells = tr.querySelectorAll('td');
                const seq = (cells[0]?.textContent || '').trim();
                const fases = (cells[4]?.textContent || '').trim();

                // Se esta TR tem "Atendimento de retorno"
                if (fases.includes('Atendimento de retorno')) {
                    // Procurar pela próxima TR (pode haver várias)
                    for (let j = i + 1; j < Math.min(i + 5, allTrs.length); j++) {
                        const proximaTr = allTrs[j];

                        // Se encontrou uma TR expandida
                        if (proximaTr.classList.contains('ui-expanded-row-content')) {
                            const td = proximaTr.querySelector('td');
                            if (td) {
                                resultado[seq] = td.textContent.trim();
                            }
                            break;
                        }
                    }
                }
            }

            return resultado;
        }
    """)

    # 4. Aplicar os conteúdos extraídos às movimentações
    for mov in movimentacoes:
        if "Atendimento de retorno" in mov.get("fases", ""):
            seq = str(mov.get("seq", "")).strip()

            # Debug: testar diferentes formatos de chave
            encontrou = False
            for chave_mapa in mapa_conteudos.keys():
                if str(chave_mapa).strip() == seq:
                    mov["descricao_retorno"] = mapa_conteudos[chave_mapa]
                    encontrou = True
                    break

            # Fallback: tentar direto
            if not encontrou and seq in mapa_conteudos:
                mov["descricao_retorno"] = mapa_conteudos[seq]

    # Narrativa (HTML do painel) — extraida da MESMA pagina de detalhamento, sem
    # re-navegar. Evita uma 2a navegacao (detalhes_paj) na aba Retornos.
    with contextlib.suppress(Exception):
        narr = await page.evaluate(
            r"""() => {
                const n = document.querySelector('#detalhamentoForm\\:narrativaPanel');
                return n ? n.innerHTML : '';
            }"""
        )
        if narr:
            dados["narrativa"] = narr

    # Dump tambem em caso de sucesso durante esta fase de afinacao — ajuda
    # diagnosticar campos vazios sem precisar retentar. Pode ser desligado
    # depois que a extracao estiver estavel. No caminho rapido (Retornos), pulamos
    # este dump de disco por PAJ.
    if not esperas_curtas:
        with contextlib.suppress(Exception):
            pasta = await _dump_debug_paj(
                page,
                paj_texto,
                "sucesso (dump pra afinacao)",
                extras={
                    "campos_extraidos": {
                        k: v for k, v in dados.items() if k not in ("movimentacoes", "sisdpu_raw")
                    },
                },
            )
            _log.info("dump de sucesso salvo em: %s", pasta)
    _log.info("FIM(ok) %s", paj_texto)
    return dados


_EXTRACAO_DOCS_JS_PATH = Path(__file__).resolve().parent / "extrair_documentos_movimentacoes.js"


async def extrair_documentos_por_seq(
    seqs_conhecidos: list[str] | None = None,
) -> dict:
    """Extrai, por seq de movimentação, os documentos anexados (via o ícone de
    "Anexo" de cada linha) do PAJ atualmente aberto — usado pela feature
    "Baixar PAJ Completo" (`services/paj_completo_service.py`). Porte do script
    idêntico ao usado no PAINEL SISDPU 2.0
    (`ingestao/extrair_documentos_movimentacoes.js`), sem a lógica de cache de
    seqs já conhecidas entre sincronizações (o Painel SAT Central gera o PDF
    sob demanda, sem sincronização prévia persistida — sempre reextrai tudo).

    PRE-CONDICAO: a page compartilhada já deve estar no detalhamento do PAJ
    (ex.: logo após `buscar_paj_global(..., forcar_sessao=False)`).

    Pilota o script passo a passo: INIT (monta a fila de seqs com anexo) -> N
    chamadas de `__extracaoProximo()` (uma por seq, clica/lê/fecha o popup) ->
    `__extracaoFinalizar()` (consolida). Retorna dict com
    `movimentacoes_com_documentos`, `total_documentos`, `seqsPuladas`, etc."""
    page = await _get_page()
    js_source = _EXTRACAO_DOCS_JS_PATH.read_text(encoding="utf-8")
    await page.evaluate("(src) => { (0, eval)(src); }", js_source)
    init = await page.evaluate("(seqs) => window.__extracaoInit(seqs)", list(seqs_conhecidos or []))
    if init.get("erro"):
        return {"erro": init["erro"], "movimentacoes_com_documentos": [], "total_documentos": 0}
    total_fila = init.get("totalFila", 0)
    if total_fila == 0:
        return await page.evaluate("window.__extracaoFinalizar()")
    while True:
        r = await page.evaluate("window.__extracaoProximo()")
        if r.get("fim"):
            break
    return await page.evaluate("window.__extracaoFinalizar()")


async def baixar_arquivo_direto(url: str, destino: Path) -> int:
    """Baixa um documento com link de download direto
    (`/rest/download/pdf/{id}/{nome}`), reaproveitando os cookies da sessão
    Playwright via `page.context.request` (sem precisar do servidor HTTP
    local auxiliar usado no PAINEL SISDPU 2.0). Retorna o tamanho em bytes
    salvo (0 em falha)."""
    page = await _get_page()
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    resp = await page.context.request.get(url)
    if not resp.ok:
        return 0
    corpo = await resp.body()
    destino.write_bytes(corpo)
    return len(corpo)


async def _abrir_popup_documentos_seq(page: Page, seq: str) -> bool:
    """Clica no ícone de anexo da linha `seq` da tabela de movimentações e
    espera o popup de documentos ('Arquivo' no cabeçalho) abrir e ficar
    visível. PRE-CONDICAO: a page já está no detalhamento do PAJ. Retorna
    True se o popup abriu."""
    clicou = await page.evaluate(
        """
        (seqAlvo) => {
            const tables = document.querySelectorAll('table');
            let tableMov = null;
            for (const t of tables) {
                const headers = Array.from(t.querySelectorAll('thead th, tbody tr:first-child td'))
                    .map(h => h.textContent.trim()).filter(Boolean);
                if (headers.includes('Seq.') && headers.includes('Data/Hora')) { tableMov = t; break; }
            }
            if (!tableMov) return false;
            const rows = tableMov.querySelectorAll('tbody tr');
            for (const row of rows) {
                const cells = row.querySelectorAll('td');
                if (!cells.length) continue;
                if ((cells[0]?.textContent || '').trim() !== String(seqAlvo)) continue;
                const clickable = cells[1]?.querySelector('a, button, [role="button"], [onclick], img[onclick], span[onclick]');
                if (!clickable) return false;
                clickable.click();
                return true;
            }
            return false;
        }
        """,
        seq,
    )
    if not clicou:
        return False
    try:
        await page.wait_for_function(
            """
            () => {
                const tables = document.querySelectorAll('table');
                for (let j = tables.length - 1; j >= Math.max(0, tables.length - 40); j--) {
                    const headers = Array.from(tables[j].querySelectorAll('thead th, tbody tr:first-child td'))
                        .map(h => h.textContent.trim()).filter(Boolean);
                    if (headers.includes('Arquivo') && tables[j].offsetParent !== null) return true;
                }
                return false;
            }
            """,
            timeout=10000,
        )
    except PWTimeoutError:
        return False
    return True


async def _fechar_popup_documentos(page: Page) -> None:
    with contextlib.suppress(Exception):
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(400)


async def baixar_documento_via_commandlink(seq: str, nome_arquivo: str, destino: Path) -> Path | None:
    """Baixa UM documento sem link de download direto (anexos tipo imagem
    "Documentos do Assistido", e petições .doc/.docx) — vêm como
    `<p:commandLink>` do PrimeFaces (href="#" + clique dispara um POST do
    formulário inteiro), então um GET simples não funciona. Clica DE VERDADE
    no elemento (localizado pelo NOME do arquivo — o id do elemento é
    regenerado a cada reabertura do popup, não é estável) e captura o evento
    `download` do Playwright. PRE-CONDICAO: a page já está no detalhamento
    do PAJ — abre e fecha o popup da seq sozinho."""
    page = await _get_page()
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)

    if not await _abrir_popup_documentos_seq(page, seq):
        return None

    try:
        async with page.expect_event("download", timeout=30000) as dl_info:
            clicou = await page.evaluate(
                """
                (nomeAlvo) => {
                    const normalizar = (s) => (s || '')
                        .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
                        .replace(/\\s+/g, ' ').trim().toLowerCase();
                    const alvo = normalizar(nomeAlvo);
                    const tables = document.querySelectorAll('table');
                    for (const t of tables) {
                        const headers = Array.from(t.querySelectorAll('thead th')).map(h => h.textContent.trim());
                        const idxArquivo = headers.findIndex(h => h.toLowerCase() === 'arquivo');
                        if (idxArquivo === -1) continue;
                        const rows = Array.from(t.querySelectorAll('tbody tr'));
                        for (const row of rows) {
                            const cells = row.querySelectorAll('td');
                            if (!cells.length || !cells[idxArquivo]) continue;
                            if (normalizar(cells[idxArquivo].textContent) !== alvo) continue;
                            const link = row.querySelector('a');
                            if (link && link.getAttribute('href') === '#') { link.click(); return true; }
                        }
                        const semLinkDireto = rows
                            .map((row) => row.querySelector('a[href="#"]'))
                            .filter(Boolean);
                        if (semLinkDireto.length === 1) { semLinkDireto[0].click(); return true; }
                    }
                    return false;
                }
                """,
                nome_arquivo,
            )
            if not clicou:
                raise RuntimeError(f"link do documento '{nome_arquivo}' não encontrado no popup")
        download = await dl_info.value
        await download.save_as(str(destino))
    except Exception:
        await _fechar_popup_documentos(page)
        return None

    await _fechar_popup_documentos(page)
    if destino.exists() and destino.stat().st_size > 0:
        return destino
    return None


async def fechar() -> None:
    """Fecha o navegador headless."""
    global _playwright, _browser, _page
    if _browser:
        with contextlib.suppress(Exception):
            await _browser.close()
        _browser = None
        _page = None
    if _playwright:
        with contextlib.suppress(Exception):
            await _playwright.stop()
        _playwright = None


async def reconectar() -> dict:
    """Força uma RECONEXÃO no SISDPU — recupera de conflito de sessão / sessão
    derrubada por outro login. Fecha o navegador headless atual e refaz o login
    com as credenciais do cofre. Ao criar uma sessão nova, ela "assume a vez",
    derrubando a sessão concorrente do mesmo usuário.
    Retorna {ok, status, detalhe}."""
    global _browser, _page
    with contextlib.suppress(Exception):
        if _browser:
            await _browser.close()
    _browser = None
    _page = None
    try:
        await _login()
        page = await _get_page()
        if "login" in (page.url or "").lower():
            return {"ok": False, "status": "credenciais",
                    "detalhe": "O SISDPU recusou o usuário/senha ao reconectar. "
                               "Confira as credenciais cadastradas no cofre "
                               "(Configurações)."}
    except CredenciaisInvalidas as e:
        return {"ok": False, "status": "credenciais",
                "detalhe": f"O SISDPU recusou o usuário/senha ao reconectar: {e}"}
    except RuntimeError as e:
        return {"ok": False, "status": "indisponivel", "detalhe": str(e)}
    except Exception as e:  # noqa: BLE001
        with contextlib.suppress(Exception):
            from services import conexao_service
            conexao_service.marcar_erro(str(e))
        return {"ok": False, "status": "erro",
                "detalhe": f"Não foi possível reconectar ({type(e).__name__}: {e})."}
    return {"ok": True, "status": "ok",
            "detalhe": "Reconectado ao SISDPU — sessão do painel reativada."}


if __name__ == "__main__":
    import asyncio
    import json

    async def _smoke() -> None:
        # Requer o cofre já destrancado nesta sessão (rode o painel e desbloqueie
        # pela UI antes de testar este módulo isoladamente).
        try:
            data = await caixa_de_entrada()
            itens = data.get("itens_tabela", [])[:5]
            print(json.dumps(itens, ensure_ascii=False, indent=2))
            print(f"(+{len(data.get('itens_tabela', [])) - len(itens)} itens)")
        finally:
            await fechar()

    asyncio.run(_smoke())
