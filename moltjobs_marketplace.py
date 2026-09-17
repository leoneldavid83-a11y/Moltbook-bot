#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
moltjobs_marketplace.py
------------------------
Cliente de bajo nivel para la parte de trabajos (jobs) de la API de
MoltJobs. Lo comparten moltjobs_discover.py (descubrimiento y puja
periodica) y webhook_server.py (ejecucion cuando se asigna un trabajo).

ADVERTENCIA DE INCERTIDUMBRE: a diferencia de auditoria.py/reporte_pdf.py,
esta parte de la API de MoltJobs esta pobremente documentada -- su propio
openapi.json trae los DTOs de crear-puja (CreateBidDto) vacios (sin
listar los campos esperados), y la guia publica del repo de GitHub ya
demostro estar desactualizada varias veces (ver moltjobs_cert.py: tipos
de item, fin de examen, nombres de campo del reporte -- todos distintos
a lo documentado). Ademas, ahora mismo (17-sep-2026) NO existe ningun
trabajo real de tipo "bid" en el marketplace para probar pujar/entregar
contra datos reales -- los unicos "abiertos" son bounties automaticos de
referidos que no usan bidding. Los campos usados aca para pujar/entregar
se basan en el mejor ejemplo disponible (guides/marketplace.md del repo
de MoltJobs), pero NO estan verificados contra la API real todavia.
Cuando aparezca el primer trabajo real, revisar con atencion si algo
falla (mismo patron que ya paso dos veces con esta API).

REGLA DE DISENO (la misma que en el resto del proyecto): sin estado que
crezca en RAM -- cada llamada abre su propia conexion HTTP.
"""

import os

import requests
from dotenv import load_dotenv

load_dotenv()

MOLTJOBS_API_KEY = os.getenv("MOLTJOBS_API_KEY")
BASE_URL = "https://api.moltjobs.io/v1"

# El vertical con el que se registro el agente -- se usa para filtrar
# que trabajos considerar, sin depender de clasificacion propia: si
# MoltJobs ya etiqueto el trabajo como este vertical, alcanza.
VERTICAL = "SECURITY_AUDIT"


def _cabeceras():
    if not MOLTJOBS_API_KEY:
        raise RuntimeError("Falta MOLTJOBS_API_KEY en el .env.")
    return {"Authorization": f"Bearer {MOLTJOBS_API_KEY}"}


def _pedir(metodo, ruta, **kwargs):
    respuesta = requests.request(metodo, f"{BASE_URL}{ruta}", headers=_cabeceras(), timeout=30, **kwargs)
    respuesta.raise_for_status()
    return respuesta.json()["data"]


def listar_trabajos_abiertos(vertical=VERTICAL, limite=20):
    """Lista trabajos abiertos filtrados por vertical (lectura, endpoint publico)."""
    return _pedir("GET", "/jobs", params={"status": "open", "vertical": vertical, "limit": limite})


def obtener_trabajo(job_id):
    return _pedir("GET", f"/jobs/{job_id}")


def pujar(job_id, monto_usdc, mensaje, eta_minutos=None):
    """
    Puja por un trabajo. Formato basado en guides/marketplace.md
    (amountUsdc/message/etaMinutes) -- ver advertencia arriba del modulo,
    no verificado contra un trabajo real todavia.
    """
    cuerpo = {"amountUsdc": str(monto_usdc), "message": mensaje}
    if eta_minutos is not None:
        cuerpo["etaMinutes"] = eta_minutos
    return _pedir("POST", f"/jobs/{job_id}/bids", json=cuerpo)


def marcar_iniciado(job_id):
    """
    Marca el trabajo como iniciado. La guia describe heartbeats
    periodicos mientras se trabaja, pero /jobs/{id}/heartbeat NO existe
    en la especificacion real de la API (openapi.json) -- en cambio SI
    existe PATCH /jobs/{id}/start (no documentado en la guia). Se usa
    este para la transicion ASSIGNED -> IN_PROGRESS.
    """
    return _pedir("PATCH", f"/jobs/{job_id}/start", json={})


def entregar_trabajo(job_id, resumen):
    """
    Entrega el resultado. SubmitWorkDto (segun openapi.json) exige
    "outputData" (objeto libre, sin campos fijos documentados) -- se
    manda el reporte como texto dentro de ese objeto.
    """
    return _pedir("PATCH", f"/jobs/{job_id}/submit", json={"outputData": {"summary": resumen}})
