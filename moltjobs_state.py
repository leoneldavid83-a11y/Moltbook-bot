#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
moltjobs_state.py
-------------------
Estado local (sqlite) del pipeline de MoltJobs: que trabajos ya se
pujaron (para no pujar dos veces por el mismo) y en que estado esta cada
uno que se esta ejecutando. Guarda SOLO metadatos operativos (job_id,
estado, fecha) -- nunca contenido del cliente/comprador, mismo criterio
que estadisticas.py para Moltify.

REGLA DE DISENO (la misma que en el resto del proyecto): vive en disco,
nunca en una estructura de Python que crezca en RAM.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

RUTA_BASE_DATOS = Path(__file__).parent / "moltjobs.sqlite3"


def inicializar_base_datos():
    conexion = sqlite3.connect(RUTA_BASE_DATOS)
    conexion.execute(
        """
        CREATE TABLE IF NOT EXISTS trabajos (
            job_id TEXT PRIMARY KEY,
            estado TEXT NOT NULL,
            actualizado_en TEXT NOT NULL
        )
        """
    )
    conexion.commit()
    conexion.close()


def ya_se_pujo(job_id):
    conexion = sqlite3.connect(RUTA_BASE_DATOS)
    fila = conexion.execute("SELECT 1 FROM trabajos WHERE job_id = ?", (job_id,)).fetchone()
    conexion.close()
    return fila is not None


def obtener_estado(job_id):
    """Devuelve el estado guardado para este trabajo, o None si no hay registro."""
    conexion = sqlite3.connect(RUTA_BASE_DATOS)
    fila = conexion.execute("SELECT estado FROM trabajos WHERE job_id = ?", (job_id,)).fetchone()
    conexion.close()
    return fila[0] if fila else None


def marcar_estado(job_id, estado):
    """estado: 'pujado' | 'asignado' | 'entregado' | 'error'."""
    conexion = sqlite3.connect(RUTA_BASE_DATOS)
    conexion.execute(
        """
        INSERT INTO trabajos (job_id, estado, actualizado_en) VALUES (?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET estado = excluded.estado, actualizado_en = excluded.actualizado_en
        """,
        (job_id, estado, datetime.now(timezone.utc).isoformat()),
    )
    conexion.commit()
    conexion.close()
