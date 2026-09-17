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

import json
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
NIVEL_ESFUERZO_OFERTA = "medium"

# Nunca se puja por encima del presupuesto que puso quien publico el
# trabajo (es lo habitual en este tipo de marketplace -- el presupuesto
# es un techo, no un piso), ni por debajo de este minimo (un audit real
# con metodologia OWASP no tiene sentido regalado).
MONTO_MINIMO_USDC = 1

SYSTEM_PROMPT_OFERTA = """
You are davlerd, an AI agent evaluating whether to bid on a security-audit
job on the MoltJobs marketplace. Given the job's title, description, and
the poster's suggested budget, judge the REAL complexity of the work
(scope, likely size of the target, depth of analysis implied) and decide:

1. Whether this is a good fit to bid on at all (shouldBid).
2. A fair bid amount in USDC -- NEVER above the poster's budget, but
   below it if the job looks simpler than the budget suggests, or if
   bidding competitively makes sense with zero reputation built up yet.
3. A realistic ETA in minutes for a real OWASP Secure Agent Playbook
   audit (not a rushed one -- more items/broader scope should take
   longer, never less than 60 minutes).
4. A short bid pitch (2-3 sentences, no markdown) explaining you'll run
   a structured, OWASP-based audit and deliver a severity-ranked
   findings report. Be concrete about the job's actual content, not
   generic.

Reply with ONLY a JSON object, no markdown, no code fences:
{"shouldBid": true|false, "amountUsdc": "<number as string>", "etaMinutes": <integer>, "pitch": "<string>", "reason": "<one short sentence>"}
""".strip()


def _evaluar_oferta(titulo, descripcion, presupuesto_usdc):
    """
    Le pide a Claude que evalue la complejidad real del trabajo contra el
    presupuesto sugerido, y devuelva si conviene pujar y por cuanto.
    Devuelve un dict (ver SYSTEM_PROMPT_OFERTA) o None si algo fallo.
    """
    mensaje_usuario = (
        f"Title: {titulo}\n\nDescription: {descripcion}\n\n"
        f"Poster's suggested budget: {presupuesto_usdc} USDC"
    )
    try:
        respuesta = cliente_anthropic.messages.create(
            model=MODELO_LLM,
            max_tokens=400,
            system=SYSTEM_PROMPT_OFERTA,
            messages=[{"role": "user", "content": mensaje_usuario}],
            output_config={"effort": NIVEL_ESFUERZO_OFERTA},
        )
        if respuesta.stop_reason == "refusal":
            print("[DISCOVER] Claude rechazo evaluar este trabajo.")
            return None
        bloque_texto = next((b.text for b in respuesta.content if b.type == "text"), None)
        if not bloque_texto:
            return None
        oferta = json.loads(bloque_texto.strip())
    except (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError):
        print("[DISCOVER] Error de la API de Anthropic al evaluar el trabajo.")
        return None
    except (json.JSONDecodeError, ValueError):
        print("[DISCOVER] Claude devolvio una respuesta que no es JSON valido; se omite.")
        return None

    # El presupuesto del comprador es un techo duro, sin importar lo que
    # haya propuesto Claude -- nunca se puja por encima de eso.
    try:
        monto = float(oferta.get("amountUsdc", 0))
        techo = float(presupuesto_usdc)
    except (TypeError, ValueError):
        return None

    monto = max(MONTO_MINIMO_USDC, min(monto, techo))
    oferta["amountUsdc"] = f"{monto:.2f}"
    return oferta


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

        oferta = _evaluar_oferta(titulo, descripcion, presupuesto)
        if not oferta:
            print(f"[DISCOVER] No se pudo evaluar {job_id}; se omite esta corrida (se reintenta despues).")
            continue

        if not oferta.get("shouldBid"):
            print(f"[DISCOVER] Se decide NO pujar por {job_id}: {oferta.get('reason', 'sin motivo dado')}")
            estado.marcar_estado(job_id, "descartado")
            continue

        try:
            mercado.pujar(
                job_id, oferta["amountUsdc"], oferta.get("pitch", ""),
                eta_minutos=oferta.get("etaMinutes"),
            )
            estado.marcar_estado(job_id, "pujado")
            print(f"[DISCOVER] Puja enviada para {job_id}: {oferta['amountUsdc']} USDC, ETA {oferta.get('etaMinutes')} min.")
        except Exception:
            print(f"[DISCOVER] Error al pujar por {job_id}; no se registra como pujado (se reintenta despues).")


if __name__ == "__main__":
    correr()
