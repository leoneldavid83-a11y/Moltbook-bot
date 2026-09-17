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
from flask import Flask, Response, jsonify, request

import estadisticas
import moltjobs_marketplace
import moltjobs_state
from auditoria import SKILLS_DISPONIBLES, elegir_skill_para_tarea, realizar_auditoria
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

# Secreto de firma del webhook de MoltJobs (distinto del de Moltify).
# MoltJobs no lo expone -- ni al registrar el webhook via API ni en su
# propio dashboard (confirmado a mano, sin boton de reveal/generar).
# Mientras no exista, /webhooks/moltjobs procesa SIN verificar firma
# (ver _verificar_firma_moltjobs y la nota de seguridad en
# recibir_webhook_moltjobs: la autorizacion real no depende de esto).
MOLTJOBS_WEBHOOK_SECRET = os.getenv("MOLTJOBS_WEBHOOK_SECRET")

if not MOLTJOBS_WEBHOOK_SECRET:
    print("[WEBHOOK] ADVERTENCIA: falta MOLTJOBS_WEBHOOK_SECRET en .env.")
    print("[WEBHOOK] /webhooks/moltjobs va a procesar sin verificar firma hasta que se complete.")

# El "id" (handle) con el que se registro el agente en MoltJobs -- se usa
# para confirmar que un evento job.updated es sobre UN TRABAJO NUESTRO
# antes de actuar.
MOLTJOBS_AGENT_ID = "davlerd"

# Credenciales del dashboard privado de estadisticas (/dashboard). Si
# DASHBOARD_PASSWORD no esta configurada, el endpoint rechaza todo --
# mismo criterio de "seguro por defecto" que MOLTIFY_WEBHOOK_SECRET.
DASHBOARD_USUARIO = os.getenv("DASHBOARD_USER", "davlerd")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD")

if not DASHBOARD_PASSWORD:
    print("[WEBHOOK] ADVERTENCIA: falta DASHBOARD_PASSWORD en .env.")
    print("[WEBHOOK] /dashboard va a rechazar todas las peticiones hasta que se complete.")

app = Flask(__name__)
estadisticas.inicializar_base_datos()
moltjobs_state.inicializar_base_datos()

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
        estadisticas.registrar_tarea(task_id, None, "prueba")
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
    tipo_skill = None
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
            estadisticas.registrar_tarea(task_id, tipo_skill, "fallida")
            return

        if len(reporte) > MAX_CARACTERES_ENTREGA:
            reporte = reporte[:MAX_CARACTERES_ENTREGA]

        _entregar_resultado_final(task_id, callback_url, reporte)
        estadisticas.registrar_tarea(task_id, tipo_skill, "entregada")
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


def _verificar_firma_moltjobs(cuerpo_crudo, header_valor):
    """
    Verifica la firma del header "MoltJobs-Signature: t=<ts>,v1=<hmac>"
    (formato segun guides/webhooks.md del repo de MoltJobs -- no
    confirmado contra una entrega real todavia, ver comentario en la
    ruta de abajo). HMAC-SHA256 sobre "{timestamp}.{cuerpo_crudo}",
    mas rechazo de firmas de mas de 5 minutos (proteccion anti-replay).

    Si no hay MOLTJOBS_WEBHOOK_SECRET configurada, devuelve None (no
    True/False) -- MoltJobs no expone ningun secreto para este webhook
    todavia (ni en su dashboard ni en la respuesta de la API al
    registrarlo, confirmado a mano), asi que no hay nada contra que
    verificar. El llamador trata None como "sin firma, pero se procesa
    igual" en vez de rechazar de plano -- ver nota de seguridad en
    recibir_webhook_moltjobs().
    """
    if not MOLTJOBS_WEBHOOK_SECRET:
        return None
    if not header_valor:
        return False
    try:
        partes = dict(kv.split("=", 1) for kv in header_valor.split(","))
        timestamp = partes["t"]
        firma_recibida = partes["v1"]
    except (KeyError, ValueError):
        return False

    mensaje = f"{timestamp}.{cuerpo_crudo.decode('utf-8')}"
    firma_esperada = hmac.new(
        MOLTJOBS_WEBHOOK_SECRET.encode("utf-8"), mensaje.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(firma_esperada, firma_recibida):
        return False

    try:
        return abs(time.time() - int(timestamp)) <= 300
    except ValueError:
        return False


@app.route("/webhooks/moltjobs", methods=["POST"])
def recibir_webhook_moltjobs():
    """
    Punto de entrada de MoltJobs. A diferencia de Moltify (un evento
    "task.submitted" por tarea), MoltJobs manda solo dos tipos de evento
    a este webhook segun su propio dashboard: "job.updated" (cualquier
    cambio de estado de un trabajo, incluida la asignacion) y
    "message.created" (mensaje de chat directo). No hay un tipo de
    evento para "trabajo nuevo publicado" -- el descubrimiento de
    trabajos nuevos lo hace moltjobs_discover.py por polling periodico
    (via systemd timer), no este webhook.

    Se loguean los headers si la firma falla, para poder diagnosticar
    si MOLTJOBS_WEBHOOK_SECRET esta mal o si el formato real de la firma
    resulta distinto al documentado (ya paso con otras partes de esta
    API -- ver moltjobs_cert.py y moltjobs_marketplace.py).

    NOTA DE SEGURIDAD (sin firma configurable todavia, ver
    _verificar_firma_moltjobs): la autorizacion real no depende de esta
    firma -- _procesar_evento_moltjobs() vuelve a pedir el detalle del
    trabajo a la API AUTENTICADA de MoltJobs (con nuestro propio API key)
    y solo actua si esa fuente de verdad confirma que el trabajo es
    nuestro (agentId) y esta realmente ASSIGNED. Un tercero que descubra
    esta URL y mande eventos falsos como mucho nos hace pedir de mas
    datos publicos/propios por API -- no puede hacer que ejecutemos o
    entreguemos nada que no sea legitimamente nuestro.
    """
    cuerpo_crudo = request.get_data()
    firma_header = request.headers.get("MoltJobs-Signature")
    firma_valida = _verificar_firma_moltjobs(cuerpo_crudo, firma_header)

    if firma_valida is False:
        print("[MOLTJOBS] Firma invalida -- peticion rechazada. Headers recibidos:")
        for nombre, valor in request.headers.items():
            if nombre.lower() not in ("authorization", "cookie"):
                print(f"  {nombre}: {valor}")
        return jsonify({"error": "invalid signature"}), 401

    if firma_valida is None:
        print("[MOLTJOBS] ADVERTENCIA: sin MOLTJOBS_WEBHOOK_SECRET configurada -- se procesa sin verificar firma (ver nota de seguridad).")

    evento = request.get_json(silent=True) or {}
    print(f"[MOLTJOBS] Evento recibido: {evento.get('type')} ({evento.get('id')})")

    hilo = threading.Thread(target=_procesar_evento_moltjobs, args=(evento,), daemon=True)
    hilo.start()

    return jsonify({"received": True}), 200


def _procesar_evento_moltjobs(evento):
    """
    Vive en su propio hilo, igual que _procesar_tarea() para Moltify.

    Que nombres de evento manda realmente MoltJobs es un blanco movil:
    la guia publica documenta 8 tipos granulares (job.assigned,
    job.paid, etc.), una vista del dashboard mostro solo "job.updated" +
    "message.created", y otra vista distinta mostro "job.assigned" /
    "job.rejected" / "job.approved" -- tres listas distintas en tres
    lugares distintos. En vez de apostar a un nombre exacto, se reacciona
    a CUALQUIER evento cuyo type empiece con "job." pidiendo el detalle
    real del trabajo por API y decidiendo la accion segun su status
    actual (fuente de verdad), no segun el nombre del evento.

    - "message.created": solo se loguea por ahora (sin auto-respuesta de
      chat en esta fase).
    - Cualquier "job.*" para un trabajo con status ASSIGNED y agentId
      nuestro: dispara la ejecucion completa (marcar iniciado ->
      auditoria real -> entregar). Cualquier otro status (SUBMITTED,
      APPROVED, PAID, REJECTED, etc.) no requiere accion nuestra -- ya la
      disparamos nosotros mismos al entregar, o nunca nos asignaron el
      trabajo.

    POLITICA DE RETENCION DE DATOS: igual que _procesar_tarea() para
    Moltify -- el titulo/descripcion del trabajo y el reporte generado
    se borran de memoria (`del`, en el finally) apenas se entrega o
    falla, nunca se escriben a disco.
    """
    tipo = evento.get("type") or ""
    datos = evento.get("data") or {}

    if tipo == "message.created":
        print(f"[MOLTJOBS] Mensaje de chat recibido (sin auto-respuesta todavia): {datos.get('id')}")
        return

    if not tipo.startswith("job."):
        print(f"[MOLTJOBS] Evento sin manejar todavia: {tipo!r}")
        return

    job_id = datos.get("jobId") or datos.get("id")
    if not job_id:
        print(f"[MOLTJOBS] Evento {tipo!r} sin jobId; se ignora.")
        return

    # Se vuelve a pedir el detalle completo del trabajo en vez de confiar
    # en el contenido del payload del webhook (cuyo formato exacto no
    # esta documentado de forma confiable) -- la API es la fuente de
    # verdad.
    try:
        trabajo = moltjobs_marketplace.obtener_trabajo(job_id)
    except Exception:
        print(f"[MOLTJOBS] Error al obtener el detalle del trabajo {job_id}.")
        return

    if trabajo.get("agentId") != MOLTJOBS_AGENT_ID or trabajo.get("status") != "ASSIGNED":
        return

    # Idempotencia: si job.updated llega repetido para la misma
    # asignacion (reintentos del lado de MoltJobs), no reprocesar un
    # trabajo que ya se empezo, entrego, o fallo en una corrida anterior.
    if moltjobs_state.obtener_estado(job_id) in ("asignado", "entregado", "error"):
        print(f"[MOLTJOBS] Trabajo {job_id} ya estaba en estado '{moltjobs_state.obtener_estado(job_id)}'; se ignora el evento repetido.")
        return

    titulo = trabajo.get("title", "")
    descripcion = None
    reporte = None
    try:
        descripcion = (trabajo.get("inputData") or {}).get("generalDescription") or titulo
        criterios = trabajo.get("acceptanceCriteria") or []
        criterios_texto = "\n".join(f"- {c.get('description', '')}" for c in criterios if c.get("description"))
        contenido_objetivo = f"{descripcion}\n\nAcceptance criteria:\n{criterios_texto}" if criterios_texto else descripcion

        moltjobs_state.marcar_estado(job_id, "asignado")
        print(f"[MOLTJOBS] Trabajo {job_id} asignado -- iniciando ejecucion.")

        try:
            moltjobs_marketplace.marcar_iniciado(job_id)
        except Exception:
            print(f"[MOLTJOBS] Error al marcar el trabajo {job_id} como iniciado (se continua igual).")

        tipo_skill = elegir_skill_para_tarea(titulo, descripcion, "")
        reporte = realizar_auditoria(tipo_skill, contenido_objetivo=contenido_objetivo, contexto_adicional=titulo)

        if not reporte:
            print(f"[MOLTJOBS] La auditoria del trabajo {job_id} fallo; no se entrega.")
            moltjobs_state.marcar_estado(job_id, "error")
            return

        moltjobs_marketplace.entregar_trabajo(job_id, reporte)
        moltjobs_state.marcar_estado(job_id, "entregado")
        print(f"[MOLTJOBS] Trabajo {job_id} entregado.")
    except Exception:
        print(f"[MOLTJOBS] Error inesperado procesando el trabajo {job_id}.")
        moltjobs_state.marcar_estado(job_id, "error")
    finally:
        del titulo, descripcion, reporte


def _autenticacion_dashboard_valida():
    """
    HTTP Basic Auth para /dashboard. Si DASHBOARD_PASSWORD no esta
    configurada, siempre rechaza -- mismo criterio de "seguro por
    defecto" que _verificar_firma() con MOLTIFY_WEBHOOK_SECRET.
    """
    auth = request.authorization
    if not DASHBOARD_PASSWORD or not auth:
        return False
    usuario_ok = hmac.compare_digest(auth.username or "", DASHBOARD_USUARIO)
    clave_ok = hmac.compare_digest(auth.password or "", DASHBOARD_PASSWORD)
    return usuario_ok and clave_ok


@app.route("/dashboard", methods=["GET"])
def dashboard():
    """
    Pagina privada (HTTP Basic Auth) con conteos de tareas procesadas y
    que servicios se piden mas. Solo lee estadisticas.py -- metadatos
    agregados (tipo de auditoria, estado, fecha), nunca el contenido que
    mando el cliente (ver POLITICA DE RETENCION DE DATOS en
    _procesar_tarea).
    """
    if not _autenticacion_dashboard_valida():
        return Response(
            "Authentication required.", 401,
            {"WWW-Authenticate": 'Basic realm="davlerd dashboard"'},
        )
    resumen = estadisticas.obtener_resumen()
    return Response(_renderizar_dashboard_html(resumen), mimetype="text/html")


def _renderizar_dashboard_html(resumen):
    import html as _html_mod

    nombres_skill = {clave: datos["nombre_publico"] for clave, datos in SKILLS_DISPONIBLES.items()}

    filas_estado = "".join(
        f"<tr><td>{_html_mod.escape(str(fila['estado']))}</td><td>{fila['n']}</td></tr>"
        for fila in resumen["por_estado"]
    ) or "<tr><td colspan='2'>Sin datos todavia.</td></tr>"

    filas_skill = "".join(
        f"<tr><td>{_html_mod.escape(nombres_skill.get(fila['tipo_skill'], fila['tipo_skill']))}</td>"
        f"<td>{fila['n']}</td></tr>"
        for fila in resumen["por_skill"]
    ) or "<tr><td colspan='2'>Sin auditorias entregadas todavia.</td></tr>"

    filas_recientes = "".join(
        f"<tr><td>{_html_mod.escape(str(fila['task_id']))}</td>"
        f"<td>{_html_mod.escape(nombres_skill.get(fila['tipo_skill'], fila['tipo_skill'] or '-'))}</td>"
        f"<td>{_html_mod.escape(str(fila['estado']))}</td>"
        f"<td>{_html_mod.escape(str(fila['procesado_en']))}</td></tr>"
        for fila in resumen["recientes"]
    ) or "<tr><td colspan='4'>Sin tareas todavia.</td></tr>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>davlerd -- Dashboard</title>
<style>
  body {{ background:#0B1B33; color:#E8ECF2; font-family: -apple-system, Segoe UI, Arial, sans-serif; margin:0; padding:32px 16px; }}
  .contenedor {{ max-width: 900px; margin: 0 auto; }}
  h1 {{ font-size: 22px; margin-bottom: 4px; }}
  .subtitulo {{ color:#00C2A8; font-size: 13px; margin-bottom: 28px; }}
  .tarjetas {{ display:flex; gap:16px; flex-wrap:wrap; margin-bottom: 32px; }}
  .tarjeta {{ background:#122344; border:1px solid #1E2F52; border-radius:10px; padding:18px 22px; min-width:140px; }}
  .tarjeta .numero {{ font-size: 28px; font-weight:700; color:#00C2A8; }}
  .tarjeta .etiqueta {{ font-size: 12px; color:#9AA7BD; margin-top:4px; }}
  table {{ width:100%; border-collapse: collapse; margin-bottom: 32px; font-size: 13px; }}
  th {{ text-align:left; color:#9AA7BD; font-weight:600; padding:8px 10px; border-bottom:1px solid #1E2F52; }}
  td {{ padding:8px 10px; border-bottom:1px solid #16233F; }}
  h2 {{ font-size:15px; color:#E8ECF2; margin: 0 0 10px; }}
</style>
</head>
<body>
<div class="contenedor">
  <h1>DAVLERD</h1>
  <div class="subtitulo">Task dashboard -- internal use only</div>

  <div class="tarjetas">
    <div class="tarjeta"><div class="numero">{resumen['total']}</div><div class="etiqueta">Total tasks processed</div></div>
  </div>

  <h2>By status</h2>
  <table>
    <tr><th>Status</th><th>Count</th></tr>
    {filas_estado}
  </table>

  <h2>Most requested services</h2>
  <table>
    <tr><th>Service</th><th>Delivered count</th></tr>
    {filas_skill}
  </table>

  <h2>Recent tasks</h2>
  <table>
    <tr><th>Task ID</th><th>Service</th><th>Status</th><th>Processed at (UTC)</th></tr>
    {filas_recientes}
  </table>
</div>
</body>
</html>"""


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
