#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
estadisticas.py
----------------
Registra METADATOS agregados de las tareas que procesa webhook_server.py
(que tipo de auditoria se pidio, cuando, si se entrego bien) -- nunca el
contenido que manda el cliente (titulo/descripcion/requisitos/reporte).

Esto respeta la politica de retencion de datos de _procesar_tarea() en
webhook_server.py (el bot no guarda informacion del cliente una vez
entregado el reporte): task_id es un identificador opaco que asigna
Moltify, no texto que haya escrito el cliente, y tipo_skill/estado son
datos derivados sobre el servicio prestado, no el contenido en si.

REGLA DE DISENO (la misma que en bot.py): vive en disco (sqlite3), nunca
en una estructura de Python que crezca en RAM.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

RUTA_BASE_DATOS = Path(__file__).parent / "estadisticas.sqlite3"

ESTADOS_VALIDOS = {"entregada", "fallida", "prueba"}


def inicializar_base_datos():
    """Crea la tabla si no existe. Se llama una vez al arrancar el servidor."""
    conexion = sqlite3.connect(RUTA_BASE_DATOS)
    conexion.execute(
        """
        CREATE TABLE IF NOT EXISTS tareas_procesadas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            tipo_skill TEXT,
            estado TEXT NOT NULL,
            procesado_en TEXT NOT NULL
        )
        """
    )
    conexion.commit()
    conexion.close()


def registrar_tarea(task_id, tipo_skill, estado):
    """
    Guarda un registro con SOLO metadatos de la tarea ya procesada: el
    task_id que asigno Moltify, que tipo de auditoria se hizo (o None si
    fue una tarea de prueba), y el estado final. Abre y cierra la conexion
    en cada llamada a proposito, igual que el resto del proyecto (bot.py)
    -- evita mantener un objeto de conexion viviendo indefinidamente.
    """
    if estado not in ESTADOS_VALIDOS:
        estado = "fallida"
    conexion = sqlite3.connect(RUTA_BASE_DATOS)
    conexion.execute(
        "INSERT INTO tareas_procesadas (task_id, tipo_skill, estado, procesado_en) VALUES (?, ?, ?, ?)",
        (task_id, tipo_skill, estado, datetime.now(timezone.utc).isoformat()),
    )
    conexion.commit()
    conexion.close()


def obtener_resumen(limite_recientes=20):
    """
    Devuelve los conteos para el dashboard: total de tareas procesadas,
    desglose por estado, desglose por tipo de skill (que servicio se pide
    mas -- solo entre las entregadas con exito), y las ultimas N tareas
    (task_id + metadatos, nunca contenido del cliente).
    """
    conexion = sqlite3.connect(RUTA_BASE_DATOS)
    conexion.row_factory = sqlite3.Row

    total = conexion.execute("SELECT COUNT(*) AS n FROM tareas_procesadas").fetchone()["n"]

    por_estado = conexion.execute(
        "SELECT estado, COUNT(*) AS n FROM tareas_procesadas GROUP BY estado ORDER BY n DESC"
    ).fetchall()

    por_skill = conexion.execute(
        """
        SELECT tipo_skill, COUNT(*) AS n FROM tareas_procesadas
        WHERE estado = 'entregada' AND tipo_skill IS NOT NULL
        GROUP BY tipo_skill ORDER BY n DESC
        """
    ).fetchall()

    recientes = conexion.execute(
        """
        SELECT task_id, tipo_skill, estado, procesado_en FROM tareas_procesadas
        ORDER BY id DESC LIMIT ?
        """,
        (limite_recientes,),
    ).fetchall()

    conexion.close()

    return {
        "total": total,
        "por_estado": [dict(fila) for fila in por_estado],
        "por_skill": [dict(fila) for fila in por_skill],
        "recientes": [dict(fila) for fila in recientes],
    }
