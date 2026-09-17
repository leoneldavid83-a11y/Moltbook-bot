#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
moltjobs_discover.py
----------------------
Script de una sola pasada (pensado para correr por systemd timer, igual
que moltjobs_heartbeat.py) que busca trabajos abiertos del vertical
SECURITY_AUDIT en MoltJobs, y puja en los que todavia no se pujo.

MoltJobs no tiene un evento de webhook para "trabajo nuevo publicado"
(solo job.updated y message.created, ver webhook_server.py) -- por eso
hace falta este polling periodico para el descubrimiento, a diferencia
de Moltify donde todo llega por webhook. La guia de MoltJobs pide evitar
polling en loop cerrado; correr esto cada 15-30 min por systemd timer
(no en un loop propio) respeta eso.

No ejecuta el trabajo en si -- eso pasa cuando llega el evento
job.updated con status=ASSIGNED via webhook_server.py.

REGLA DE DISENO (la misma que en el resto del proyecto): sin estado que
crezca en RAM. moltjobs_state.py (sqlite) es lo unico que persiste entre
corridas, para no pujar dos veces por el mismo trabajo.
"""

import os
import sys

import anthropic
from dotenv import load_dotenv

import moltjobs_marketplace as mercado
import moltjobs_state as estado

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
if not ANTHROPIC_API_KEY:
    raise RuntimeError("Falta ANTHROPIC_API_KEY en el .env.")

cliente_anthropic = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

MODELO_LLM = "claude-sonnet-5"
NIVEL_ESFUERZO_PITCH = "low"

# ETA por defecto para la propuesta: un audit real de OWASP tarda mas
# que unos minutos, igual que se le aclara al cliente en Moltify.
ETA_MINUTOS_POR_DEFECTO = 180

SYSTEM_PROMPT_PITCH = """
You are davlerd, an AI agent bidding for a security-audit job on the
MoltJobs marketplace. Given the job's title and description, write a
short bid pitch (2-3 sentences max, no markdown) explaining that you'll
run a structured, OWASP Secure Agent Playbook-based audit and deliver a
findings report ranked by severity. Be concrete about the job's actual
content, not generic. Output ONLY the pitch text, nothing else.
""".strip()


def _armar_pitch(titulo, descripcion):
    try:
        respuesta = cliente_anthropic.messages.create(
            model=MODELO_LLM,
            max_tokens=200,
            system=SYSTEM_PROMPT_PITCH,
            messages=[{"role": "user", "content": f"Title: {titulo}\n\nDescription: {descripcion}"}],
            output_config={"effort": NIVEL_ESFUERZO_PITCH},
        )
        if respuesta.stop_reason == "refusal":
            return None
        bloque_texto = next((b.text for b in respuesta.content if b.type == "text"), None)
        return bloque_texto.strip() if bloque_texto else None
    except (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError):
        print("[DISCOVER] Error de la API de Anthropic al armar el pitch.")
        return None


def correr():
    estado.inicializar_base_datos()

    try:
        trabajos = mercado.listar_trabajos_abiertos()
    except Exception:
        print("[DISCOVER] Error al listar trabajos abiertos.")
        return

    print(f"[DISCOVER] {len(trabajos)} trabajo(s) abierto(s) en el vertical {mercado.VERTICAL}.")

    for trabajo in trabajos:
        job_id = trabajo.get("id")
        if not job_id or estado.ya_se_pujo(job_id):
            continue

        # Los bounties automaticos de referidos (y cualquier otro modo
        # que no requiera puja) no encajan con el flujo de bidding.
        if trabajo.get("participationMode") not in (None, "MANUAL_SELECTION", "BID"):
            continue

        titulo = trabajo.get("title", "")
        descripcion = (trabajo.get("inputData") or {}).get("generalDescription", "") or titulo
        presupuesto = trabajo.get("budgetUsdc")

        print(f"[DISCOVER] Trabajo nuevo: {job_id} -- {titulo!r} (presupuesto {presupuesto} USDC)")

        pitch = _armar_pitch(titulo, descripcion)
        if not pitch:
            print(f"[DISCOVER] No se pudo armar un pitch para {job_id}; se omite esta corrida.")
            continue

        try:
            mercado.pujar(job_id, presupuesto, pitch, eta_minutos=ETA_MINUTOS_POR_DEFECTO)
            estado.marcar_estado(job_id, "pujado")
            print(f"[DISCOVER] Puja enviada para {job_id}.")
        except Exception:
            print(f"[DISCOVER] Error al pujar por {job_id}; no se registra como pujado (se reintenta despues).")


if __name__ == "__main__":
    correr()
