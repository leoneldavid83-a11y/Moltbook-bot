#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
reporte_pdf.py
---------------
Genera el PDF de marca "Davlerd" a partir del reporte markdown que produce
auditoria.py. Basado en la skill "davlerd-cybersecurity-report" (generate_
report.py) que armo el usuario -- usa reportlab directamente en vez de
markdown+xhtml2pdf: reportlab ajusta el texto dentro de celdas de tabla de
forma nativa (Paragraph flowables), sin los problemas de CSS no soportado
ni desborde de columnas que se pelearon con xhtml2pdf antes.

Cambios de integracion sobre el script original de la skill:
- Se registra la fuente DejaVu Sans (ya en fonts/ del repo) para que
  simbolos Unicode como >= o -> se dibujen bien -- los estilos base de
  reportlab (Helvetica) no cubren esos caracteres, mismo problema que
  tuvimos con xhtml2pdf antes.
- build_pdf_bytes() envuelve build_pdf() para devolver bytes en memoria
  (BytesIO) en vez de escribir a un archivo, para integrarse con
  webhook_server.py sin tocar disco.
- El resto (parser de markdown, estilos, portada, resaltado de riesgo,
  extraccion automatica del "asunto" desde el primer encabezado del .md)
  es el diseño original de la skill, sin cambios de logica.

REGLA DE DISENO (la misma que en el resto del proyecto): no acumula
estado en RAM. Cada PDF se genera, se devuelve como bytes, y se olvida.
"""

import difflib
import io
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable, Image, ListFlowable, ListItem, PageBreak, Paragraph,
    SimpleDocTemplate, Spacer, Table, TableStyle,
)

# ---------------------------------------------------------------------------
# Fuente Unicode: se registra una sola vez al importar el modulo. Sin esto,
# reportlab usa Helvetica por defecto, que no tiene glifos para simbolos
# como >= o -> que Claude a veces usa en los reportes (salen como cuadros
# en blanco, el mismo problema que se encontro con xhtml2pdf).
# ---------------------------------------------------------------------------
RUTA_FUENTES = Path(__file__).parent / "fonts"
NOMBRE_FUENTE = "Helvetica"
NOMBRE_FUENTE_BOLD = "Helvetica-Bold"

_ruta_regular = RUTA_FUENTES / "DejaVuSans.ttf"
_ruta_bold = RUTA_FUENTES / "DejaVuSans-Bold.ttf"
if _ruta_regular.is_file() and _ruta_bold.is_file():
    try:
        pdfmetrics.registerFont(TTFont("DejaVuSans", str(_ruta_regular)))
        pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", str(_ruta_bold)))
        NOMBRE_FUENTE = "DejaVuSans"
        NOMBRE_FUENTE_BOLD = "DejaVuSans-Bold"
    except Exception:
        print("[PDF] No se pudo registrar DejaVu Sans; se usa Helvetica (menor cobertura Unicode).")


def format_date_en(dt: datetime) -> str:
    meses = {
        1: "January", 2: "February", 3: "March", 4: "April", 5: "May", 6: "June",
        7: "July", 8: "August", 9: "September", 10: "October", 11: "November", 12: "December",
    }
    return f"Generated on {meses[dt.month]} {dt.day}, {dt.year}"


# ---------------------------------------------------------------------------
# Marca / estilo
# ---------------------------------------------------------------------------
BRAND_NAME = "DAVLERD"
BRAND_TAGLINE = "Cybersecurity Bot"
DOC_SUBTITLE = "Cybersecurity Audit Report"

PRIMARY_DARK = colors.HexColor("#0B1B33")
ACCENT = colors.HexColor("#00C2A8")
TEXT_GRAY = colors.HexColor("#3A3A3A")

RISK_COLORS = {
    "critical": colors.HexColor("#7A0C0C"),
    "high": colors.HexColor("#C0392B"),
    "medium": colors.HexColor("#E08E0B"),
    "moderate": colors.HexColor("#E08E0B"),
    "low": colors.HexColor("#2E8B57"),
    "informational": colors.HexColor("#5B7DB1"),
    "critico": colors.HexColor("#7A0C0C"),
    "crítico": colors.HexColor("#7A0C0C"),
    "alto": colors.HexColor("#C0392B"),
    "medio": colors.HexColor("#E08E0B"),
    "moderado": colors.HexColor("#E08E0B"),
    "bajo": colors.HexColor("#2E8B57"),
    "informativo": colors.HexColor("#5B7DB1"),
}

DEFAULT_SUBJECT = "Findings Report"
DEFAULT_LOGO = Path(__file__).parent / "assets" / "davlerd-logo.png"

_MD_INLINE_STRIP_RE = re.compile(r"[*_`#]")


def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    return s


def is_redundant_with_subtitle(subject: str) -> bool:
    norm_subject = _normalize(subject)
    norm_subtitle = _normalize(DOC_SUBTITLE)
    if not norm_subject:
        return True
    if norm_subject == norm_subtitle or norm_subtitle.startswith(norm_subject) or norm_subject in norm_subtitle:
        return True
    ratio = difflib.SequenceMatcher(None, norm_subject, norm_subtitle).ratio()
    return ratio >= 0.75


def extract_subject_from_md(md_text: str):
    for raw_line in md_text.replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        h_match = re.match(r"^#{1,4}\s+(.*)$", line)
        if h_match:
            title = _MD_INLINE_STRIP_RE.sub("", h_match.group(1)).strip()
            if title:
                return title
    return None


# ---------------------------------------------------------------------------
# Parser Markdown -> flowables de reportlab (subset suficiente para reportes)
# ---------------------------------------------------------------------------

def inline_markup(text: str) -> str:
    text = text.strip()
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*(.+?)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"`(.+?)`", r"<font face='Courier'>\1</font>", text)

    def _risk_sub(match):
        word = match.group(0)
        color = RISK_COLORS.get(word.lower())
        if color:
            return f"<b><font color='{color.hexval()}'>{word}</font></b>"
        return word

    pattern = r"\b(" + "|".join(sorted(set(RISK_COLORS.keys()), key=len, reverse=True)) + r")\b"
    text = re.sub(pattern, _risk_sub, text, flags=re.IGNORECASE)
    return text


def parse_table_block(lines, start_idx):
    rows = []
    i = start_idx
    while i < len(lines) and lines[i].strip().startswith("|"):
        row = [c.strip() for c in lines[i].strip().strip("|").split("|")]
        if not re.fullmatch(r"\s*-{2,}\s*", "".join(row)) and not all(
            re.fullmatch(r":?-{2,}:?", c) for c in row
        ):
            rows.append(row)
        i += 1
    return rows, i


def markdown_to_flowables(md_text: str, styles):
    lines = md_text.replace("\r\n", "\n").split("\n")
    flow = []
    i = 0
    pending_list = []
    pending_list_type = None

    def flush_list():
        nonlocal pending_list, pending_list_type
        if pending_list:
            items = [ListItem(Paragraph(inline_markup(t), styles["Body"])) for t in pending_list]
            if pending_list_type == "ul":
                flow.append(ListFlowable(items, bulletType="bullet", leftIndent=18))
            else:
                flow.append(ListFlowable(items, bulletType="1", start="1", leftIndent=18))
            flow.append(Spacer(1, 6))
        pending_list = []
        pending_list_type = None

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            flush_list()
            i += 1
            continue

        if stripped.startswith("|"):
            flush_list()
            rows, next_i = parse_table_block(lines, i)
            if rows:
                table_data = [
                    [Paragraph(f"<b>{inline_markup(c)}</b>", styles["TableHead"]) for c in rows[0]]
                ]
                for r in rows[1:]:
                    table_data.append([Paragraph(inline_markup(c), styles["TableCell"]) for c in r])
                col_count = max(len(r) for r in rows)
                col_width = (LETTER[0] - 1.6 * inch) / col_count
                t = Table(table_data, colWidths=[col_width] * col_count, repeatRows=1)
                t.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), PRIMARY_DARK),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CCCCCC")),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F6F8")]),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]))
                flow.append(t)
                flow.append(Spacer(1, 10))
            i = next_i
            continue

        h_match = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if h_match:
            flush_list()
            level = len(h_match.group(1))
            text = inline_markup(h_match.group(2))
            style_name = {1: "H1", 2: "H2", 3: "H3", 4: "H4"}.get(level, "H4")
            flow.append(Paragraph(text, styles[style_name]))
            if level <= 2:
                flow.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#DADEE3"), spaceAfter=8))
            flow.append(Spacer(1, 4))
            i += 1
            continue

        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", stripped):
            flush_list()
            flow.append(HRFlowable(width="100%", thickness=0.75, color=colors.HexColor("#DADEE3")))
            flow.append(Spacer(1, 8))
            i += 1
            continue

        ul_match = re.match(r"^[-*+]\s+(.*)$", stripped)
        ol_match = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if ul_match or ol_match:
            list_type = "ul" if ul_match else "ol"
            if pending_list_type and pending_list_type != list_type:
                flush_list()
            pending_list_type = list_type
            pending_list.append((ul_match or ol_match).group(1))
            i += 1
            continue

        flush_list()
        para_lines = [stripped]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(
            r"^(#{1,4}\s|\||[-*+]\s|\d+[.)]\s|-{3,}$|\*{3,}$|_{3,}$)", lines[i].strip()
        ):
            para_lines.append(lines[i].strip())
            i += 1
        flow.append(Paragraph(inline_markup(" ".join(para_lines)), styles["Body"]))
        flow.append(Spacer(1, 6))

    flush_list()
    return flow


# ---------------------------------------------------------------------------
# Estilos
# ---------------------------------------------------------------------------

def build_styles():
    base = getSampleStyleSheet()
    styles = {}
    styles["Body"] = ParagraphStyle(
        "Body", parent=base["Normal"], fontName=NOMBRE_FUENTE, fontSize=10.2, leading=14.5,
        textColor=TEXT_GRAY, spaceAfter=2, alignment=TA_LEFT,
    )
    styles["H1"] = ParagraphStyle(
        "H1", parent=base["Heading1"], fontName=NOMBRE_FUENTE_BOLD, fontSize=17, leading=21,
        textColor=PRIMARY_DARK, spaceBefore=14, spaceAfter=4,
    )
    styles["H2"] = ParagraphStyle(
        "H2", parent=base["Heading2"], fontName=NOMBRE_FUENTE_BOLD, fontSize=14, leading=18,
        textColor=PRIMARY_DARK, spaceBefore=12, spaceAfter=4,
    )
    styles["H3"] = ParagraphStyle(
        "H3", parent=base["Heading3"], fontName=NOMBRE_FUENTE_BOLD, fontSize=12, leading=16,
        textColor=PRIMARY_DARK, spaceBefore=10, spaceAfter=3,
    )
    styles["H4"] = ParagraphStyle(
        "H4", parent=base["Heading4"], fontName=NOMBRE_FUENTE_BOLD, fontSize=11, leading=15,
        textColor=PRIMARY_DARK, spaceBefore=8, spaceAfter=3,
    )
    styles["TableHead"] = ParagraphStyle(
        "TableHead", parent=base["Normal"], fontName=NOMBRE_FUENTE_BOLD, fontSize=9.5, leading=12,
        textColor=colors.white,
    )
    styles["TableCell"] = ParagraphStyle(
        "TableCell", parent=base["Normal"], fontName=NOMBRE_FUENTE, fontSize=9.5, leading=12.5,
        textColor=TEXT_GRAY,
    )
    styles["CoverTitle"] = ParagraphStyle(
        "CoverTitle", parent=base["Title"], fontName=NOMBRE_FUENTE_BOLD, fontSize=30, leading=34,
        textColor=colors.white, alignment=TA_CENTER, spaceAfter=6,
    )
    # Variante para cuando el nombre va al lado del logo (no solo, centrado
    # arriba): alineada a la izquierda dentro de su celda de la tabla.
    styles["CoverTitleConLogo"] = ParagraphStyle(
        "CoverTitleConLogo", parent=styles["CoverTitle"], alignment=TA_LEFT,
    )
    styles["CoverTagline"] = ParagraphStyle(
        "CoverTagline", parent=base["Normal"], fontName=NOMBRE_FUENTE, fontSize=11, leading=14,
        textColor=ACCENT, alignment=TA_CENTER, spaceAfter=40,
    )
    styles["CoverSubtitle"] = ParagraphStyle(
        "CoverSubtitle", parent=base["Normal"], fontName=NOMBRE_FUENTE, fontSize=16, leading=20,
        textColor=colors.white, alignment=TA_CENTER, spaceAfter=10,
    )
    styles["CoverClient"] = ParagraphStyle(
        "CoverClient", parent=base["Normal"], fontName=NOMBRE_FUENTE, fontSize=18, leading=22,
        textColor=colors.white, alignment=TA_CENTER, spaceBefore=4, spaceAfter=4,
    )
    styles["CoverDate"] = ParagraphStyle(
        "CoverDate", parent=base["Normal"], fontName=NOMBRE_FUENTE, fontSize=10, leading=13,
        textColor=colors.HexColor("#B7C4D6"), alignment=TA_CENTER, spaceBefore=60,
    )
    return styles


# ---------------------------------------------------------------------------
# Portada y paginas
# ---------------------------------------------------------------------------

def cover_page(subject, styles, logo_path=None):
    flow = []
    flow.append(Spacer(1, 1.4 * inch))

    logo_colocado = False
    if logo_path and os.path.isfile(logo_path):
        try:
            tamano_logo = 0.85 * inch
            imagen = Image(logo_path, width=tamano_logo, height=tamano_logo)
            titulo_con_logo = Paragraph(BRAND_NAME, styles["CoverTitleConLogo"])
            # Tabla de 2 columnas sin bordes: logo | nombre, centrada en
            # conjunto en la pagina (no cada celda por separado). El ancho
            # de la 2da columna es None para que se ajuste al texto.
            fila_titulo = Table(
                [[imagen, titulo_con_logo]],
                colWidths=[tamano_logo + 14, None],
                hAlign="CENTER",
            )
            fila_titulo.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (0, 0), "CENTER"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]))
            flow.append(fila_titulo)
            flow.append(Spacer(1, 18))
            logo_colocado = True
        except Exception:
            pass

    if not logo_colocado:
        flow.append(Paragraph(BRAND_NAME, styles["CoverTitle"]))

    flow.append(Paragraph(BRAND_TAGLINE, styles["CoverTagline"]))
    flow.append(Paragraph(DOC_SUBTITLE, styles["CoverSubtitle"]))
    if not is_redundant_with_subtitle(subject):
        flow.append(Paragraph(subject, styles["CoverClient"]))
    flow.append(Paragraph(format_date_en(datetime.now()), styles["CoverDate"]))
    flow.append(PageBreak())
    return flow


def _cover_background(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(PRIMARY_DARK)
    canvas.rect(0, 0, LETTER[0], LETTER[1], stroke=0, fill=1)
    canvas.setFillColor(ACCENT)
    canvas.rect(0, 0, LETTER[0], 0.12 * inch, stroke=0, fill=1)
    canvas.restoreState()


def _content_page(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(PRIMARY_DARK)
    canvas.rect(0, LETTER[1] - 0.42 * inch, LETTER[0], 0.42 * inch, stroke=0, fill=1)
    canvas.setFillColor(colors.white)
    canvas.setFont(NOMBRE_FUENTE_BOLD, 9)
    canvas.drawString(0.7 * inch, LETTER[1] - 0.28 * inch, BRAND_NAME)
    canvas.setFont(NOMBRE_FUENTE, 9)
    canvas.drawRightString(LETTER[0] - 0.7 * inch, LETTER[1] - 0.28 * inch, DOC_SUBTITLE)

    canvas.setFillColor(colors.HexColor("#8A8A8A"))
    canvas.setFont(NOMBRE_FUENTE, 8)
    canvas.drawCentredString(LETTER[0] / 2, 0.4 * inch, f"Page {doc.page - 1}")
    canvas.restoreState()


def build_pdf(md_path, output_path, logo_path=None, subject=None):
    """Version original de la skill: lee un .md desde disco y escribe el PDF a disco."""
    with open(md_path, "r", encoding="utf-8") as f:
        md_text = f.read()
    return build_pdf_from_text(md_text, output_path, logo_path=logo_path, subject=subject)


def build_pdf_from_text(md_text, output, logo_path=None, subject=None):
    """
    Igual que build_pdf(), pero recibe el texto markdown directo (no una
    ruta) y `output` puede ser una ruta de archivo O un objeto tipo
    archivo (ej. io.BytesIO) -- SimpleDocTemplate de reportlab acepta
    ambos. Devuelve el "asunto" que finalmente se uso en la portada.
    """
    resolved_subject = subject or extract_subject_from_md(md_text) or DEFAULT_SUBJECT
    styles = build_styles()

    if not logo_path and DEFAULT_LOGO.is_file():
        logo_path = str(DEFAULT_LOGO)

    doc = SimpleDocTemplate(
        output, pagesize=LETTER,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        leftMargin=0.8 * inch, rightMargin=0.8 * inch,
        title=f"{DOC_SUBTITLE} - {resolved_subject}", author=BRAND_NAME,
    )

    story = []
    story.extend(cover_page(resolved_subject, styles, logo_path))
    story.extend(markdown_to_flowables(md_text, styles))

    def on_page(canvas, doc_):
        if doc_.page == 1:
            _cover_background(canvas, doc_)
        else:
            _content_page(canvas, doc_)

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return resolved_subject


def generar_pdf_desde_markdown(texto_markdown, titulo=None, fecha=None):
    """
    Punto de entrada que usa webhook_server.py. Mantiene la firma que ya
    tenia el modulo anterior para no tener que tocar el resto del pipeline.

    - titulo: si se pasa, se usa como "asunto" forzado de portada (igual
      que --subject en el script original). Si se omite (recomendado), el
      asunto se extrae solo del primer encabezado del propio reporte --
      no depende del titulo de la tarea de Moltify, que puede ser menos
      descriptivo que el encabezado real que Claude le puso al reporte.
    - fecha: no se usa (el script original siempre pone la fecha de
      generacion real); se deja el parametro solo por compatibilidad de
      firma con quien llama.

    Devuelve los bytes del PDF, o None si algo fallo.
    """
    try:
        buffer_pdf = io.BytesIO()
        build_pdf_from_text(texto_markdown, buffer_pdf, subject=titulo)
        return buffer_pdf.getvalue()
    except Exception:
        print("[PDF] Error inesperado al generar el PDF del reporte.")
        return None


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Generate the Davlerd PDF report from a .md file")
    parser.add_argument("md_path", help="Path to the findings/audit .md file")
    parser.add_argument("--logo", default=None, help="Path to a PNG/JPG logo for the cover")
    parser.add_argument("--output", default=None, help="Output PDF path")
    parser.add_argument(
        "--subject", default=None,
        help="Text to show on the cover instead of a client name. "
             "If omitted, the .md's first heading is used.",
    )
    args = parser.parse_args()

    with open(args.md_path, "r", encoding="utf-8") as f:
        subject_guess = args.subject or extract_subject_from_md(f.read()) or DEFAULT_SUBJECT

    if args.output:
        output_path = args.output
    else:
        safe_subject = re.sub(r"[^\w\-() ]+", "", subject_guess).strip() or "Report"
        safe_subject = safe_subject[:80].strip()
        output_path = f"{safe_subject} - Davlerd Cybersecurity Report.pdf"

    resolved_subject = build_pdf(args.md_path, output_path, logo_path=args.logo, subject=args.subject)
    print(f"Cover subject used: {resolved_subject}")
    if is_redundant_with_subtitle(resolved_subject):
        print("[info] Subject is redundant with the fixed subtitle; not repeated on the cover.")
    print(f"PDF generated at: {output_path}")


if __name__ == "__main__":
    main()
