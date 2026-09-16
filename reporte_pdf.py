#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
reporte_pdf.py
---------------
Convierte el reporte markdown que genera auditoria.py en un PDF con
formato profesional, para entregar como adjunto real (no solo texto
plano) en las tareas de Moltify.

Deliberadamente pura-Python (markdown + xhtml2pdf, que usa reportlab por
debajo): nada de binarios externos ni dependencias de sistema pesadas
(WeasyPrint necesita Cairo/Pango, wkhtmltopdf es un binario aparte) -- en
una VM de 1GB conviene lo mas liviano posible.

REGLA DE DISENO (la misma que en el resto del proyecto): esto no acumula
estado en RAM. Cada PDF se genera, se devuelve como bytes, y se olvida.
"""

import io
import re

import markdown as md_lib
from bs4 import BeautifulSoup
from xhtml2pdf import pisa

# Anchos de columna (en %) para encabezados de tabla reconocidos. Los que
# suelen llevar texto/codigo largo (rutas, identificadores) necesitan mas
# espacio; los categoricos cortos (Risk Level: High/Medium/Low) necesitan
# poco. Cualquier columna no listada se reparte el espacio restante en
# partes iguales.
ANCHOS_COLUMNA_POR_ENCABEZADO = {
    "input path": 28,
    "risk level": 12,
    "location": 22,
    "confidence": 12,
    "severity": 12,
    "access level": 20,
}

# Colores por severidad, para que los hallazgos se distingan de un
# vistazo (igual que en cualquier reporte de seguridad real).
COLORES_SEVERIDAD = {
    "CRITICAL": "#b30000",
    "HIGH": "#e05d00",
    "MEDIUM": "#b8860b",
    "LOW": "#2f6db3",
    "INFORMATIONAL": "#555555",
}

PLANTILLA_HTML = """
<html>
<head>
<style>
    /* DejaVu Sans en vez de Helvetica: tiene cobertura Unicode amplia
       (flechas, simbolos matematicos, etc.) que Claude a veces usa en los
       reportes y que Helvetica no puede dibujar (salen como cuadros en
       blanco). Ya viene instalada en la VM (paquete fonts-dejavu-core). */
    @font-face {{
        font-family: "DejaVu Sans";
        src: url("fonts/DejaVuSans.ttf");
    }}
    @font-face {{
        font-family: "DejaVu Sans";
        font-weight: bold;
        src: url("fonts/DejaVuSans-Bold.ttf");
    }}
    @page {{
        size: A4;
        margin: 2.2cm 1.8cm;
        @frame footer_frame {{
            -pdf-frame-content: footer_content;
            bottom: 1cm; margin-left: 1.8cm; margin-right: 1.8cm; height: 1cm;
        }}
    }}
    body {{
        font-family: "DejaVu Sans", Helvetica, Arial, sans-serif;
        font-size: 10pt;
        line-height: 1.5;
        color: #1a1a1a;
    }}
    .portada {{
        text-align: center;
        padding-top: 6cm;
    }}
    .portada h1 {{
        font-size: 22pt;
        margin-bottom: 0.3cm;
    }}
    .portada .subtitulo {{
        font-size: 12pt;
        color: #555555;
    }}
    .portada .marca {{
        margin-top: 3cm;
        font-size: 10pt;
        color: #888888;
    }}
    h1 {{ font-size: 16pt; color: #111111; border-bottom: 2px solid #333333; padding-bottom: 4px; }}
    h2 {{ font-size: 13pt; color: #222222; margin-top: 18px; }}
    h3 {{ font-size: 11.5pt; margin-top: 14px; }}
    /* NOTA: xhtml2pdf ignora table-layout/word-wrap/overflow-wrap sin
       avisar con error, solo con un warning en consola -- no sirven aca,
       asi que no se usan. El corte de linea en identificadores largos
       (nombres de funciones con guion bajo) se resuelve insertando
       espacios de ancho cero en el HTML, ver _insertar_puntos_de_corte_en_codigo. */
    table {{ width: 100%; margin: 8px 0; }}
    th, td {{ border: 1px solid #cccccc; padding: 5px 8px; font-size: 8.5pt; text-align: left; }}
    th {{ background-color: #f0f0f0; }}
    /* Dentro de celdas de tabla, <code> con fondo propio se ve peor que
       texto monoespaciado simple cuando hace salto de linea en una
       columna angosta. Fuera de tablas si mantiene el fondo (ahi funciona
       bien, ver la regla "code" mas abajo). */
    td code, th code {{
        font-family: Courier, monospace; font-size: 8pt; background-color: transparent; padding: 0;
    }}
    code {{ background-color: #f2f2f2; padding: 1px 3px; font-family: Courier, monospace; font-size: 8.5pt; }}
    pre {{ background-color: #f2f2f2; padding: 8px; font-family: Courier, monospace; font-size: 8.5pt; }}
    hr {{ border: none; border-top: 1px solid #dddddd; margin: 14px 0; }}
    #footer_content {{ font-size: 8pt; color: #999999; text-align: center; }}
</style>
</head>
<body>
    <div class="portada">
        <h1>{titulo}</h1>
        <div class="subtitulo">Security Assessment Report</div>
        <div class="marca">Prepared by davlerd -- OWASP Secure Agent Playbook methodology<br/>{fecha}</div>
    </div>
    <pdf:nextpage />
    {cuerpo_html}
    <div id="footer_content">davlerd -- OWASP-grounded security audit -- Confidential to the requesting party</div>
</body>
</html>
"""


def _ajustar_anchos_de_columnas(html):
    """
    xhtml2pdf ignora table-layout, pero SI respeta un "width" puesto
    directo en cada celda. Para cada tabla del reporte, mira el texto de
    los encabezados y le asigna un ancho segun ANCHOS_COLUMNA_POR_ENCABEZADO
    (columnas con texto/codigo largo como "Input Path" quedan mas anchas
    que columnas cortas y categoricas como "Risk Level"). El resto del
    espacio se reparte en partes iguales entre las columnas no reconocidas.
    """
    soup = BeautifulSoup(html, "html.parser")

    for tabla in soup.find_all("table"):
        filas = tabla.find_all("tr")
        if not filas:
            continue

        celdas_encabezado = filas[0].find_all(["th", "td"])
        if not celdas_encabezado:
            continue

        anchos = []
        reconocidas = 0
        for celda in celdas_encabezado:
            clave = celda.get_text(strip=True).lower()
            ancho = ANCHOS_COLUMNA_POR_ENCABEZADO.get(clave)
            anchos.append(ancho)
            if ancho is not None:
                reconocidas += 1

        total_reconocido = sum(a for a in anchos if a is not None)
        cantidad_no_reconocidas = len(anchos) - reconocidas
        ancho_restante = max(100 - total_reconocido, 0)
        ancho_por_defecto = (
            ancho_restante / cantidad_no_reconocidas if cantidad_no_reconocidas else 0
        )

        anchos_finales = [a if a is not None else ancho_por_defecto for a in anchos]

        # Aplicar el mismo ancho a la columna en TODAS las filas, no solo
        # el encabezado, para que quede alineado en toda la tabla.
        for fila in filas:
            celdas = fila.find_all(["th", "td"])
            for indice, celda in enumerate(celdas):
                if indice < len(anchos_finales):
                    celda["style"] = f"width: {anchos_finales[indice]:.1f}%;"

    return str(soup)


def _colorear_severidades(html):
    """
    Le agrega color al texto "[CRITICAL]", "[HIGH]", etc. que ya viene en
    los titulos de los hallazgos (formato de finding.md), para que salten
    a la vista en el PDF sin tener que tocar el markdown original.
    """
    for severidad, color in COLORES_SEVERIDAD.items():
        html = html.replace(
            f"[{severidad}]",
            f'<span style="color:{color}; font-weight:bold;">[{severidad}]</span>',
        )
    return html


def _insertar_puntos_de_corte_en_codigo(html):
    """
    xhtml2pdf no soporta word-wrap/overflow-wrap (las ignora directo, sin
    error): un identificador largo sin espacios dentro de <code> (nombres
    de funciones como escuchar_comunidad) no se corta de linea solo y se
    desborda de su celda de tabla. Insertamos un guion suave (soft hyphen,
    invisible salvo que el renderer corte justo ahi, en cuyo caso se ve
    como un guion) despues de cada guion bajo -- un espacio de ancho cero
    (U+200B) se probo primero pero xhtml2pdf no tiene glifo para el en
    ninguna fuente disponible y lo dibuja como un cuadro visible, el
    problema opuesto al que se queria resolver. Solo DENTRO de los tags
    <code> ya convertidos a HTML -- nunca en el markdown crudo (ahi podria
    confundir al parser con la sintaxis de enfasis "_texto_") ni fuera de
    <code> (no hace falta, y evita tocar atributos HTML como href).
    """

    def reemplazar(coincidencia):
        contenido = coincidencia.group(1)
        return f"<code>{contenido.replace(chr(95), chr(95) + chr(173))}</code>"

    return re.sub(r"<code>(.*?)</code>", reemplazar, html, flags=re.DOTALL)


def generar_pdf_desde_markdown(texto_markdown, titulo, fecha):
    """
    Convierte un reporte en markdown (el que devuelve
    auditoria.realizar_auditoria) a PDF con portada y formato profesional.
    Devuelve los bytes del PDF, o None si algo fallo.
    """
    try:
        cuerpo_html = md_lib.markdown(
            texto_markdown, extensions=["tables", "fenced_code", "nl2br"]
        )
        cuerpo_html = _ajustar_anchos_de_columnas(cuerpo_html)
        cuerpo_html = _insertar_puntos_de_corte_en_codigo(cuerpo_html)
        cuerpo_html = _colorear_severidades(cuerpo_html)

        html_completo = PLANTILLA_HTML.format(
            titulo=titulo, fecha=fecha, cuerpo_html=cuerpo_html
        )

        buffer_pdf = io.BytesIO()
        resultado = pisa.CreatePDF(src=html_completo, dest=buffer_pdf, encoding="utf-8")

        if resultado.err:
            print("[PDF] xhtml2pdf reporto errores al generar el PDF.")
            return None

        return buffer_pdf.getvalue()

    except Exception:
        print("[PDF] Error inesperado al generar el PDF del reporte.")
        return None
