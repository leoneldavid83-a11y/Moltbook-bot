#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
moltjobs_heartbeat.py
-----------------------
Manda un heartbeat a MoltJobs para que el agente davlerd figure "online"
en su dashboard. Sin esto, lastHeartbeatAt queda en null indefinidamente
y el agente aparece offline aunque el resto de la infraestructura este
funcionando.

Este es un script de una sola pasada, pensado para correr por cron
periodicamente (ver crontab en la VM) -- no es un proceso de larga
duracion como webhook_server.py.
"""

import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

MOLTJOBS_API_KEY = os.getenv("MOLTJOBS_API_KEY")
if not MOLTJOBS_API_KEY:
    print("[HEARTBEAT] Falta MOLTJOBS_API_KEY en el .env.")
    sys.exit(1)

try:
    respuesta = requests.post(
        "https://api.moltjobs.io/v1/agents/heartbeat",
        headers={"Authorization": f"Bearer {MOLTJOBS_API_KEY}"},
        json={"statusReport": "idle"},
        timeout=15,
    )
    respuesta.raise_for_status()
    print("[HEARTBEAT] OK")
except requests.exceptions.RequestException:
    print("[HEARTBEAT] Error de conexion al mandar el heartbeat.")
    sys.exit(1)
