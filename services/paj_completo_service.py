"""Gera, sob demanda (sem sincronização prévia persistida), um PDF único com
capa, narrativa, histórico de movimentações e todos os anexos de um PAJ —
feature "Baixar PAJ Completo" (ícone na coluna Ação da Fila Automática/
Individual/PAJs Concluídos). Pedido do usuário, 2026-11-13: "o download deve
ser completo, sem a possibilidade de escolher movimentações específicas, ou
apenas arquivos crus... É o PAJ completo apenas".

Ao contrário do PAINEL SISDPU 2.0 (que monta o PDF a partir de dados já
sincronizados em disco, por um processo de sincronização separado), aqui TUDO
é feito ao vivo no clique: abre o PAJ pela busca global, extrai movimentações
e os documentos de cada uma, baixa cada anexo para uma pasta temporária,
monta o PDF e descarta os temporários em seguida. Mais simples de manter (sem
sincronização persistida, sem limite de anexos por PAJ, sem modal de
recorte), ao custo de ser mais lento por clique.

Tempo medido ao vivo (2026-11-13, 2 PAJs reais da caixa): ~7s para abrir o
PAJ + ~11s por movimentação COM anexo (o custo é do popup do SISDPU — AJAX +
estabilização —, independente de quantos documentos tem dentro dela) +
download/montagem do PDF praticamente instantâneos (sessão já logada,
PyMuPDF). Para um PAJ com 2 movimentações com anexo, ~30s do clique ao PDF
pronto.
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
import unicodedata
from datetime import datetime
from html import unescape
from pathlib import Path

import fitz  # PyMuPDF

from ingestao import sisdpu_client

log = logging.getLogger("sisdpu.paj_completo")

A4 = fitz.paper_rect("a4")
MARGEM = 40
FONTE = "helv"
FONTE_NEGRITO = "hebo"
IMAGENS_EXT = {"jpg", "jpeg", "png", "gif", "bmp", "tiff", "tif"}


class PajCompletoErro(RuntimeError):
    """Erro ao gerar o PDF completo (PAJ inválido, falha de navegação no
    SISDPU, etc.) — mensagem já pronta para mostrar ao usuário."""


def _nome_seguro(nome: str) -> str:
    return re.sub(r"[^\w\-. ]", "_", nome or "documento")[:120]


def _seq_int(seq) -> int:
    try:
        return int(seq)
    except (TypeError, ValueError):
        return 10**9  # sem seq numérica válida: joga pro final, nunca quebra a ordenação


def _e_formulario_paj(descricao: str) -> bool:
    """O "Formulário do PAJ" (descrição do SISDPU começando com "PAJ", ex.:
    "PAJ - SEM ASSINATURA") sempre vem primeiro na sua sequência — mesmo
    critério do PAINEL SISDPU 2.0 (`_e_formulario_paj`, services/
    download_completo_service.py), para o PDF sair na mesma ordem de lá."""
    t = "".join(c for c in unicodedata.normalize("NFD", descricao or "") if unicodedata.category(c) != "Mn").strip().lower()
    return t.startswith("paj") or "formulario do paj" in t or "formulario paj" in t


def _html_para_texto(html: str) -> str:
    if not html:
        return ""
    txt = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    txt = re.sub(r"</p>", "\n\n", txt, flags=re.IGNORECASE)
    txt = re.sub(r"<[^>]+>", "", txt)
    return unescape(txt).strip()


def _quebrar_linhas(texto: str, largura_max: float, fontsize: float, fontname: str = FONTE) -> list[str]:
    """Quebra `texto` em linhas que cabem em `largura_max` pontos, respeitando
    quebras de linha já existentes no texto."""
    linhas_finais: list[str] = []
    for paragrafo in (texto or "").split("\n"):
        if not paragrafo.strip():
            linhas_finais.append("")
            continue
        palavras = paragrafo.split(" ")
        linha_atual = ""
        for palavra in palavras:
            candidata = (linha_atual + " " + palavra).strip()
            # Soma caractere a caractere, não a string inteira de uma vez —
            # `fitz.get_text_length` subestima a largura de certas combinações de
            # caracteres acentuados (confirmado em services/levantamento_sat_service.py,
            # 2026-11-13), o que deixaria linhas passarem da margem.
            if sum(fitz.get_text_length(c, fontname=fontname, fontsize=fontsize)
                   for c in candidata) <= largura_max:
                linha_atual = candidata
            else:
                if linha_atual:
                    linhas_finais.append(linha_atual)
                linha_atual = palavra
        if linha_atual:
            linhas_finais.append(linha_atual)
    return linhas_finais


def _nova_pagina(doc: fitz.Document, titulo: str) -> tuple[fitz.Page, float]:
    page = doc.new_page(width=A4.width, height=A4.height)
    y = MARGEM
    page.insert_text((MARGEM, y + 14), titulo, fontsize=16, fontname=FONTE_NEGRITO)
    y += 30
    page.draw_line((MARGEM, y), (A4.width - MARGEM, y), color=(0.6, 0.6, 0.6), width=0.8)
    y += 20
    return page, y


def _escrever_texto_paginado(doc: fitz.Document, titulo: str, texto: str, fontsize: float = 10) -> None:
    largura_util = A4.width - 2 * MARGEM
    linhas = _quebrar_linhas(texto, largura_util, fontsize)
    page, y = _nova_pagina(doc, titulo)
    altura_linha = fontsize * 1.35
    for linha in linhas:
        if y + altura_linha > A4.height - MARGEM:
            page = doc.new_page(width=A4.width, height=A4.height)
            y = MARGEM
        if linha:
            page.insert_text((MARGEM, y), linha, fontsize=fontsize, fontname=FONTE)
        y += altura_linha


def _escrever_capa(doc: fitz.Document, numero_paj: str, dados: dict) -> None:
    page, y = _nova_pagina(doc, "PAJ Completo")
    # Só os campos com extração confiável (teste real, 2026-11-13: "Ofício" e
    # "Juízo/Órgão Julgador" vêm trocados/errados em alguns PAJs — mesma
    # heurística frágil de `valorParaLabel` usada também no PAINEL SISDPU
    # 2.0 — por isso ficam de fora da capa).
    campos = [
        ("Número do PAJ", numero_paj),
        ("Status", dados.get("status_paj", "")),
        ("Assistido", dados.get("assistido", "")),
        ("CPF", dados.get("cpf_assistido", "")),
        ("Pretensão", dados.get("pretensao", "")),
        ("Data de abertura", dados.get("data_abertura", "")),
        ("Processo judicial", dados.get("processo_judicial", "")),
    ]
    for rotulo, valor in campos:
        if not valor:
            continue
        page.insert_text((MARGEM, y), f"{rotulo}:", fontsize=10, fontname=FONTE_NEGRITO)
        page.insert_text((MARGEM + 165, y), str(valor), fontsize=10, fontname=FONTE)
        y += 18
    page.insert_text(
        (MARGEM, A4.height - MARGEM),
        f"Gerado pelo Painel SAT Central em {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        fontsize=8, fontname=FONTE, color=(0.5, 0.5, 0.5),
    )


def _escrever_historico(doc: fitz.Document, movimentacoes: list[dict]) -> None:
    if not movimentacoes:
        return
    col_seq, col_data, col_mov, col_fases = MARGEM, MARGEM + 35, MARGEM + 115, MARGEM + 320
    fontsize = 8.5
    altura_linha = 14
    page, y = _nova_pagina(doc, "Histórico de Movimentações")

    def cabecalho(pagina: fitz.Page, y0: float) -> float:
        pagina.insert_text((col_seq, y0), "Seq.", fontsize=fontsize, fontname=FONTE_NEGRITO)
        pagina.insert_text((col_data, y0), "Data/Hora", fontsize=fontsize, fontname=FONTE_NEGRITO)
        pagina.insert_text((col_mov, y0), "Movimentação", fontsize=fontsize, fontname=FONTE_NEGRITO)
        pagina.insert_text((col_fases, y0), "Fases/Usuário", fontsize=fontsize, fontname=FONTE_NEGRITO)
        y0 += altura_linha
        pagina.draw_line((MARGEM, y0 - 3), (A4.width - MARGEM, y0 - 3), color=(0.7, 0.7, 0.7), width=0.5)
        return y0

    y = cabecalho(page, y)
    for mov in movimentacoes:
        if y + altura_linha > A4.height - MARGEM:
            page = doc.new_page(width=A4.width, height=A4.height)
            y = cabecalho(page, MARGEM)
        page.insert_text((col_seq, y), str(mov.get("seq", "")), fontsize=fontsize, fontname=FONTE)
        page.insert_text((col_data, y), mov.get("data", ""), fontsize=fontsize, fontname=FONTE)
        page.insert_text((col_mov, y), (mov.get("movimentacao", "") or "")[:45], fontsize=fontsize, fontname=FONTE)
        fases_usuario = " / ".join(x for x in [mov.get("fases", ""), mov.get("usuario", "")] if x)
        page.insert_text((col_fases, y), fases_usuario[:55], fontsize=fontsize, fontname=FONTE)
        y += altura_linha


def _pagina_separadora(doc: fitz.Document, seq: str, mov: dict, n_docs: int) -> None:
    page = doc.new_page(width=A4.width, height=A4.height)
    page.draw_rect(A4, color=None, fill=(0.95, 0.95, 0.97))
    y = A4.height / 2 - 40
    page.insert_text((MARGEM, y), f"Movimentação Seq. {seq}", fontsize=18, fontname=FONTE_NEGRITO)
    y += 26
    descricao = f"{mov.get('data', '')} — {mov.get('movimentacao', '')}".strip(" —")
    if descricao:
        page.insert_text((MARGEM, y), descricao, fontsize=11, fontname=FONTE)
        y += 18
    fases_usuario = " / ".join(x for x in [mov.get("fases", ""), mov.get("usuario", "")] if x)
    if fases_usuario:
        page.insert_text((MARGEM, y), fases_usuario, fontsize=10, fontname=FONTE, color=(0.4, 0.4, 0.4))
        y += 18
    page.insert_text((MARGEM, y), f"{n_docs} documento(s) anexado(s) nesta sequência",
                      fontsize=10, fontname=FONTE, color=(0.4, 0.4, 0.4))


def _anexar_informativo(doc: fitz.Document, titulo: str, detalhe: str) -> None:
    page = doc.new_page(width=A4.width, height=A4.height)
    page.insert_text((MARGEM, MARGEM + 20), titulo, fontsize=12, fontname=FONTE_NEGRITO, color=(0.6, 0.1, 0.1))
    page.insert_text((MARGEM, MARGEM + 45), detalhe, fontsize=10, fontname=FONTE)


def _anexar_item(doc: fitz.Document, caminho: Path | None, nome_arquivo: str) -> None:
    if not caminho or not caminho.exists():
        _anexar_informativo(doc, "Documento não baixado", nome_arquivo)
        return
    ext = caminho.suffix.lower().lstrip(".")
    try:
        if ext == "pdf":
            src = fitz.open(str(caminho))
            doc.insert_pdf(src)
            src.close()
        elif ext in IMAGENS_EXT:
            page = doc.new_page(width=A4.width, height=A4.height)
            page.insert_image(fitz.Rect(MARGEM, MARGEM, A4.width - MARGEM, A4.height - MARGEM),
                               filename=str(caminho))
        else:
            _anexar_informativo(doc, "Formato não incorporado ao PDF", f"{nome_arquivo} ({ext or '?'})")
    except Exception as e:  # noqa: BLE001
        log.warning("falha ao anexar %s: %r", nome_arquivo, e)
        _anexar_informativo(doc, "Falha ao abrir o documento", nome_arquivo)


_RE_PAJ_NORM = re.compile(r"PAJ-(\d{4})-(\d{3})-(\d+)")


async def gerar_pdf_paj_completo(paj_norm: str) -> tuple[bytes, str]:
    """Gera o PDF completo do PAJ `paj_norm` (formato "PAJ-AAAA-UUU-NNNNN").
    Retorna (pdf_bytes, nome_sugerido_do_arquivo). Levanta `PajCompletoErro`
    em qualquer falha (PAJ não encontrado, sessão SISDPU indisponível etc.)."""
    m = _RE_PAJ_NORM.match(paj_norm or "")
    if not m:
        raise PajCompletoErro(f"PAJ inválido: {paj_norm!r}")
    ano, unidade, numero = m.group(1), m.group(2), m.group(3)
    numero_fmt = f"{ano}/{unidade}-{numero}"

    nav = await sisdpu_client.buscar_paj_global(numero, ano, unidade, forcar_sessao=False)
    if nav.get("erro"):
        raise PajCompletoErro(f"Não foi possível abrir o PAJ no SISDPU: {nav['erro']}")

    docs = await sisdpu_client.extrair_documentos_por_seq()
    if docs.get("erro"):
        raise PajCompletoErro(f"Não foi possível listar os documentos das movimentações: {docs['erro']}")

    # O SISDPU lista as movimentações do mais recente para o mais antigo (tela
    # de caixa de entrada) — mas o PAINEL SISDPU 2.0 monta o PDF em ordem
    # CRONOLÓGICA (seq crescente, do nº 1 em diante), tanto no histórico
    # quanto na ordem dos separadores/anexos (`sorted(grupos)` em
    # `download_completo_service.py`). Reordena aqui pra sair igual (pedido
    # do usuário, 2026-11-13).
    movimentacoes = sorted(nav.get("movimentacoes") or [], key=lambda mv: _seq_int(mv.get("seq")))
    mov_por_seq = {str(mv.get("seq", "")): mv for mv in movimentacoes}
    movs_com_docs = sorted(docs.get("movimentacoes_com_documentos") or [],
                            key=lambda mv: _seq_int(mv.get("seq")))

    tmp = Path(tempfile.mkdtemp(prefix="paj_completo_"))
    try:
        doc = fitz.open()
        _escrever_capa(doc, numero_fmt, nav)
        narrativa = _html_para_texto(nav.get("narrativa", ""))
        if narrativa:
            _escrever_texto_paginado(doc, "Narrativa", narrativa)
        _escrever_historico(doc, movimentacoes)

        for mv_doc in movs_com_docs:
            seq = str(mv_doc.get("seq", ""))
            # Formulário do PAJ sempre primeiro na sua sequência — mesmo
            # critério do PAINEL SISDPU 2.0.
            documentos = sorted(mv_doc.get("documentos") or [],
                                 key=lambda d: 0 if _e_formulario_paj(d.get("descricao") or d.get("nomeArquivo") or "") else 1)
            mov_info = mov_por_seq.get(seq) or {
                "data": mv_doc.get("dataHora", ""), "movimentacao": mv_doc.get("movimentacao", ""),
            }
            _pagina_separadora(doc, seq, mov_info, len(documentos))
            for d in documentos:
                nome = d.get("nomeArquivo") or "documento"
                url = d.get("urlDownload") or ""
                destino = tmp / f"seq{seq}_{_nome_seguro(nome)}"
                caminho: Path | None = None
                if url.startswith("http"):
                    tamanho = await sisdpu_client.baixar_arquivo_direto(url, destino)
                    if tamanho > 0:
                        caminho = destino
                elif url.startswith("sisdpu-commandlink:"):
                    caminho = await sisdpu_client.baixar_documento_via_commandlink(seq, nome, destino)
                _anexar_item(doc, caminho, nome)

        pdf_bytes = doc.tobytes(garbage=4, deflate=True)
        doc.close()
        nome_sugerido = f"{numero_fmt.replace('/', '-')} - PAJ Completo.pdf"
        return pdf_bytes, nome_sugerido
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
