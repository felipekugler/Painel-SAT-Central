"""Movimentar / Tramitar / Concluir PAJ no SISDPU (Playwright headless, async).

Portado do PAINEL COMUNICAÇÕES (`ingestao/sisdpu_client.py`), onde o mecanismo foi
mapeado e validado em teste real. Aqui reusa a infra de sessão/login do
`ingestao.sisdpu_client` deste painel (browser/page globais, login com as
credenciais do cofre) — não abre um segundo browser.

Funções públicas:
- `listar_caixa_estruturada()` — lê a caixa de entrada do usuário por linha, com
  `id_tramite` (data-rk), `id` (id_processo), `paj`, `assistido`, `descricao`.
- `movimentar_paj(pajs, descricao, fase, anexos, tipo_arquivo, dry_run)`.
- `tramitar_paj(pajs, grupo, usuarios, descricao, prazo_data, prazo_desc, dry_run)`.
- `concluir_paj(id_tramites, dry_run)` — admite na caixa de grupo + conclui.
- `listar_grupos_unidade(id_processo)` — lê o dropdown de grupos da unidade.
"""

from __future__ import annotations

import contextlib
import mimetypes
import re
import unicodedata
from pathlib import Path

from ingestao import sisdpu_client as _sc

# --- infra reusada do cliente deste painel (mesmo browser/page globais) ---
_sel = _sc._sel
SISDPU_URL = _sc.SISDPU_URL
_get_page = _sc._get_page
_ensure_logged_in = _sc._ensure_logged_in
_wait_pf_ajax = _sc._wait_pf_ajax
_log = _sc._log

# --- constantes de formulário / negócio ---
_MOV_FORM = "formHonorario"
_TRAM_FORM = "form_tramite"
_TIPOS_ANEXO_OK = {"doc", "docx", "odt", "png", "jpg", "jpeg", "gif", "pdf"}
_MSG_CONFLITO_SESSAO = "Conflito de sessão do SISDPU — feche a outra sessão e tente de novo."


# ---------------------------------------------------------------------------
# JS auxiliares
# ---------------------------------------------------------------------------

# Seleciona <option> por TEXTO num <select> nativo (.epaj_select_formata) + dispara
# 'change' (o AJAX do PrimeFaces reage). args = [selectId, textoAlvo, exato].
_JS_SELECT_OPT = r"""
(args) => {
  const [selId, alvo, exato] = args;
  const s = document.getElementById(selId);
  if (!s) return {ok:false, err:'select-inexistente'};
  const n = t => (t||'').normalize('NFD').replace(/[̀-ͯ]/g,'').toLowerCase().trim();
  let val=null, txt=null;
  for (const o of s.options){ if (n(o.text)===n(alvo)){ val=o.value; txt=o.text.trim(); break; } }
  if (val===null && !exato) for (const o of s.options){ if (n(o.text).includes(n(alvo))){ val=o.value; txt=o.text.trim(); break; } }
  if (val===null) return {ok:false, opts:[...s.options].map(o=>o.text.trim())};
  s.value = val; s.dispatchEvent(new Event('change', {bubbles:true}));
  return {ok:true, val, txt};
}
"""

# Escreve na descrição (CKEditor) + input/textarea subjacentes. args=[txt, hidId, taId, raw].
_JS_SET_CKEDITOR = r"""
(args) => {
  const [txt, hidId, taId, raw] = args;
  const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const html = raw ? (txt||'') : esc(txt).replace(/\r\n|\r|\n/g, '<br>');
  let done=false, keys=[];
  if (window.CKEDITOR && CKEDITOR.instances) {
    keys = Object.keys(CKEDITOR.instances);
    for (const nm of keys) {
      if (nm.includes('editorValue')) {
        CKEDITOR.instances[nm].setData(html);
        try { CKEDITOR.instances[nm].updateElement(); } catch(e) {}
        done = true;
      }
    }
  }
  const hid = document.getElementById(hidId); if (hid) hid.value = html;
  const ta  = document.getElementById(taId);  if (ta)  ta.value  = html;
  return {done, keys};
}
"""

# Marca o checkbox "Todas as fases" (revela fases fora da lista curta). Por TEXTO do label.
_JS_MARCAR_TODAS_FASES = r"""
() => {
  const l = [...document.querySelectorAll('label')].find(x => /todas as fases/i.test(x.innerText||''));
  if (!l) return false;
  const td = l.closest('td') || l.parentElement;
  const box = td && td.querySelector('.ui-chkbox-box');
  if (!box) return false;
  const icon = box.querySelector('.ui-chkbox-icon');
  const jaMarcado = icon && /ui-icon-check/.test(icon.className);
  if (!jaMarcado) box.click();
  return true;
}
"""

# Maior nº de sequência de movimentação no detalhamento (para reportar "Mov X").
_JS_MAX_SEQ_MOV = r"""
() => {
  let max = null;
  for (const tr of document.querySelectorAll("table tbody tr")) {
    const cells = tr.querySelectorAll(":scope > td");
    if (cells.length < 6) continue;
    const seq = (cells[0].textContent || "").trim();
    const data = (cells[2].textContent || "").trim();
    if (!/^\d{1,4}$/.test(seq)) continue;
    if (!/\d{2}\/\d{2}\/\d{4}/.test(data)) continue;
    const n = parseInt(seq, 10);
    if (max === null || n > max) max = n;
  }
  return max;
}
"""

# Conflito de sessão (1 sessão por usuário): diálogo 'confirmDialogLogin' visível.
_JS_CONFLITO_LOGIN = r"""
() => !!document.querySelector('[id$="confirmDialogLogin_modal"]')
      || [...document.querySelectorAll('[id$="confirmDialogLogin"]')].some(d => d.offsetParent !== null)
"""

# --- Concluir: admitir (caixa de grupo) + concluir (caixa do usuário) ---
_JS_ATIVA_ABA_GRUPO = r"""() => {
    const links = document.querySelectorAll('.ui-tabs-nav a, a[href*="tabView:tab"]');
    for (const a of links) if ((a.textContent||'').toLowerCase().includes('caixa de entrada do grupo')) { a.click(); return true; }
    return false;
}"""
_JS_TRAMITE_NO_GRUPO = r"""(idt) => {
    for (const tr of document.querySelectorAll('tr[data-rk="'+idt+'"]')) {
        for (const a of tr.querySelectorAll('a, button'))
            if ((a.textContent||'').replace(/\s+/g,' ').trim().toLowerCase() === 'admitir') return true;
    }
    return false;
}"""
_JS_ADMITIR_TRAMITE = r"""(idt) => {
    for (const tr of document.querySelectorAll('tr[data-rk="'+idt+'"]')) {
        for (const a of tr.querySelectorAll('a, button'))
            if ((a.textContent||'').replace(/\s+/g,' ').trim().toLowerCase() === 'admitir') { a.click(); return true; }
    }
    return false;
}"""
_JS_TRAMITE_NA_CAIXA = r"""(idt) => {
    const t = document.querySelector('[id="tabView:caixaEntradaTab1Form:tramites"]');
    return !!(t && t.querySelector('tbody tr[data-rk="'+idt+'"]'));
}"""
_JS_MARCA_CHECKBOX = r"""(idt) => {
    const box = document.querySelector('[id="tabView:caixaEntradaTab1Form:tramites_'+idt+'_checkbox"]');
    if (!box) return false;
    if (!/ui-state-active/.test(box.getAttribute('class')||'')) box.click();
    return true;
}"""
_JS_CLICA_BTN_CONCLUIR = r"""() => {
    const b = document.querySelector('[id="tabView:caixaEntradaTab1Form:btnConcluir"]');
    if (!b) return false; b.click(); return true;
}"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _norm_nome(s: str) -> str:
    """Normaliza p/ comparar: sem acentos, MAIÚSCULO, sem '(N PAJ'S)', 1 espaço."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\(\s*\d+\s*paj.*?\)", "", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip().upper()


async def _eval_seguro(page, js: str, arg=None, tentativas: int = 5) -> dict:
    """page.evaluate tolerante a navegação (o contexto pode ser destruído por postback
    JSF no meio do evaluate) — re-tenta após a navegação assentar. `arg`, se informado
    (!= None), é repassado como argumento da função JS (ex.: nome da caixa de grupo)."""
    ultimo = None
    for _ in range(tentativas):
        try:
            return await (page.evaluate(js, arg) if arg is not None else page.evaluate(js))
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if "context was destroyed" in msg or "navigation" in msg or "execution context" in msg:
                ultimo = e
                with contextlib.suppress(Exception):
                    await page.wait_for_load_state("domcontentloaded", timeout=8000)
                await page.wait_for_timeout(500)
                continue
            raise
    if ultimo:
        raise ultimo
    return await (page.evaluate(js, arg) if arg is not None else page.evaluate(js))


async def _resolver_conflito_login(page) -> bool:
    """Compatível com o uso antigo (só `if not await _resolver_conflito_login(page): ...`).
    Veja `_resolver_conflito_login2` — usa aqui o mesmo resultado, descartando o 2º valor."""
    ok, _havia = await _resolver_conflito_login2(page)
    return ok


async def _resolver_conflito_login2(page) -> tuple[bool, bool]:
    """Se o diálogo de conflito de sessão estiver VISÍVEL (outra aba/computador logado
    com o MESMO usuário — comum quando o Defensor acompanha o SISDPU na própria janela
    enquanto o painel também usa a sessão headless), clica 'Desbloquear' (assume a
    sessão) e aguarda a página assentar de verdade (AJAX do PrimeFaces, não só o modal
    sumir). Retorna (ok, havia_conflito): `ok` = False só se o conflito ficou sem
    resolver; `havia_conflito` = True sempre que um conflito foi detectado (resolvido ou
    não) — o chamador deve então RELER a página do zero (não confiar no DOM atual), pois
    o "Desbloquear" pode ter deixado a tela num estado intermediário (ex.: uma tabela de
    caixa de grupo lida nesse instante aparecia com só 1 de 5 PAJs, sem nenhum erro —
    bug corrigido em 2026-11-10)."""
    conflito = await _eval_seguro(page, _JS_CONFLITO_LOGIN)
    if not conflito:
        with contextlib.suppress(Exception):
            from services import conexao_service
            conexao_service.marcar_conectado()
        return True, False
    _log.info("movtram: conflito de sessão detectado — clicando 'Desbloquear'.")
    btn = page.locator('[id$="confirmTramitacao"]').first
    with contextlib.suppress(Exception):
        if await btn.count():
            await btn.click(timeout=5000)
    with contextlib.suppress(Exception):
        await page.wait_for_selector('[id$="confirmDialogLogin_modal"]', state="hidden", timeout=8000)
    # O "Desbloquear" dispara um postback do PrimeFaces que pode demorar bem mais que
    # os antigos 300ms fixos — espera o AJAX de verdade assentar antes de confiar no DOM.
    with contextlib.suppress(Exception):
        await _wait_pf_ajax(page, timeout=10000)
    await page.wait_for_timeout(800)
    resolvido = not await _eval_seguro(page, _JS_CONFLITO_LOGIN)
    with contextlib.suppress(Exception):
        from services import conexao_service
        conexao_service.marcar_conectado() if resolvido else conexao_service.marcar_conflito(_MSG_CONFLITO_SESSAO)
    return resolvido, True


async def _ler_pajs_lista(page, input_id: str) -> list[str]:
    return await page.evaluate(
        """(id)=>{const s=document.getElementById(id);return s?[...s.options].map(o=>o.text.trim()).filter(Boolean):[];}""",
        input_id,
    )


async def _dialogos_visiveis(page) -> list[dict]:
    return await _eval_seguro(page, r"""
    () => {
      const vis = d => { const s=getComputedStyle(d); const r=d.getBoundingClientRect();
        return s.display!=='none' && s.visibility!=='hidden' && r.width>0 && r.height>0; };
      return [...document.querySelectorAll('.ui-confirm-dialog,.ui-dialog')].filter(vis).map(d=>({
        id: d.id,
        msg: ((d.querySelector('.ui-confirm-dialog-message,.ui-dialog-content')||{}).innerText||'').trim().slice(0,180),
        btns: [...d.querySelectorAll('button, a.ui-button')].filter(vis).map(b=>(b.innerText||'').trim()).filter(Boolean)
      }));
    }
    """)


async def _adicionar_paj_por_numero(page, form: str, add_btn_id: str, numero: str) -> None:
    ano, resto = numero.split("/")
    _unidade, num = resto.split("-")
    await page.evaluate(_JS_SELECT_OPT, [f"{form}:data", ano, True])
    await page.fill(_sel(f"{form}:numProcesso"), num)
    with contextlib.suppress(Exception):
        await page.locator(_sel(add_btn_id)).first.click(timeout=6000)
    await _wait_pf_ajax(page)
    await page.wait_for_timeout(600)


async def _ler_seq_mov(page, id_processo: str) -> int | None:
    """Lê o nº de sequência da movimentação recém-criada (maior seq no detalhamento)."""
    try:
        url = f"{SISDPU_URL}/pages/atendimento/detalhamentoProcesso.xhtml?id={id_processo}"
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=10000)
        await page.wait_for_timeout(300)
        seq = await _eval_seguro(page, _JS_MAX_SEQ_MOV)
        return int(seq) if seq is not None else None
    except Exception:  # noqa: BLE001
        return None


async def _anexar_documentos_movimentacao(page, anexos: list[dict], tipo_arquivo: str) -> list[str]:
    """Anexa documentos à MOVIMENTAÇÃO (popup 'dlgModalArquivos' da fase). Para cada
    anexo: abre o popup, sobe o arquivo (buffer, preserva nome), Descrição = nome,
    Tipo de Arquivo = `tipo_arquivo`, 'Anexar'. Fecha o popup ao fim. Best-effort."""
    anexos = [a for a in (anexos or []) if a]
    if not anexos:
        return []
    anexados: list[str] = []
    with contextlib.suppress(Exception):
        await page.get_by_text("Adicionar Anexo", exact=True).first.click()
    with contextlib.suppress(Exception):
        await page.wait_for_selector(_sel(f"{_MOV_FORM}:arquivosFase_input"), timeout=10000)
    await page.wait_for_timeout(400)
    for a in anexos:
        caminho = a.get("caminho") or ""
        nome = a.get("nome") or (Path(caminho).name if caminho else "documento")
        if not caminho or not Path(caminho).exists():
            _log.warning("mov anexo ausente no disco, pulado: %s (%s)", nome, caminho)
            continue
        ext = (a.get("tipo") or Path(nome).suffix).lstrip(".").lower()
        if ext not in _TIPOS_ANEXO_OK:
            _log.warning("mov anexo de tipo não permitido, pulado: %s (.%s)", nome, ext)
            continue
        try:
            data = Path(caminho).read_bytes()
            mime = mimetypes.guess_type(nome)[0] or "application/octet-stream"
            await page.locator(_sel(f"{_MOV_FORM}:arquivosFase_input")).set_input_files(
                [{"name": nome, "mimeType": mime, "buffer": data}])
            desc_sel = _sel(f"{_MOV_FORM}:arquivosSalvar:0:desc")
            await page.wait_for_selector(desc_sel, timeout=10000)
            with contextlib.suppress(Exception):
                await page.fill(desc_sel, nome)
            tp = await page.evaluate(
                _JS_SELECT_OPT, [f"{_MOV_FORM}:arquivosSalvar:0:tipoArquivo", tipo_arquivo, False])
            if not tp.get("ok"):
                _log.warning("mov anexo: tipo %r não achado (opts=%s)", tipo_arquivo, tp.get("opts"))
            with contextlib.suppress(Exception):
                await page.locator(
                    'button[name^="%s:arquivosSalvar"]' % _MOV_FORM, has_text="Anexar").first.click()
            await _wait_pf_ajax(page)
            await page.wait_for_timeout(500)
            anexados.append(nome)
            _log.info("mov anexo incluído: %s (tipo=%s)", nome, tipo_arquivo)
        except Exception as e:  # noqa: BLE001
            _log.warning("mov falha ao anexar %s: %r", nome, e)
    with contextlib.suppress(Exception):
        fechar = page.locator('button[name^="%s:"]' % _MOV_FORM, has_text="Fechar").first
        if await fechar.is_visible():
            await fechar.click()
            await page.wait_for_timeout(400)
    with contextlib.suppress(Exception):
        aberto = await page.evaluate(
            """()=>{const d=document.getElementById('%s:dlgModalArquivos');
                if(!d)return false;const s=getComputedStyle(d);const r=d.getBoundingClientRect();
                return s.display!=='none'&&r.width>0;}""" % _MOV_FORM)
        if aberto:
            await page.evaluate(
                "()=>{try{PF('dlgModalArquivosWidget').hide();}catch(e){} "
                "for(const w in (PrimeFaces.widgets||{})){try{if(/dlgModalArquivos/i.test(w))PrimeFaces.widgets[w].hide();}catch(e){}}}")
            await page.wait_for_timeout(300)
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(200)
    return anexados


# ---------------------------------------------------------------------------
# Leitura estruturada de UMA caixa de entrada de GRUPO (ex.: "1 - PREV - SAT"), usada
# pela fila automática — a origem dos PAJs é uma caixa de grupo específica, NÃO a caixa
# pessoal do usuário. Porta de Painel Comunicações (ingestao/sisdpu_client.py), mesmo
# padrão validado: a aba "Caixa de Entrada do Grupo" empilha um datatable por caixa de
# grupo a que o usuário tem acesso, cada um com cabeçalho <strong>NOME DO GRUPO</strong>.
# ---------------------------------------------------------------------------
CAIXA_GRUPO_SEED = "(descoberta de caixas)"  # não casa com nenhuma caixa real — só para
                                             # harvestar grupos_disponiveis (descoberta)

_JS_CAIXA_GRUPO = r"""(nomeGrupo) => {
    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const alvo = norm(nomeGrupo);
    const headers = Array.from(document.querySelectorAll('.ui-datatable-header'));
    const disponiveis = [];
    let tabelaAlvo = null;
    let matchExato = false;
    for (const h of headers) {
        const strong = h.querySelector('strong');
        const nome = strong ? strong.textContent.trim() : '';
        if (!nome) continue;
        disponiveis.push(nome);
        if (matchExato) continue;
        if (norm(nome) === alvo) { tabelaAlvo = h.closest('.ui-datatable'); matchExato = true; }
        else if (tabelaAlvo === null && norm(nome).includes(alvo)) { tabelaAlvo = h.closest('.ui-datatable'); }
    }
    const itens = [];
    if (tabelaAlvo) {
        for (const tr of tabelaAlvo.querySelectorAll('tbody tr[data-rk]')) {
            const idTramite = tr.getAttribute('data-rk') || '';
            const tds = Array.from(tr.querySelectorAll(':scope > td'));
            let pIdx = -1, linkDet = null;
            for (let i = 0; i < tds.length; i++) {
                const a = tds[i].querySelector('a[href*="detalhamentoProcesso"]');
                if (a) { pIdx = i; linkDet = a; break; }
            }
            if (pIdx < 0) continue;
            const textoProc = (linkDet.textContent || '').replace(/\s+/g, ' ').trim();
            const mPaj = textoProc.match(/(\d{4}\/\d{3}-\d+)\s*(.*)$/);
            const paj = mPaj ? mPaj[1] : '';
            let oficio = mPaj ? mPaj[2].trim() : '';
            oficio = oficio.replace(/^\(\s*/, '').replace(/\s*\)$/, '').trim();
            const href = linkDet.getAttribute('href') || '';
            const mId = href.match(/[?&]id=(\d+)/);
            const idProc = mId ? mId[1] : '';
            const urlDet = href ? (href.startsWith('http') ? href : (location.origin + href)) : '';
            const cellText = (i) => (tds[i] ? (tds[i].textContent || '').replace(/\s+/g, ' ').trim() : '');
            const cellRich = (i) => {
                const td = tds[i]; if (!td) return '';
                const txt = (td.textContent || '').replace(/\s+/g, ' ').trim();
                const ttl = (td.getAttribute('title') || '').replace(/\s+/g, ' ').trim();
                return /\d{2}:\d{2}/.test(ttl) ? ttl : txt;
            };
            itens.push({
                id_tramite: idTramite, id: idProc, paj: paj, oficio_origem: oficio,
                remetente: cellText(pIdx + 1), data_envio: cellRich(pIdx + 2),
                prazo: cellText(pIdx + 3), url_detalhe: urlDet,
                linha_texto: tds.map(td => (td.textContent || '').replace(/\s+/g, ' ').trim())
                               .filter(Boolean).join(' | '),
            });
        }
    }
    return { encontrado: tabelaAlvo !== null, grupos_disponiveis: disponiveis, itens: itens };
}"""

# Lê o popup "Detalhes do trâmite" (já aberto) — a linha da caixa de GRUPO não traz a
# descrição do trâmite nem o assistido (só Processo/Remetente/Data de Envio/Prazo); a
# descrição do trâmite só existe aqui (não é usada pela fila automática — basta o PAJ
# estar na caixa —, mas fica disponível via `enriquecer=True` para outros usos).
_JS_POPUP_TRAMITE = r"""() => {
    const d = document.querySelector('[id="formC:tramiteDetalhamentoDlg"]');
    if (!d) return {};
    const t = (d.innerText || '').replace(/\r/g, '');
    const dt = t.match(/(\d{2}\/\d{2}\/\d{4}\s+\d{2}:\d{2}:\d{2})/);
    const ehRotulo = (v) => /^(Assistido|Atribui[çc][ãa]o|Data do tr[âa]mite|Situa[çc][ãa]o|Órg[ãa]o)\s*(\(s\))?\s*:/i.test(v);
    const pega = (rot) => {
        const m = t.match(new RegExp(rot + '\\s*:?\\s*\\n\\s*([^\\n]+)', 'i'));
        if (!m) return '';
        const v = m[1].trim();
        return ehRotulo(v) ? '' : v;
    };
    return {
        data_envio: dt ? dt[1] : '',
        descricao_tramite: pega('Descri[çc][ãa]o do tr[âa]mite'),
        atribuicao: pega('Atribui[çc][ãa]o'),
    };
}"""


async def _ler_popup_tramite(page, id_tramite: str) -> dict:
    """Abre o popup "Detalhes do trâmite" de UMA tramitação (clique no ícone da coluna
    Detalhes), lê data-hora/descrição/atribuição e FECHA o diálogo (é modal, senão
    bloqueia o próximo clique). Best-effort: erro → {}."""
    clicou = await page.evaluate(
        """(idt) => {
            const tr = document.querySelector('tr[data-rk="' + idt + '"]');
            if (!tr) return false;
            const tds = [...tr.querySelectorAll(':scope > td')];
            const a = tds[1] && tds[1].querySelector('a');
            if (!a) return false;
            a.click();
            return true;
        }""",
        id_tramite,
    )
    if not clicou:
        return {}
    await _wait_pf_ajax(page, timeout=10000)
    await page.wait_for_timeout(500)
    dados = await page.evaluate(_JS_POPUP_TRAMITE)
    with contextlib.suppress(Exception):
        await page.evaluate(
            """() => {
                const d = document.querySelector('[id="formC:tramiteDetalhamentoDlg"]');
                const x = d && d.querySelector('.ui-dialog-titlebar-close');
                if (x) x.click();
            }"""
        )
    await page.wait_for_timeout(300)
    return dados or {}


async def listar_caixa_grupo(nome_grupo: str = CAIXA_GRUPO_SEED, enriquecer: bool = False) -> dict:
    """Lista os PAJs de UMA caixa de entrada de GRUPO (ex.: "1 - PREV - SAT"), na aba
    "Caixa de Entrada do Grupo" do SISDPU — distinta da caixa pessoal do usuário. Essa
    aba empilha um datatable por caixa de grupo a que o usuário tem acesso, cada um com
    cabeçalho <strong>NOME DO GRUPO</strong>; localiza o datatable cujo cabeçalho casa
    com `nome_grupo` (match exato preferencial, senão substring) e extrai as linhas.

    A linha NÃO traz a descrição do trâmite nem o assistido (só Processo/Remetente/Data
    de Envio/Prazo) — a fila automática não precisa de nenhum dos dois (basta o PAJ
    estar na caixa). `enriquecer=True` abre o popup "Detalhes do trâmite" de CADA item
    para completar essa descrição, se algum outro uso precisar dela — custa ~1-2s/item.

    Retorna {ok, encontrado, grupos_disponiveis, itens, detalhe}. `grupos_disponiveis`
    traz TODOS os nomes de caixa de grupo vistos na página (descoberta), independente de
    `nome_grupo` ter sido encontrado."""
    await _ensure_logged_in()
    # PAGE_LOCK: a page do SISDPU é compartilhada (fila automática, "Sincronizar caixa"
    # manual, "Adicionar PAJ" etc. podem rodar ao mesmo tempo) — sem o lock, uma chamada
    # concorrente podia navegar a page no meio desta leitura e corromper o resultado
    # (ex.: a tabela voltando com só 1 PAJ em vez dos 5 que estavam na caixa).
    async with _sc.PAGE_LOCK:
        page = await _get_page()
        itens: list[dict] = []
        grupos: list[str] = []
        encontrado = False
        # Até 2 tentativas: se um conflito de sessão (outro login simultâneo com o MESMO
        # usuário — ex.: o Defensor acompanhando o SISDPU na própria janela) for resolvido
        # NA 1ª passada, a página pode ter ficado num estado intermediário (o "Desbloquear"
        # dispara um postback) — descarta essa leitura e relê do zero, em vez de confiar
        # num DOM que pode estar incompleto (ex.: a tabela "sumindo" PAJs sem erro nenhum).
        for tentativa in range(2):
            await page.goto(f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml",
                            wait_until="domcontentloaded", timeout=30000)
            await _wait_pf_ajax(page, timeout=10000)
            await page.wait_for_timeout(800)
            ok_conflito, havia_conflito = await _resolver_conflito_login2(page)
            if not ok_conflito:
                return {"ok": False, "detalhe": _MSG_CONFLITO_SESSAO, "encontrado": False,
                        "grupos_disponiveis": [], "itens": []}
            await page.evaluate(_JS_ATIVA_ABA_GRUPO)
            await _wait_pf_ajax(page, timeout=10000)
            await page.wait_for_timeout(1200)

            resultado = await _eval_seguro(page, _JS_CAIXA_GRUPO, nome_grupo or CAIXA_GRUPO_SEED)
            resultado = resultado or {}
            itens = resultado.get("itens", []) or []
            grupos = resultado.get("grupos_disponiveis", []) or []
            encontrado = bool(resultado.get("encontrado"))
            if not havia_conflito or tentativa == 1:
                break
            _log.info("listar_caixa_grupo: houve conflito de sessão durante a leitura — relendo do zero.")

        if enriquecer:
            for it in itens:
                if not it.get("id_tramite"):
                    continue
                try:
                    extra = await _ler_popup_tramite(page, it["id_tramite"])
                except Exception as e:  # noqa: BLE001
                    _log.warning("listar_caixa_grupo: popup do trâmite %s falhou: %s", it.get("id_tramite"), e)
                    extra = {}
                if extra.get("data_envio"):
                    it["data_envio"] = extra["data_envio"]
                if extra.get("descricao_tramite"):
                    it["descricao"] = extra["descricao_tramite"]
                if extra.get("atribuicao"):
                    it["atribuicao"] = extra["atribuicao"]

    _log.info("listar_caixa_grupo(%r): encontrado=%s, %d item(ns), %d grupo(s) descoberto(s).",
               nome_grupo, encontrado, len(itens), len(grupos))
    return {"ok": True, "encontrado": encontrado, "grupos_disponiveis": grupos, "itens": itens}


# Extração do DETALHAMENTO do PAJ (reusa os seletores do sincronizador): CPF na tabela
# "Assistido(s) Pessoa(s) Física" (3ª coluna), pretensão/assistido por regex no innerText
# e a narrativa (HTML) do painel `narrativaPanel`.
_JS_DETALHE_PAJ = r"""() => {
  const r = {};
  const body = document.body.innerText;
  const am = body.match(/Assistido\(s\)[\t\s]+([^\n]+)/);
  if (am) r.assistido = am[1].trim();
  const cpfCell = document.querySelector('#detalhamentoForm\\:dataTableListaAssistidosPF tbody tr td:nth-child(3)');
  if (cpfCell) {
    const m = (cpfCell.textContent || '').match(/\d{3}\.\d{3}\.\d{3}-\d{2}/);
    if (m) r.cpf = m[0];
  }
  const pm = body.match(/Pretens[ãa]o[\t\s]+([^\n]+)/);
  if (pm) r.pretensao = pm[1].trim();
  const np = document.querySelector('#detalhamentoForm\\:narrativaPanel');
  if (np) r.narrativa = np.innerHTML;
  return r;
}"""


async def detalhes_paj(url_detalhe: str) -> dict:
    """Abre o detalhamento do PAJ (na MESMA sessão headless) e extrai
    {cpf, pretensao, assistido, narrativa}. Usado como FALLBACK quando o PAJ ainda não
    foi sincronizado localmente (sem metadata.json). Retorna {} em falha/conflito."""
    if not url_detalhe:
        return {}
    await _ensure_logged_in()
    try:
        async with _sc.PAGE_LOCK:  # page compartilhada — ver comentário em listar_caixa_grupo
            page = await _get_page()
            for tentativa in range(2):  # relê do zero se houve conflito — ver listar_caixa_grupo
                await page.goto(url_detalhe, wait_until="domcontentloaded", timeout=30000)
                await _wait_pf_ajax(page, timeout=12000)
                await page.wait_for_timeout(600)
                ok_conflito, havia_conflito = await _resolver_conflito_login2(page)
                if not ok_conflito:
                    return {}
                dados = await _eval_seguro(page, _JS_DETALHE_PAJ)
                if not havia_conflito or tentativa == 1:
                    return dados if isinstance(dados, dict) else {}
            return {}
    except Exception as e:  # noqa: BLE001
        _log.warning("detalhes_paj(%s): %r", url_detalhe, e)
        return {}


# ---------------------------------------------------------------------------
# MOVIMENTAR
# ---------------------------------------------------------------------------
async def movimentar_paj(
    pajs: list[dict],
    descricao: str,
    fase: str,
    anexos: list[dict] | None = None,
    tipo_arquivo: str = "Documentos do Assistido",
    dry_run: bool = False,
    descricao_html: bool = False,
) -> dict:
    """Registra uma MOVIMENTAÇÃO (novo sequencial) em um ou vários PAJs.
    pajs: [{"id": <id_processo>, "numero": "AAAA/UUU-NNNNN"}, ...] (bloco = mesma unidade).
    Retorna {ok, status, detalhe, pajs, anexos, mov_seq}."""
    pajs = [p for p in (pajs or []) if p and p.get("id")]
    if not pajs:
        return {"ok": False, "status": "falha", "detalhe": "Nenhum PAJ informado."}
    numeros = [p.get("numero", "") for p in pajs]
    _log.info("movimentar_paj(%s) fase=%r dry=%s", numeros, fase, dry_run)

    await _ensure_logged_in()
    page = await _get_page()
    url = (f"{SISDPU_URL}/pages/movimentacao/movimentaProcesso.xhtml"
           f"?id={pajs[0]['id']}&caixaEntrada=false")
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await _wait_pf_ajax(page)
    await page.wait_for_timeout(400)
    if not await _resolver_conflito_login(page):
        return {"ok": False, "status": "falha", "detalhe": _MSG_CONFLITO_SESSAO}

    for extra in pajs[1:]:
        if extra.get("numero"):
            await _adicionar_paj_por_numero(page, _MOV_FORM, f"{_MOV_FORM}:add", extra["numero"])
    lista = await _ler_pajs_lista(page, f"{_MOV_FORM}:processosSelecionados_input")
    faltando = [n for n in numeros if n and n not in lista]
    if faltando:
        return {"ok": False, "status": "falha",
                "detalhe": (f"PAJ(s) não entraram na lista de movimentação: {faltando} "
                            f"(lista={lista}). Possível restrição de unidade no bloco.")}

    await page.evaluate(_JS_MARCAR_TODAS_FASES)
    await _wait_pf_ajax(page)
    await page.wait_for_timeout(700)
    fr = await page.evaluate(_JS_SELECT_OPT, [f"{_MOV_FORM}:faseId", fase, False])
    if not fr.get("ok"):
        return {"ok": False, "status": "falha",
                "detalhe": f"Fase {fase!r} não encontrada no SISDPU (opções={fr.get('opts')})."}
    await _wait_pf_ajax(page)
    await page.wait_for_timeout(900)
    nfases = await page.eval_on_selector_all(
        _sel(f"{_MOV_FORM}:dataTableFasesSelecionadas") + " tbody tr",
        "els=>els.filter(e=>!/Nenhum registro/i.test(e.innerText)).length")
    if not nfases:
        return {"ok": False, "status": "falha", "detalhe": "Fase não foi adicionada à movimentação."}

    await page.evaluate(_JS_SET_CKEDITOR,
                        [descricao, f"{_MOV_FORM}:editorValue", f"{_MOV_FORM}_editorValue",
                         bool(descricao_html)])

    anexados: list[str] = []
    if anexos and not dry_run:
        anexados = await _anexar_documentos_movimentacao(page, anexos, tipo_arquivo)

    if dry_run:
        n_anexos = len([a for a in (anexos or []) if a])
        return {"ok": True, "status": "dry_run",
                "detalhe": f"[dry_run] Movimentaria {numeros} (fase {fase!r}, {n_anexos} anexo(s)).",
                "pajs": numeros}

    with contextlib.suppress(Exception):
        await page.get_by_role("button", name="Movimentar", exact=True).first.click()
    await _wait_pf_ajax(page)
    await page.wait_for_timeout(900)
    body = await page.evaluate("()=>document.body.innerText||''")
    ok = bool(re.search(r"gravad[oa] com sucesso", body, re.I))
    for d in await _dialogos_visiveis(page):
        if "tramitar" in _norm_nome(d.get("msg", "")).lower():
            with contextlib.suppress(Exception):
                await page.locator(_sel(f"{_MOV_FORM}:confirmDialogTramitacao") + ' button', has_text="Não").first.click()
                await page.wait_for_timeout(600)
            break
    if not ok and "detalhamentoProcesso" in page.url:
        ok = True
    detalhe = "Movimentação registrada com sucesso." if ok else "Não confirmei a movimentação (sem growl de sucesso)."
    if anexados:
        detalhe += f" Anexos: {len(anexados)}."
    mov_seq = await _ler_seq_mov(page, pajs[0]["id"]) if ok else None
    if ok and mov_seq is not None:
        detalhe += f" · Mov {mov_seq}"
    return {"ok": ok, "status": "movimentado" if ok else "falha",
            "detalhe": detalhe, "pajs": numeros, "anexos": anexados, "mov_seq": mov_seq}


# ---------------------------------------------------------------------------
# TRAMITAR
# ---------------------------------------------------------------------------
async def tramitar_paj(
    pajs: list[dict],
    grupo: str | None = None,
    usuarios: list[str] | None = None,
    descricao: str | None = None,
    prazo_data: str | None = None,
    prazo_desc: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Registra uma TRAMITAÇÃO (Trâmite Interno) de PAJ(s) para um GRUPO da unidade
    e/ou USUÁRIO(s). Confirma SEM movimentar. Retorna {ok, status, detalhe, pajs}."""
    pajs = [p for p in (pajs or []) if p and p.get("id")]
    if not pajs:
        return {"ok": False, "status": "falha", "detalhe": "Nenhum PAJ informado."}
    if not grupo and not usuarios:
        return {"ok": False, "status": "falha", "detalhe": "Informe um grupo ou usuário(s) destino."}
    numeros = [p.get("numero", "") for p in pajs]
    _log.info("tramitar_paj(%s) grupo=%r usuarios=%s dry=%s", numeros, grupo, usuarios, dry_run)

    await _ensure_logged_in()
    page = await _get_page()
    url = f"{SISDPU_URL}/pages/tramite/tramitaProcesso.xhtml?id={pajs[0]['id']}&idTramite=0"
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await _wait_pf_ajax(page)
    await page.wait_for_timeout(400)
    if not await _resolver_conflito_login(page):
        return {"ok": False, "status": "falha", "detalhe": _MSG_CONFLITO_SESSAO}

    for extra in pajs[1:]:
        if extra.get("numero"):
            await _adicionar_paj_por_numero(page, _TRAM_FORM, f"{_TRAM_FORM}:Adicionar", extra["numero"])
    lista = await _ler_pajs_lista(page, f"{_TRAM_FORM}:pajs_input")
    faltando = [n for n in numeros if n and n not in lista]
    if faltando:
        return {"ok": False, "status": "falha",
                "detalhe": (f"PAJ(s) não entraram na lista de tramitação: {faltando} (lista={lista}).")}

    with contextlib.suppress(Exception):
        await page.evaluate(r"""()=>{const r=document.querySelector('input[name="form_tramite:j_idt125"][value="1"]');
            if(r&&!r.checked){const box=r.closest('.ui-radiobutton').querySelector('.ui-radiobutton-box'); box&&box.click();}}""")
        await _wait_pf_ajax(page)

    destino = []
    if grupo:
        g = await page.evaluate(_JS_SELECT_OPT, [f"{_TRAM_FORM}:grupoDestinatarioLocal", grupo, False])
        if not g.get("ok"):
            return {"ok": False, "status": "falha",
                    "detalhe": f"Grupo {grupo!r} não encontrado (opções={g.get('opts')})."}
        destino.append(f"grupo {g['txt']!r}")
        await _wait_pf_ajax(page)
        await page.wait_for_timeout(500)
    for nome in (usuarios or []):
        with contextlib.suppress(Exception):
            item = page.locator(
                _sel(f"{_TRAM_FORM}:pickList") + ' .ui-picklist-source li').filter(has_text=nome).first
            await item.dblclick(timeout=5000)
            await _wait_pf_ajax(page)
        destino.append(f"usuário {nome!r}")

    if descricao:
        with contextlib.suppress(Exception):
            await page.fill(_sel(f"{_TRAM_FORM}:descTramite"), descricao)
    if prazo_data:
        with contextlib.suppress(Exception):
            await page.fill(_sel(f"{_TRAM_FORM}:dataPrazo_input"), prazo_data)
    if prazo_desc:
        with contextlib.suppress(Exception):
            await page.evaluate(r"""(txt)=>{const ta=[...document.querySelectorAll('textarea')].find(t=>t.getAttribute('maxlength')==='300');
                if(ta){ta.value=txt; ta.dispatchEvent(new Event('input',{bubbles:true}));}}""", prazo_desc)

    if dry_run:
        return {"ok": True, "status": "dry_run",
                "detalhe": f"[dry_run] Tramitaria {numeros} → {', '.join(destino)}.", "pajs": numeros}

    with contextlib.suppress(Exception):
        await page.evaluate(
            "()=>{window.onbeforeunload=null; try{PF('confirmationDialogTramitarProcessoJudicialWidget').show();}catch(e){}}")
    await page.wait_for_timeout(700)
    dlgs = await _dialogos_visiveis(page)
    if not any("movimentar" in _norm_nome(d.get("msg", "")).lower() for d in dlgs):
        with contextlib.suppress(Exception):
            await page.get_by_role("button", name="Tramitar", exact=True).first.click(timeout=5000)
        await page.wait_for_timeout(700)
        dlgs = await _dialogos_visiveis(page)
    if not any("movimentar" in _norm_nome(d.get("msg", "")).lower() for d in dlgs):
        _log.warning("tramitar_paj: diálogo não apareceu; dialogs=%s url=%s", dlgs, page.url)
        return {"ok": False, "status": "falha", "detalhe": "Diálogo de confirmação da tramitação não apareceu."}
    with contextlib.suppress(Exception):
        await page.locator(_sel(f"{_TRAM_FORM}:NaoTramitarProcessoJudicial")).click(timeout=5000)
    await _wait_pf_ajax(page)
    await page.wait_for_timeout(1000)
    body = await page.evaluate("()=>document.body.innerText||''")
    ok = bool(re.search(r"tramitad[oa]s?.*sucesso", body, re.I)) or "historicoTramitacao" in page.url
    detalhe = (f"Tramitação registrada para {', '.join(destino)}." if ok
               else "Não confirmei a tramitação (sem growl de sucesso).")
    return {"ok": ok, "status": "tramitado" if ok else "falha", "detalhe": detalhe, "pajs": numeros}


# ---------------------------------------------------------------------------
# CONCLUIR (admitir na caixa de grupo + concluir na caixa do usuário)
# ---------------------------------------------------------------------------
async def concluir_paj(id_tramites: list[str], dry_run: bool = False) -> dict:
    """ADMITE (retira da caixa de grupo) e CONCLUI (baixa) o(s) trâmite(s) ORIGINÁRIO(S)
    pelo `id_tramite` (data-rk; não muda ao admitir). Retorna
    {ok, status, detalhe, resultados:{id:{admitido,concluido,ja_ausente}}}.

    IMPORTANTE: a checagem final só confirma que o id sumiu da caixa/grupo — isso é
    verdadeiro tanto quando ESTA chamada admitiu+concluiu o trâmite quanto quando ele
    JÁ estava ausente (concluído manualmente por fora, ou id incorreto) ANTES de
    começar. Por isso rastreamos `visto[i]` (o id foi encontrado em algum momento,
    na aba grupo OU na caixa pessoal, ANTES de qualquer ação) para não reportar
    "concluído com sucesso" em cima de um id que nunca existiu nesta execução —
    nesse caso o resultado é `status="ja_ausente"` (nada foi feito agora)."""
    ids = [str(i).strip() for i in (id_tramites or []) if str(i).strip()]
    if not ids:
        return {"ok": False, "status": "falha", "detalhe": "Nenhum id de trâmite informado."}
    _log.info("concluir_paj(%s) dry=%s", ids, dry_run)
    res = {i: {"admitido": False, "concluido": False, "ja_ausente": False} for i in ids}
    if dry_run:
        return {"ok": True, "status": "dry_run",
                "detalhe": f"[dry_run] Admitiria+concluiria os trâmites {ids}.", "resultados": res}

    await _ensure_logged_in()
    page = await _get_page()
    cx_url = f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml"

    await page.goto(cx_url, wait_until="domcontentloaded", timeout=30000)
    await _wait_pf_ajax(page); await page.wait_for_timeout(600)
    if not await _resolver_conflito_login(page):
        return {"ok": False, "status": "falha", "detalhe": _MSG_CONFLITO_SESSAO, "resultados": res}
    await page.evaluate(_JS_ATIVA_ABA_GRUPO)
    await _wait_pf_ajax(page); await page.wait_for_timeout(900)
    visto: dict[str, bool] = {i: False for i in ids}
    for i in ids:
        try:
            if await page.evaluate(_JS_TRAMITE_NO_GRUPO, i):
                visto[i] = True
                if await page.evaluate(_JS_ADMITIR_TRAMITE, i):
                    await _wait_pf_ajax(page, timeout=15000); await page.wait_for_timeout(800)
                    res[i]["admitido"] = True
            else:
                res[i]["admitido"] = None
        except Exception as e:  # noqa: BLE001
            _log.warning("concluir_paj: admitir %s falhou: %s", i, e)

    await page.goto(cx_url, wait_until="domcontentloaded", timeout=30000)
    await _wait_pf_ajax(page); await page.wait_for_timeout(700)
    marcados = 0
    for i in ids:
        try:
            achou_caixa = await page.evaluate(_JS_TRAMITE_NA_CAIXA, i)
            if achou_caixa:
                visto[i] = True
                if await page.evaluate(_JS_MARCA_CHECKBOX, i):
                    marcados += 1
        except Exception as e:  # noqa: BLE001
            _log.warning("concluir_paj: marcar %s falhou: %s", i, e)
    if marcados:
        await page.evaluate(_JS_CLICA_BTN_CONCLUIR)
        await _wait_pf_ajax(page, timeout=15000); await page.wait_for_timeout(1300)

    await page.goto(cx_url, wait_until="domcontentloaded", timeout=30000)
    await _wait_pf_ajax(page); await page.wait_for_timeout(600)
    na_caixa = {i: await page.evaluate(_JS_TRAMITE_NA_CAIXA, i) for i in ids}
    await page.evaluate(_JS_ATIVA_ABA_GRUPO)
    await _wait_pf_ajax(page); await page.wait_for_timeout(800)
    no_grupo = {i: await page.evaluate(_JS_TRAMITE_NO_GRUPO, i) for i in ids}
    for i in ids:
        ausente_agora = not na_caixa[i] and not no_grupo[i]
        if ausente_agora and not visto[i]:
            # Nunca foi encontrado (nem na aba grupo, nem na caixa pessoal) em NENHUM
            # momento desta chamada — já estava ausente ANTES de começarmos. Não é uma
            # conclusão desta execução (provável conclusão manual anterior, ou id errado).
            res[i]["concluido"] = False
            res[i]["ja_ausente"] = True
        else:
            res[i]["concluido"] = ausente_agora

    concluidos = [i for i in ids if res[i]["concluido"]]
    ja_ausentes = [i for i in ids if res[i]["ja_ausente"]]
    if len(concluidos) == len(ids):
        status, ok = "concluido", True
        detalhe = f"{len(concluidos)}/{len(ids)} trâmite(s) concluído(s)."
    elif len(ja_ausentes) == len(ids):
        status, ok = "ja_ausente", False
        detalhe = (f"{len(ja_ausentes)}/{len(ids)} trâmite(s) já estava(m) ausente(s) da caixa/grupo "
                   "antes desta execução — provavelmente já concluído(s) manualmente. Nada foi alterado agora.")
    elif concluidos or ja_ausentes:
        status, ok = "parcial", False
        detalhe = (f"{len(concluidos)}/{len(ids)} concluído(s) agora, {len(ja_ausentes)} já ausente(s) "
                   "antes desta execução.")
    else:
        status, ok = "falha", False
        detalhe = f"{len(concluidos)}/{len(ids)} trâmite(s) concluído(s)."
    return {"ok": ok, "status": status, "detalhe": detalhe, "resultados": res}


# ---------------------------------------------------------------------------
# Grupos da unidade (para o dropdown de tramitação / favoritas)
# ---------------------------------------------------------------------------
async def listar_grupos_unidade(id_processo: str = "") -> list[str]:
    """Lê os nomes das caixas de grupo da unidade a partir do dropdown
    `grupoDestinatarioLocal` da tela de tramitação. Independe de PAJ: a mesma tela
    (`tramitaProcesso.xhtml`), acessada SEM `?id=` — pelo link "Tramitação" direto da
    caixa de entrada (modo bloco) —, já traz o dropdown populado. `id_processo`, se
    informado, abre a tela no contexto daquele PAJ."""
    try:
        await _ensure_logged_in()
        async with _sc.PAGE_LOCK:  # page compartilhada — ver comentário em listar_caixa_grupo
            page = await _get_page()
            url = f"{SISDPU_URL}/pages/tramite/tramitaProcesso.xhtml"
            if id_processo:
                url += f"?id={id_processo}&idTramite=0"
            grupos = []
            for tentativa in range(2):  # relê do zero se houve conflito — ver listar_caixa_grupo
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await _wait_pf_ajax(page); await page.wait_for_timeout(400)
                ok_conflito, havia_conflito = await _resolver_conflito_login2(page)
                if not ok_conflito:
                    return []
                grupos = await _eval_seguro(page, r"""()=>{
                    const s=document.getElementById('form_tramite:grupoDestinatarioLocal');
                    return s?[...s.options].map(o=>o.text.trim()).filter(t=>t && !/selecione/i.test(t)):[];
                }""")
                if not havia_conflito or tentativa == 1:
                    break
        _log.info("listar_grupos_unidade: %d grupo(s).", len(grupos or []))
        return grupos or []
    except Exception as e:  # noqa: BLE001
        _log.warning("listar_grupos_unidade falhou: %r", e)
        return []


