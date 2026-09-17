#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
moltjobs_cert.py
-----------------
Corre un pack de evaluacion de MoltJobs (marketplace de trabajos pagados
en USDC para agentes de IA -- ver https://github.com/Moltjobs) usando
Claude para responder cada item, y certifica al agente davlerd si
aprueba. Es un script de uso puntual (como auditoria.py en modo CLI), NO
un servicio que corre indefinidamente: se ejecuta a mano cuando hace
falta certificar (o re-certificar) al agente en un pack.

Flujo de la API (ver guides/evals-and-gating.md del repo de MoltJobs):
create -> next -> answer (repetido por cada item) -> finalize -> report.
La certificacion, si aprueba, es lo que despues habilita pujar en
trabajos que la requieran (la mayoria pide "General Fundamentals").

Modo por defecto: CLOSED_BOOK (el agente responde con una sola llamada a
Claude, sin herramientas ni acceso a la web) -- es la representacion mas
honesta de como el bot genera sus respuestas hoy en produccion (ver
auditoria.py: tambien una sola llamada a messages.create(), sin tools).

Uso:
    python3 moltjobs_cert.py                                    # pack general, CLOSED_BOOK
    python3 moltjobs_cert.py pack_engineering
    python3 moltjobs_cert.py pack_general_fundamentals TOOL_ALLOWED
    python3 moltjobs_cert.py --listar-packs                     # solo lista los packs disponibles
"""

import os
import sys
import time

import anthropic
import requests
from dotenv import load_dotenv

load_dotenv()

MOLTJOBS_API_KEY = os.getenv("MOLTJOBS_API_KEY")
if not MOLTJOBS_API_KEY:
    raise RuntimeError("Falta MOLTJOBS_API_KEY en el .env: no se puede certificar sin eso.")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
if not ANTHROPIC_API_KEY:
    raise RuntimeError("Falta ANTHROPIC_API_KEY en el .env.")

cliente_anthropic = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

BASE_URL = "https://api.moltjobs.io/v1"
CABECERAS = {"Authorization": f"Bearer {MOLTJOBS_API_KEY}"}

MODELO_LLM = "claude-sonnet-5"

# Responder un item de examen es una tarea simple y acotada (elegir una
# opcion, o una respuesta corta) -- no necesita el esfuerzo "high" que usa
# auditoria.py para un reporte real.
NIVEL_ESFUERZO = "medium"

# Slug real segun la API (no "pack_general_fundamentals" como sugiere la
# documentacion publica) -- gratis, y el que gatea la mayoria de trabajos.
PACK_POR_DEFECTO = "pack_01_general"
MODO_POR_DEFECTO = "CLOSED_BOOK"

# Cada cuantos items se manda un heartbeat para que la sesion no se de
# por abandonada en packs largos (la guia lo recomienda para packs con
# muchos items; no hace falta en packs cortos pero no molesta).
ITEMS_POR_HEARTBEAT = 10

SYSTEM_PROMPT_EVAL = """
You are davlerd, an AI agent taking a timed certification exam on MoltJobs
to prove your capabilities before bidding on paid work. Answer each item
accurately and concisely, in EXACTLY the format requested below -- no
extra commentary, no markdown, no explanation, just the answer value.
""".strip()


def _pedir(metodo, ruta, **kwargs):
    """
    Llamada HTTP generica a la API de MoltJobs. Todas las respuestas
    exitosas vienen envueltas en {"data": ...} -- ver guides/getting-
    started.md del repo de MoltJobs.
    """
    respuesta = requests.request(
        metodo, f"{BASE_URL}{ruta}", headers=CABECERAS, timeout=30, **kwargs
    )
    respuesta.raise_for_status()
    return respuesta.json()["data"]


def _responder_item(item):
    """
    Le pide a Claude que resuelva un item del examen con una sola llamada
    (CLOSED_BOOK: sin tools, sin web). Devuelve (texto_respuesta, ms que
    tardo) para mandar como telemetria junto con la respuesta.
    """
    # NOTA: estos nombres de campo/valores son los REALES, confirmados
    # inspeccionando un item en vivo -- difieren de la guia publica del
    # repo de GitHub (que sugeria "mcq"/"short"/"structured" en minuscula
    # y un campo plano "options"). La primera version de este script uso
    # los nombres de la guia, no matcheo nunca, y cayo siempre en la rama
    # generica sin mostrarle las opciones al modelo -- salio 0/26 en MCQ
    # y JSON por ese bug, no por falta de capacidad real.
    tipo = item.get("type")  # "MCQ" | "SHORT_ANSWER" | "STRUCTURED_TASK"
    prompt_item = item.get("prompt", "")

    if tipo == "MCQ":
        opciones_lista = (item.get("options") or {}).get("choices", [])
        opciones = "\n".join(f"{o['id']}: {o['text']}" for o in opciones_lista)
        instruccion = (
            f"Question:\n{prompt_item}\n\nOptions:\n{opciones}\n\n"
            'Reply with ONLY the option id (e.g. "b").'
        )
    elif tipo == "STRUCTURED_TASK":
        esquema = item.get("outputSchema")
        instruccion = f"Question:\n{prompt_item}\n\nReply with ONLY a valid JSON object"
        if esquema:
            instruccion += f" matching this schema:\n{esquema}"
        instruccion += ". No markdown, no code fences, no explanation -- just the raw JSON."
    else:
        # SHORT_ANSWER y cualquier tipo no reconocido todavia: el prompt
        # ya trae toda la instruccion necesaria (ver ejemplos reales).
        instruccion = f"Question:\n{prompt_item}\n\nReply with ONLY the answer text, nothing else."

    inicio = time.monotonic()
    try:
        respuesta = cliente_anthropic.messages.create(
            model=MODELO_LLM,
            max_tokens=500,
            system=SYSTEM_PROMPT_EVAL,
            messages=[{"role": "user", "content": instruccion}],
            output_config={"effort": NIVEL_ESFUERZO},
        )
    except (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError):
        print("[EVAL] Error de la API de Anthropic al responder un item; se deja en blanco.")
        return "", int((time.monotonic() - inicio) * 1000)

    duracion_ms = int((time.monotonic() - inicio) * 1000)

    if respuesta.stop_reason == "refusal":
        print("[EVAL] Claude rechazo responder este item; se deja en blanco.")
        return "", duracion_ms

    bloque_texto = next((b.text for b in respuesta.content if b.type == "text"), "")
    return bloque_texto.strip(), duracion_ms


def correr_eval(pack_id=PACK_POR_DEFECTO, modo=MODO_POR_DEFECTO):
    """Corre el flujo completo create -> next/answer -> finalize -> report."""
    print(f"[EVAL] Iniciando pack '{pack_id}' en modo {modo}...")
    quiz = _pedir("POST", "/evals", json={"packId": pack_id, "mode": modo})
    quiz_id = quiz["quizId"]
    print(f"[EVAL] Quiz {quiz_id} iniciado. Vence: {quiz.get('expiresAt')}")

    contador = 0
    while True:
        item = _pedir("GET", f"/evals/{quiz_id}/next")
        # La guia publica dice que "data" viene null al agotar el pack,
        # pero la API real devuelve un objeto con "done": true (y sin
        # itemId) -- se chequean las tres formas por las dudas.
        if not item or item.get("done") or not item.get("itemId"):
            break

        contador += 1
        respuesta_texto, duracion_ms = _responder_item(item)
        total_items = item.get("totalItems")
        print(f"[EVAL] Item {contador}/{total_items} ({item.get('itemId')}, {item.get('type')}): respondido en {duracion_ms}ms")

        _pedir(
            "POST", f"/evals/{quiz_id}/items/{item['itemId']}/answer",
            json={"answer": respuesta_texto, "ttcMs": duracion_ms},
        )

        if contador % ITEMS_POR_HEARTBEAT == 0:
            _pedir("POST", f"/evals/{quiz_id}/heartbeat")

    print(f"[EVAL] Pack agotado ({contador} items respondidos). Finalizando...")
    _pedir("POST", f"/evals/{quiz_id}/finalize")

    reporte = _pedir("GET", f"/evals/{quiz_id}/report")
    resultado = "APROBADO" if reporte.get("passed") else "NO APROBADO"
    print(f"[EVAL] Score: {reporte.get('overallScore')} (minimo {reporte.get('passThreshold')}) -- {resultado}")

    certificacion = reporte.get("certification")
    if certificacion:
        print(f"[EVAL] Certificacion obtenida: {certificacion.get('id')} (topic={certificacion.get('topic')})")

    return reporte


def listar_packs():
    """
    Lista los packs de evaluacion disponibles (endpoint publico).

    NOTA: el formato real de /evals/packs difiere del ejemplo que trae la
    documentacion publica del repo (github.com/Moltjobs/docs) -- ahi
    campos son "packId" (no "id", que es un UUID interno), "passThreshold"
    (no "passPct"), "_count.items" (no "itemCount"), y no hay "topic" ni
    "durationMin". Tambien trae "priceUsdc"/"isFree": varios packs no son
    gratis (se cobran en USDC para intentarlos).
    """
    packs = _pedir("GET", "/evals/packs")
    print("Packs disponibles:")
    for p in packs:
        costo = "gratis" if p.get("isFree") else f"{p.get('priceUsdc')} USDC"
        print(
            f"  - {p['packId']}: {p['title']} "
            f"({p['_count']['items']} items, {p.get('passThreshold')}% para aprobar, {costo})"
        )
    return packs


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--listar-packs":
        try:
            listar_packs()
        except requests.exceptions.RequestException:
            print("[EVAL] Error de conexion al listar los packs.")
            sys.exit(1)
        sys.exit(0)

    pack_elegido = sys.argv[1] if len(sys.argv) > 1 else PACK_POR_DEFECTO
    modo_elegido = sys.argv[2] if len(sys.argv) > 2 else MODO_POR_DEFECTO

    try:
        correr_eval(pack_elegido, modo_elegido)
    except requests.exceptions.HTTPError as e:
        print(f"[EVAL] Error HTTP de la API de MoltJobs: {e.response.status_code}")
        try:
            print(f"[EVAL] Detalle: {e.response.json().get('error', {})}")
        except Exception:
            pass
        sys.exit(1)
    except requests.exceptions.RequestException:
        print("[EVAL] Error de conexion con la API de MoltJobs.")
        sys.exit(1)
