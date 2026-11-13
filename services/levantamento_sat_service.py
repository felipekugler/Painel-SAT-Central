"""Gera o PDF "Levantamento SAT" — anonimizado (sem nome do assistido, só o número do
PAJ). É recriado a CADA juntada (1ª movimentação via "Anexar ao PAJ" ou qualquer "Anexar
mais" complementar) — nunca um arquivo estático salvo para seleção manual. Mostra, em
cards separados: o que está sendo anexado NESTA movimentação (verde, datado de hoje), o
que já tinha sido anexado em movimentações anteriores (cinza, cada card com a data
daquela movimentação — mais recente primeiro, para rastreabilidade) e, por fim, o que
ainda não foi anexado (lista sempre recalculada a partir do estado atual). Serve para
quem for analisar o PAJ depois saber, sem precisar deste painel, o histórico completo do
que existe no SAT para este assistido. Por isso é sempre o 1º anexo de cada movimentação
(pedido do usuário, 2026-11-13).
"""

from __future__ import annotations

from datetime import datetime

import fitz  # PyMuPDF

from ingestao import sat_client
from services import sat_service

A4 = fitz.paper_rect("a4")
MARGEM = 40
FONTE = "helv"
FONTE_NEGRITO = "hebo"
COR_CINZA = (0.45, 0.45, 0.45)
COR_TEXTO = (0.12, 0.12, 0.12)
COR_OK = (0.0, 0.42, 0.18)       # verde — baixado
COR_ERRO = (0.70, 0.11, 0.11)    # vermelho — falha/antirrobô/indisponível
COR_AVISO = (0.70, 0.45, 0.0)    # âmbar — qualquer outro motivo
_STATUS_ERRO = {"falha", "antirobo", "indisponivel"}

# Mesmo texto do `bmStatusTxt()` em templates/index.html — paridade com o modal "Baixar
# mais" (pedido do usuário, 2026-11-13).
_STATUS_TEXTO = {
    "nao_selecionado": "não baixado (desativado p/ a pretensão)",
    "nao_relacionado": "não relacionado à pretensão",
    "antirobo": "bloqueado (antirobô) no 1º acesso",
    "indisponivel": "indisponível no SAT",
    "sem_dados": "sem dados no SAT",
    "falha": "falhou no 1º download",
    "nao_consultado": "não consultado (indeferido)",
    "pulado_prazo": "pulado - protocolo há mais de 5 anos",
    "nao_filtrado_nb": "pulado - NB não indicado no filtro",
    "nao_filtrado_protocolo": "pulado - protocolo não indicado no filtro",
    "nao_filtrado_pretensao": "fora da associação de PAT configurada para esta pretensão",
    "ignorado": "ignorado - espécie na lista de ignorados",
}


def _rotulo_item(it: dict, tipos_por_key: dict) -> str:
    t = tipos_por_key.get(it.get("key", ""))
    if it.get("key") == "pat":
        svc = it.get("servico") or "Tarefa PAT"
        label = svc + (f" — protocolo {it['sub']}" if it.get("sub") else "")
        if it.get("nb"):
            label += f" — NB {it['nb']}"
        return label
    rotulo_base = t["label"] if t else (it.get("servico") or it.get("key") or "Documento")
    return rotulo_base + (f" — {it['sub']}" if it.get("sub") else "")


def _classificar(itens: list[dict], nomes_atuais: set[str] | None
                 ) -> tuple[list[dict], dict[int, list[dict]], list[dict]]:
    """Separa `itens` em (rodada atual, passado por nº de movimentação, não anexados).

    `nomes_atuais=None` (1ª movimentação / "Anexar ao PAJ"): a rodada atual é tudo que
    está `ja_baixado` e não `descartado_manual` e ainda sem movimentação prévia.
    `nomes_atuais={...}` ("Anexar mais"): a rodada atual é EXATAMENTE os nomes escolhidos
    pelo usuário nesta chamada — não importa o que o inventário já diga sobre eles."""
    atual: list[dict] = []
    passado: dict[int, list[dict]] = {}
    nao_anexados: list[dict] = []
    for it in itens:
        nome = it.get("nome", "")
        movs = it.get("anexado_movs") or []
        if nomes_atuais is not None and nome in nomes_atuais:
            atual.append(it)
        elif movs:
            for seq in movs:
                passado.setdefault(seq, []).append(it)
        elif nomes_atuais is None and it.get("ja_baixado") and not it.get("descartado_manual"):
            atual.append(it)
        else:
            nao_anexados.append(it)
    return atual, passado, nao_anexados


def _fmt_data(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%d/%m/%Y")
    except Exception:  # noqa: BLE001
        return ""


def _datas_dos_movs(paj_norm: str) -> dict[int, str]:
    """{mov_seq: 'DD/MM/AAAA'} de cada movimentação JÁ REGISTRADA deste PAJ (a principal +
    cada "anexar mais" complementar), lida do histórico local — usada para datar os cards
    cinza das movimentações anteriores."""
    rec = next((r for r in sat_service.historico_sat.listar() if r.get("paj_norm") == paj_norm), None)
    if not rec:
        return {}
    out: dict[int, str] = {}
    seq0 = rec.get("mov_seq")
    if seq0:
        out[seq0] = _fmt_data(rec.get("ts", ""))
    for m in (rec.get("movs_extra") or []):
        if m.get("seq"):
            out[m["seq"]] = _fmt_data(m.get("ts", ""))
    return out


def _status_texto_pendente(it: dict) -> str:
    """Texto entre parênteses de um item em "Documentos não anexados" — por que ainda não
    foi anexado."""
    if it.get("descartado_manual"):
        return "Baixado, descartado manualmente"
    if it.get("ja_baixado"):
        return "Baixado — ainda não anexado"
    if it.get("detalhe"):
        return it["detalhe"]
    if it.get("status") in _STATUS_TEXTO:
        return _STATUS_TEXTO[it["status"]]
    return ""


def _cor_status_pendente(it: dict) -> tuple[float, float, float]:
    if it.get("status") in _STATUS_ERRO:
        return COR_ERRO
    return COR_AVISO


def _montar_grupos(itens: list[dict], tipos_por_key: dict) -> list[dict]:
    """Agrupa por `grupo` do catálogo `TIPOS_DOCUMENTOS`, na mesma ordem usada no modal
    "Baixar mais". Oculta só o que não faz sentido listar (sem_dados/descartado)."""
    grupos_ordem: list[str] = []
    for t in sat_client.TIPOS_DOCUMENTOS:
        if t["grupo"] not in grupos_ordem:
            grupos_ordem.append(t["grupo"])
    vis = [it for it in itens if it.get("status") not in ("sem_dados", "descartado")]
    out = []
    for g in grupos_ordem:
        rows = []
        for t in sat_client.TIPOS_DOCUMENTOS:
            if t["grupo"] != g:
                continue
            for it in vis:
                if it.get("key") != t["key"]:
                    continue
                rows.append({**it, "label": _rotulo_item(it, tipos_por_key)})
        if rows:
            out.append({"grupo": g, "rows": rows})
    return out


def _largura(texto: str, fontname: str = FONTE, fontsize: float = 9) -> float:
    """Largura de `texto` nesta fonte — soma caractere a caractere, NUNCA a string
    inteira de uma vez. `fitz.get_text_length` SUBESTIMA a largura de strings com
    certas combinações de caracteres acentuados (confirmado: "Declaração" mede ~40.5pt
    de uma vez, mas a soma das letras isoladas — e a largura realmente desenhada por
    `insert_text` — dá ~45.5pt); isso colava a palavra seguinte sem espaço nenhum.
    Medir por caractere é a forma confiável de bater com o que é desenhado na tela."""
    return sum(fitz.get_text_length(c, fontname=fontname, fontsize=fontsize) for c in (texto or ""))


def _tokens(texto: str, cor) -> list[tuple[str, tuple]]:
    return [(p, cor) for p in (texto or "").split(" ") if p]


def _escrever_tokens(doc: fitz.Document, page: fitz.Page, y: float, x0: float,
                     largura_max: float, tokens: list[tuple[str, tuple]],
                     fontsize: float = 9, fontname: str = FONTE) -> tuple[fitz.Page, float]:
    """Desenha `tokens` (lista de (palavra, cor)) lado a lado, quebrando linha quando
    excede `largura_max` a partir de `x0` — permite 2 cores na mesma linha (rótulo em
    preto + status colorido), ao contrário de uma única `insert_text` por linha."""
    espaco_w = _largura(" ", fontname, fontsize)
    x = x0
    for palavra, cor in tokens:
        w = _largura(palavra, fontname, fontsize)
        if x > x0 and x + w > x0 + largura_max:
            y += 12.5
            x = x0
            if y + 12 > A4.height - MARGEM:
                page, y = _nova_pagina(doc)
                x = x0
        if y + 12 > A4.height - MARGEM:
            page, y = _nova_pagina(doc)
            x = x0
        page.insert_text((x, y), palavra, fontsize=fontsize, fontname=fontname, color=cor)
        x += w + espaco_w
    y += 12.5
    return page, y


def _nova_pagina(doc: fitz.Document) -> tuple[fitz.Page, float]:
    page = doc.new_page(width=A4.width, height=A4.height)
    return page, MARGEM


def _texto_centralizado(page: fitz.Page, y: float, texto: str, fontsize: float,
                        fontname: str = FONTE, color=COR_TEXTO) -> None:
    largura = _largura(texto, fontname, fontsize)
    x = (A4.width - largura) / 2
    page.insert_text((x, y), texto, fontsize=fontsize, fontname=fontname, color=color)


def _escrever_secao(doc: fitz.Document, page: fitz.Page, y: float, titulo: str,
                    grupos: list[dict], destacar: bool = False, cor_card: str = "verde",
                    data: str = "", mostrar_status: bool = True) -> tuple[fitz.Page, float]:
    """`destacar`: desenha um card (verde ou cinza, conforme `cor_card`) por baixo desta
    seção inteira (título + conteúdo), para destacá-la das demais — verde = rodada atual
    (sendo anexada agora), cinza = movimentação anterior já concluída. Desenhado DEPOIS do
    texto, com `overlay=False` (fica atrás do que já foi escrito) — evita ter que medir a
    altura antes de escrever. Se a seção virar página no meio, o card cobre só o que ficou
    na página onde começou (até o rodapé) — limitação aceitável, seções deste tamanho
    raramente viram página.
    `data`: quando informada, escrita no canto direito da linha do título — a data da
    movimentação, para rastreabilidade (pedido do usuário, 2026-11-13).
    `mostrar_status`: False para os cards de "anexados" (o próprio card já diz o motivo —
    foi ou está sendo anexado); True (padrão) para "não anexados", onde cada linha mostra
    o motivo específico entre parênteses."""
    pagina_inicio, y_inicio = page, y
    largura_util = A4.width - 2 * MARGEM - 14
    if y + 26 > A4.height - MARGEM:
        page, y = _nova_pagina(doc)
        pagina_inicio, y_inicio = page, y
    page.insert_text((MARGEM, y), titulo, fontsize=13, fontname=FONTE_NEGRITO, color=COR_TEXTO)
    if data:
        largura_data = _largura(data, FONTE, 9)
        page.insert_text((A4.width - MARGEM - largura_data, y - 1), data, fontsize=9,
                         fontname=FONTE, color=COR_CINZA)
    y += 10
    page.draw_line((MARGEM, y), (A4.width - MARGEM, y), color=(0.6, 0.6, 0.6), width=0.8)
    y += 18
    if not grupos:
        page.insert_text((MARGEM, y), "Nenhum documento nesta situação.", fontsize=9,
                          fontname=FONTE, color=COR_CINZA)
        y += 18
    else:
        for grupo in grupos:
            if y + 18 > A4.height - MARGEM:
                page, y = _nova_pagina(doc)
            page.insert_text((MARGEM, y), grupo["grupo"], fontsize=10.5, fontname=FONTE_NEGRITO, color=COR_TEXTO)
            y += 15
            for row in grupo["rows"]:
                if y + 12 > A4.height - MARGEM:
                    page, y = _nova_pagina(doc)
                page.insert_text((MARGEM + 2, y), "•", fontsize=9, fontname=FONTE, color=COR_TEXTO)
                tokens = _tokens(row["label"], COR_TEXTO)
                if mostrar_status:
                    status_txt = _status_texto_pendente(row)
                    if status_txt:
                        tokens += _tokens(f"({status_txt})", _cor_status_pendente(row))
                page, y = _escrever_tokens(doc, page, y, MARGEM + 13, largura_util, tokens)
            y += 8
    if destacar:
        pad = 10
        # `y_inicio` é a BASE da linha do título (não o topo) — precisa de mais margem
        # acima para não encostar no texto (a letra sobe uns ~9-10pt acima da base com
        # fontsize 13). Pedido do usuário, 2026-11-13.
        pad_topo = 20
        y_fim = y if page is pagina_inicio else (A4.height - MARGEM)
        card = fitz.Rect(MARGEM - pad, y_inicio - pad_topo, A4.width - MARGEM + pad, y_fim)
        if cor_card == "cinza":
            borda, preenchido = (0.70, 0.70, 0.70), (0.93, 0.93, 0.93)
        else:
            borda, preenchido = (0.55, 0.78, 0.60), (0.90, 0.97, 0.91)
        pagina_inicio.draw_rect(card, color=borda, fill=preenchido, width=0.8, overlay=False)
    return page, y


def gerar_pdf_levantamento_sat(paj_norm: str, *, nomes_rodada_atual: list[str] | None = None) -> bytes:
    """Gera o PDF "Levantamento SAT" deste PAJ (sem nome do assistido) — ver docstring do
    módulo para a lógica dos cards por movimentação.

    `nomes_rodada_atual`: None (padrão, usado pela 1ª movimentação/"Anexar ao PAJ") — a
    rodada atual é tudo que está baixado e não descartado manualmente. Lista de nomes de
    arquivo (usado por "Anexar mais") — a rodada atual é EXATAMENTE essa lista."""
    numero_fmt = sat_service._norm_to_num(paj_norm)
    tipos_por_key = {t["key"]: t for t in sat_client.TIPOS_DOCUMENTOS}
    nomes_atuais = set(nomes_rodada_atual) if nomes_rodada_atual is not None else None

    itens = sat_service.inventario_paj(paj_norm).get("itens") or []
    atual, passado, nao_anex = _classificar(itens, nomes_atuais)

    itens_i: list[dict] = []
    if sat_service.tem_instituidor(paj_norm):
        itens_i = [it for it in (sat_service.inventario_paj(paj_norm, escopo="instituidor").get("itens") or [])
                  if it.get("status") not in ("sem_dados", "descartado")]
    atual_i, passado_i, nao_anex_i = _classificar(itens_i, nomes_atuais)

    def _grupos(rows_a: list[dict], rows_i: list[dict]) -> list[dict]:
        grupos = _montar_grupos(rows_a, tipos_por_key)
        if rows_i:
            grupos.append({"grupo": "Instituidor",
                           "rows": [{**it, "label": "Instituidor — " + _rotulo_item(it, tipos_por_key)}
                                   for it in rows_i]})
        return grupos

    datas_mov = _datas_dos_movs(paj_norm)
    seqs_passados = sorted(set(passado) | set(passado_i), reverse=True)

    doc = fitz.open()
    page, y = _nova_pagina(doc)
    y += 16
    _texto_centralizado(page, y, "LEVANTAMENTO SAT", 18, FONTE_NEGRITO, COR_TEXTO)
    y += 28
    _texto_centralizado(page, y, f"PAJ {numero_fmt}", 11, FONTE_NEGRITO, COR_TEXTO)
    y += 18
    _texto_centralizado(page, y, f"Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}",
                        8.5, FONTE, COR_CINZA)
    y += 30

    if atual or atual_i:
        page, y = _escrever_secao(doc, page, y, "Documentos anexados nesta movimentação",
                                  _grupos(atual, atual_i), destacar=True, cor_card="verde",
                                  data=datetime.now().strftime("%d/%m/%Y"), mostrar_status=False)
        y += 28

    for seq in seqs_passados:
        titulo = f"Documentos já anexados — movimentação nº {seq}"
        page, y = _escrever_secao(doc, page, y, titulo, _grupos(passado.get(seq, []), passado_i.get(seq, [])),
                                  destacar=True, cor_card="cinza", data=datas_mov.get(seq, ""),
                                  mostrar_status=False)
        y += 28

    page, y = _escrever_secao(doc, page, y, "Documentos não anexados", _grupos(nao_anex, nao_anex_i),
                              destacar=False)

    pdf_bytes = doc.tobytes(garbage=4, deflate=True)
    doc.close()
    return pdf_bytes


def nome_arquivo_levantamento(paj_norm: str) -> str:
    numero_fmt = sat_service._norm_to_num(paj_norm)
    return f"Levantamento SAT - {numero_fmt.replace('/', '-')}.pdf"
