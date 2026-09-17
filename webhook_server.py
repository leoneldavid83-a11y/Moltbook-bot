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

from auditoria import elegir_skill_para_tarea, realizar_auditoria
from reporte_pdf import generar_pdf_desde_markdown

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

# Endpoint REST de entrega final (soporta adjuntar archivos, a diferencia
# del callback simple que solo manda texto). Requiere el API key, no la
# firma HMAC -- es una llamada nuestra hacia su API, no un webhook de ellos.
URL_ENTREGA_REST = "https://moltify.ai/api/agent/tasks/{task_id}/deliver"



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
    # No se imprime el titulo/descripcion de la tarea (texto del cliente):
    # este log va a webhook.log, que queda en disco indefinidamente. El
    # taskId alcanza para rastrear el procesamiento sin retener contenido
    # del cliente en un archivo persistente.
    print(f"[WEBHOOK] Tarea nueva: {tarea.get('taskId')}")

    hilo = threading.Thread(target=_procesar_tarea, args=(tarea,), daemon=True)
    hilo.start()

    return jsonify({"received": True}), 200


def _procesar_tarea(tarea):
    """
    Vive en su propio hilo para no bloquear la respuesta rapida que ya se
    le dio al webhook (Moltify exige 200 OK en menos de 30s).

    - Si es una tarea de PRUEBA (Moltify manda "test": true en data para
      el boton "Test Connection"), respondemos de inmediato sin correr
      nada real -- si intentaramos una auditoria de verdad aca, el test
      de Moltify expira a los 15s esperando el callback y nunca llegariamos
      a tiempo (esto paso literalmente la primera vez que probamos).
    - Si es una tarea REAL: primero confirmamos recepcion ("accept", rapido),
      despues corremos la auditoria de verdad (puede tardar varios minutos
      con esfuerzo alto) y recien ahi entregamos el resultado final
      ("deliver").

    POLITICA DE RETENCION DE DATOS: el titulo/descripcion/requisitos que
    manda el cliente (y el reporte generado a partir de eso) nunca se
    escriben a disco ni a una base de datos -- viven solo como variables
    locales de esta funcion mientras dura el procesamiento de ESTA tarea.
    Al terminar (se haya entregado el reporte o haya fallado la auditoria),
    se borran explicitamente de la memoria del proceso con `del` en vez de
    dejar que el recolector de basura de Python las limpie eventualmente
    por su cuenta -- no queda ningun registro del contenido del cliente
    una vez generado y entregado el reporte.
    """
    task_id = tarea.get("taskId")
    callback_url = tarea.get("callbackUrl")

    if tarea.get("test"):
        print(f"[WEBHOOK] Tarea de prueba ({task_id}): respondo sin correr una auditoria real.")
        _enviar_callback(callback_url, "deliver", "Test received -- davlerd's audit pipeline is online and ready.")
        return

    print(f"[WEBHOOK] Confirmando recepcion de la tarea real {task_id}...")
    _enviar_callback(
        callback_url, "accept",
        "Starting the security audit now -- a real OWASP-based review takes a few minutes, not seconds.",
    )

    titulo = tarea.get("title", "")
    descripcion = tarea.get("description", "")
    requerimientos = tarea.get("requirements", "")
    # Ya copiamos lo que necesitamos del dict original de la tarea -- no
    # hace falta seguir sosteniendo una referencia a el.
    del tarea

    reporte = None
    try:
        # Que skill usar no viene explicito en la tarea -- se infiere del
        # titulo/descripcion/requisitos con una clasificacion barata (esfuerzo
        # bajo) antes de correr la auditoria de verdad.
        tipo_skill = elegir_skill_para_tarea(titulo, descripcion, requerimientos)
        print(f"[WEBHOOK] Tarea {task_id} clasificada como: {tipo_skill}")

        reporte = realizar_auditoria(
            tipo_skill,
            contenido_objetivo=requerimientos or descripcion,
            contexto_adicional=descripcion,
        )

        if not reporte:
            print(f"[WEBHOOK] La auditoria de la tarea {task_id} fallo; no se entrega el reporte final.")
            return

        if len(reporte) > MAX_CARACTERES_ENTREGA:
            reporte = reporte[:MAX_CARACTERES_ENTREGA]

        _entregar_resultado_final(task_id, callback_url, reporte)
    finally:
        # Se ejecuta siempre (exito, auditoria fallida, o excepcion): el
        # contenido del cliente no sobrevive mas alla de esta funcion.
        del titulo, descripcion, requerimientos, reporte


def _entregar_resultado_final(task_id, callback_url, reporte):
    """
    Entrega el reporte final con un PDF con formato profesional adjunto
    (via el endpoint REST que soporta archivos), ademas del texto plano.
    Si algo falla generando o mandando el PDF, cae al callback simple de
    solo texto -- el cliente siempre recibe algo, aunque no sea lo ideal.

    No se fuerza el titulo de la tarea de Moltify como asunto de portada:
    reporte_pdf.py lo extrae solo del primer encabezado del propio
    reporte, que suele ser mas descriptivo (ej. "Agent Security Audit:
    ...") que el titulo corto que puso el comprador al pedir la tarea.

    Los bytes del PDF se borran explicitamente al terminar (ver POLITICA
    DE RETENCION DE DATOS en _procesar_tarea) -- no quedan en memoria mas
    tiempo del necesario para mandarlos.
    """
    pdf_bytes = generar_pdf_desde_markdown(reporte)

    try:
        if pdf_bytes and MOLTIFY_API_KEY:
            if _entregar_via_rest_con_pdf(task_id, reporte, pdf_bytes):
                return
            print("[WEBHOOK] La entrega con PDF fallo; caigo al callback de solo texto.")

        _enviar_callback(callback_url, "deliver", reporte)
    finally:
        del pdf_bytes


def _entregar_via_rest_con_pdf(task_id, contenido_texto, pdf_bytes):
    """
    Entrega el resultado via el endpoint REST (multipart/form-data), con
    el reporte en texto Y el PDF adjunto. Devuelve True si salio bien.
    """
    url = URL_ENTREGA_REST.format(task_id=task_id)
    cabeceras = {"Authorization": f"Bearer {MOLTIFY_API_KEY}"}
    datos = {"content": contenido_texto}
    archivos = {"files": ("security-audit-report.pdf", pdf_bytes, "application/pdf")}

    try:
        respuesta = requests.post(url, headers=cabeceras, data=datos, files=archivos, timeout=30)
        respuesta.raise_for_status()
        print("[WEBHOOK] Reporte final entregado con PDF adjunto.")
        return True
    except requests.exceptions.Timeout:
        print("[WEBHOOK] Error: tiempo de espera agotado al entregar con PDF.")
    except requests.exceptions.HTTPError:
        print("[WEBHOOK] Error HTTP al entregar con PDF.")
    except requests.exceptions.RequestException:
        print("[WEBHOOK] Error de conexion al entregar con PDF.")

    return False


def _enviar_callback(callback_url, accion, contenido):
    """Envia un callback firmado a Moltify. accion: 'accept' o 'deliver'."""
    if not callback_url:
        print("[WEBHOOK] La tarea no trajo callbackUrl; no se puede entregar nada.")
        return

    timestamp = str(int(time.time() * 1000))
    cuerpo_json_str = json.dumps({"action": accion, "content": contenido})
    firma = _firmar(timestamp, cuerpo_json_str)

    cabeceras = {
        "Content-Type": "application/json",
        "X-Moltify-Signature": firma,
        "X-Moltify-Timestamp": timestamp,
    }

    try:
        respuesta = requests.post(callback_url, data=cuerpo_json_str, headers=cabeceras, timeout=15)
        respuesta.raise_for_status()
        print(f"[WEBHOOK] Callback '{accion}' entregado correctamente.")
    except requests.exceptions.Timeout:
        print(f"[WEBHOOK] Error: tiempo de espera agotado al enviar el callback '{accion}'.")
    except requests.exceptions.HTTPError:
        print(f"[WEBHOOK] Error HTTP al enviar el callback '{accion}'.")
    except requests.exceptions.RequestException:
        print(f"[WEBHOOK] Error de conexion al enviar el callback '{accion}'.")


@app.route("/health", methods=["GET"])
def salud():
    """
    Endpoint simple de estado. Moltify exige que el agente responda a
    chequeos automaticos cada <4h para seguir visible en el marketplace.
    """
    return jsonify({"status": "ok", "agent": "davlerd"}), 200


if __name__ == "__main__":
    # Waitress en vez de app.run(): el servidor de desarrollo de Flask no
    # esta pensado para manejar trafico real (es de un solo hilo por
    # default). Waitress es liviano (puro Python, sin dependencias
    # pesadas) y le alcanza de sobra a una VM de 1GB.
    #
    # Solo escucha en localhost: el unico que le habla directo es Caddy
    # (reverse proxy con HTTPS), que corre en la misma maquina. Nunca
    # expuesto directo a internet sin el proxy.
    from waitress import serve

    serve(app, host="127.0.0.1", port=8000, threads=4)
