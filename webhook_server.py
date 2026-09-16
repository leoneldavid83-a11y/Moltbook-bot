#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
webhook_server.py
------------------
Servidor que recibe tareas reales desde Moltify (el marketplace donde se
contrata el servicio de auditoria de seguridad de davlerd) y las procesa
usando el motor en auditoria.py.

Corre SEPARADO de bot.py: bot.py es el ciclo de publicar/responder en
Moltbook cada ~31 min; este es el servidor que atiende trabajo pago,
expuesto publicamente en https://api.davlerd.dev via Caddy (que maneja el
certificado HTTPS automatico) haciendo reverse-proxy a este proceso en
localhost:8000.

Especificacion del webhook (Moltify): POST firmado con HMAC-SHA256 en
X-Moltify-Signature (mensaje = "{timestamp}.{cuerpo_crudo}"), timestamp en
X-Moltify-Timestamp. Hay que responder 200 OK en menos de 30 segundos y
entregar el resultado despues via un callback firmado de la misma forma.

REGLA DE DISENO (la misma que en bot.py y auditoria.py): prohibido
acumular estado que crezca en RAM. Cada tarea se procesa y se olvida; no
hay una lista/diccionario global de tareas en memoria.
"""

import hashlib
import hmac
import json
import os
import threading
import time

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request

from auditoria import realizar_auditoria

load_dotenv()

# Estas dos claves se obtienen recien al aplicar y ser aceptado en Moltify
# (moltify.ai/for/moltbook-agents o /founding-builders). Hasta entonces
# quedan vacias y el servidor rechaza todo por firma invalida -- comportamiento
# seguro por defecto, no un error a corregir apurado.
MOLTIFY_API_KEY = os.getenv("MOLTIFY_API_KEY")
MOLTIFY_WEBHOOK_SECRET = os.getenv("MOLTIFY_WEBHOOK_SECRET")

if not MOLTIFY_WEBHOOK_SECRET:
    print("[WEBHOOK] ADVERTENCIA: falta MOLTIFY_WEBHOOK_SECRET en .env.")
    print("[WEBHOOK] El servidor va a rechazar todas las peticiones hasta que se complete.")

app = Flask(__name__)

# Moltify exige max 50.000 caracteres en el campo "content" al entregar un
# resultado. Con max_tokens=16000 en auditoria.py, un reporte muy largo
# podria superarlo -- se corta con margen antes de entregar.
MAX_CARACTERES_ENTREGA = 49000

# Skill por defecto cuando la tarea no especifica cual usar. La auditoria
# de agentes es la especialidad principal ya establecida del personaje.
SKILL_POR_DEFECTO = "agent-security-audit"


def _verificar_firma(cuerpo_crudo, timestamp, firma_recibida):
    """
    Confirma que una peticion entrante realmente viene de Moltify y no fue
    forjada por un tercero que descubrio la URL del webhook.
    """
    if not MOLTIFY_WEBHOOK_SECRET or not timestamp or not firma_recibida:
        return False

    mensaje = f"{timestamp}.{cuerpo_crudo.decode('utf-8')}"
    firma_esperada = hmac.new(
        MOLTIFY_WEBHOOK_SECRET.encode("utf-8"),
        mensaje.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    # hmac.compare_digest en vez de "==": comparacion en tiempo constante,
    # para no filtrar la firma correcta por un timing attack.
    return hmac.compare_digest(firma_esperada, firma_recibida)


def _firmar(timestamp, cuerpo_json_str):
    """Genera la firma HMAC para un callback que nosotros mandamos a Moltify."""
    mensaje = f"{timestamp}.{cuerpo_json_str}"
    return hmac.new(
        MOLTIFY_WEBHOOK_SECRET.encode("utf-8"),
        mensaje.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@app.route("/webhooks/moltify", methods=["POST"])
def recibir_webhook():
    """
    Punto de entrada real de Moltify. Hay que responder 200 OK en menos de
    30 segundos: el trabajo pesado (la auditoria en si) se dispara en un
    hilo aparte para no bloquear esta respuesta.
    """
    cuerpo_crudo = request.get_data()
    firma = request.headers.get("X-Moltify-Signature")
    timestamp = request.headers.get("X-Moltify-Timestamp")

    if not _verificar_firma(cuerpo_crudo, timestamp, firma):
        print("[WEBHOOK] Firma invalida o ausente -- peticion rechazada.")
        return jsonify({"error": "invalid signature"}), 401

    datos = request.get_json(silent=True) or {}

    if datos.get("event") != "task.submitted":
        print(f"[WEBHOOK] Evento recibido sin manejar todavia: {datos.get('event')}")
        return jsonify({"received": True}), 200

    tarea = datos.get("data", {})
    print(f"[WEBHOOK] Tarea nueva: {tarea.get('taskId')} -- {tarea.get('title')!r}")

    hilo = threading.Thread(target=_procesar_tarea, args=(tarea,), daemon=True)
    hilo.start()

    return jsonify({"received": True}), 200


def _procesar_tarea(tarea):
    """
    Corre la auditoria real (puede tardar varios minutos con esfuerzo
    alto) y entrega el resultado. Vive en su propio hilo para no bloquear
    la respuesta rapida que ya se le dio al webhook.
    """
    task_id = tarea.get("taskId")
    callback_url = tarea.get("callbackUrl")
    descripcion = tarea.get("description", "")
    requerimientos = tarea.get("requirements", "")

    reporte = realizar_auditoria(
        SKILL_POR_DEFECTO,
        contenido_objetivo=requerimientos or descripcion,
        contexto_adicional=descripcion,
    )

    if not reporte:
        print(f"[WEBHOOK] La auditoria de la tarea {task_id} fallo; no se entrega nada.")
        return

    if len(reporte) > MAX_CARACTERES_ENTREGA:
        reporte = reporte[:MAX_CARACTERES_ENTREGA]

    _entregar_resultado(callback_url, reporte)


def _entregar_resultado(callback_url, contenido):
    """Entrega el resultado terminado a Moltify via el callback firmado."""
    if not callback_url:
        print("[WEBHOOK] La tarea no trajo callbackUrl; no se puede entregar nada.")
        return

    timestamp = str(int(time.time() * 1000))
    cuerpo_json_str = json.dumps({"action": "deliver", "content": contenido})
    firma = _firmar(timestamp, cuerpo_json_str)

    cabeceras = {
        "Content-Type": "application/json",
        "X-Moltify-Signature": firma,
        "X-Moltify-Timestamp": timestamp,
    }

    try:
        respuesta = requests.post(callback_url, data=cuerpo_json_str, headers=cabeceras, timeout=15)
        respuesta.raise_for_status()
        print("[WEBHOOK] Resultado entregado correctamente.")
    except requests.exceptions.Timeout:
        print("[WEBHOOK] Error: tiempo de espera agotado al entregar el resultado.")
    except requests.exceptions.HTTPError:
        print("[WEBHOOK] Error HTTP al entregar el resultado.")
    except requests.exceptions.RequestException:
        print("[WEBHOOK] Error de conexion al entregar el resultado.")


@app.route("/health", methods=["GET"])
def salud():
    """
    Endpoint simple de estado. Moltify exige que el agente responda a
    chequeos automaticos cada <4h para seguir visible en el marketplace.
    """
    return jsonify({"status": "ok", "agent": "davlerd"}), 200


if __name__ == "__main__":
    # Solo escucha en localhost: el unico que le habla directo es Caddy
    # (reverse proxy con HTTPS), que corre en la misma maquina. Nunca
    # expuesto directo a internet sin el proxy.
    app.run(host="127.0.0.1", port=8000)
