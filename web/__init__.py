"""
This module is designed to implement a Flask web server for video
streaming and handling other functionalities of AnkerMake M5.
It also implements various services, routes and functions including.

Methods:
    - startup(): Registers required services on server start

Routes:
    - /ws/mqtt: Handles receiving and sending messages on the 'mqttqueue' stream service through websocket
    - /ws/pppp-state: Provides the status of the 'pppp' stream service through websocket
    - /ws/video: Handles receiving and sending messages on the 'videoqueue' stream service through websocket
    - /ws/ctrl: Handles controlling of light and video quality through websocket
    - /video: Handles the video streaming/downloading feature in the Flask app
    - /: Renders the html template for the root route, which is the homepage of the Flask app
    - /api/version: Returns the version details of api and server as dictionary
    - /api/ankerctl/config/upload: Handles the uploading of configuration file \
        to Flask server and returns a HTML redirect response
    - /api/ankerctl/server/reload: Reloads the Flask server and returns a HTML redirect response
    - /api/files/local: Handles the uploading of files to Flask server and returns a dictionary containing file details

Functions:
    - webserver(config, host, port, **kwargs): Starts the Flask webserver

Services:
    - util: Houses utility services for use in the web module
    - config: Handles configuration manipulation for ankerctl
"""
import json
import logging as log

from datetime import datetime
from secrets import token_urlsafe as token
from flask import Flask, flash, request, render_template, Response, session, url_for, jsonify
from flask_sock import Sock
from user_agents import parse as user_agent_parse

from libflagship import ROOT_DIR

from web.lib.service import ServiceManager, RunState, ServiceStoppedError

import web.config
import web.platform
import web.util

import cli.util
import cli.config
import cli.countrycodes


app = Flask(__name__, root_path=ROOT_DIR, static_folder="static", template_folder="static")
# secret_key is required for flash() to function
app.secret_key = token(24)
app.config.from_prefixed_env()
app.svc = ServiceManager()

sock = Sock(app)


# autopep8: off
import web.service.pppp
import web.service.video
import web.service.mqtt
import web.service.filetransfer
# autopep8: on


PRINTERS_WITHOUT_CAMERA = ["V8110"]


@sock.route("/ws/mqtt")
def mqtt(sock):
    """
    Handles receiving and sending messages on the 'mqttqueue' stream service through websocket
    """
    if not app.config["login"]:
        return
    for data in app.svc.stream("mqttqueue"):
        log.debug(f"MQTT message: {data}")
        sock.send(json.dumps(data))


@sock.route("/ws/video")
def video(sock):
    """
    Handles receiving and sending messages on the 'videoqueue' stream service through websocket
    """
    if not app.config["login"] or not app.config["video_supported"]:
        return

    import time

    # During file upload we must not start video - it steals the PPPP session
    # and is exactly what broke web/Orca transfers (browser reconnect spam).
    while app.config.get("suspend_video") or app.config.get("transfer_in_progress"):
        try:
            time.sleep(0.5)
            # Keep socket barely alive until transfer finishes, then exit so
            # the client reconnects cleanly afterward.
            if not app.config.get("suspend_video") and not app.config.get("transfer_in_progress"):
                break
        except Exception:
            return
        # Cap wait so we don't hold forever if flags get stuck
        # (client will reconnect via AutoWebSocket)
        # fall through after transfer ends

    if app.config.get("suspend_video") or app.config.get("transfer_in_progress"):
        return

    try:
        for msg in app.svc.stream("videoqueue", timeout=30.0):
            if app.config.get("suspend_video") or app.config.get("transfer_in_progress"):
                break
            try:
                sock.send(msg.data)
            except Exception as E:
                log.debug(f"video websocket send failed: {E}")
                break
    except Exception as E:
        log.debug(f"video websocket ended: {E}")


@sock.route("/ws/pppp-state")
def pppp_state(sock):
    """
    Report PPPP connection status over websocket.

    Important: do NOT use stream(..., timeout=3) here. On many firmwares (incl. V3)
    idle PPPP sessions do not emit packets every second, so a short queue timeout
    would drop the borrow, stop PPPP, and restart in a loop - which freezes the UI
    ("loading please wait") and breaks Orca uploads mid-transfer.
    """
    if not app.config["login"]:
        return

    import time

    try:
        # Hold a borrow for the lifetime of this websocket so PPPP stays up while
        # the UI is open (and while Orca may be uploading via the same server).
        with app.svc.borrow("pppp") as pppp:
            # Wait up to ~20s for the initial connection
            deadline = time.time() + 20
            while time.time() < deadline:
                if pppp.connected and pppp.state == RunState.Running:
                    sock.send(json.dumps({"status": "connected"}))
                    log.info("PPPP connection established")
                    break
                time.sleep(0.25)
            else:
                log.warning("PPPP did not become ready for websocket client")
                try:
                    sock.send(json.dumps({"status": "error", "message": "pppp not connected"}))
                except Exception:
                    pass
                return

            # Keep the socket (and borrow) open until the client disconnects or
            # the underlying PPPP session drops. Soft-reconnect without thrashing.
            while True:
                if not (pppp.connected and pppp.state == RunState.Running):
                    log.warning("PPPP session dropped while UI connected; waiting for auto-restart")
                    try:
                        sock.send(json.dumps({"status": "reconnecting"}))
                    except Exception:
                        return
                    # Service thread restarts itself on CLOSE; wait for it.
                    wait_deadline = time.time() + 30
                    while time.time() < wait_deadline:
                        if pppp.connected and pppp.state == RunState.Running:
                            try:
                                sock.send(json.dumps({"status": "connected"}))
                            except Exception:
                                return
                            log.info("PPPP connection re-established")
                            break
                        time.sleep(0.5)
                    else:
                        log.warning("PPPP failed to recover; closing pppp-state websocket")
                        return
                # Block briefly for optional client messages / disconnect detection.
                # flask-sock has no receive timeout; use a short sleep poll instead.
                time.sleep(1.0)
    except ServiceStoppedError as E:
        log.warning(f"PPPP service unavailable: {E}")
    except Exception as E:
        log.exception(f"pppp-state websocket error: {E}")


@sock.route("/ws/ctrl")
def ctrl(sock):
    """
    Handles controlling of light and video quality through websocket
    """
    if not app.config["login"]:
        return

    # send a response on connect, to let the client know the connection is ready
    sock.send(json.dumps({"ankerctl": 1}))

    while True:
        msg = json.loads(sock.receive())

        if "mqtt" in msg:
            with app.svc.borrow("mqttqueue") as mq:
                mq.client.command(msg["mqtt"])

        if "light" in msg:
            with app.svc.borrow("videoqueue") as vq:
                vq.api_light_state(msg["light"])

        if "quality" in msg:
            with app.svc.borrow("videoqueue") as vq:
                vq.api_video_mode(msg["quality"])


@app.get("/video")
def video_download():
    """
    Handles the video streaming/downloading feature in the Flask app
    """
    def generate():
        if not app.config["login"] or not app.config["video_supported"]:
            return
        # start videoqueue if it is not running
        vq = app.svc.svcs.get("videoqueue")
        if vq and vq.state == RunState.Stopped:
            try:
                vq.start()
                vq.await_ready()
            except ServiceStoppedError:
                log.error("VideoQueueService could not be started")
                return
        for msg in app.svc.stream("videoqueue"):
            yield msg.data

    return Response(generate(), mimetype="video/mp4")


@app.get("/")
def app_root():
    """
    Renders the html template for the root route, which is the homepage of the Flask app
    """
    config = app.config["config"]
    with config.open() as cfg:
        user_agent = user_agent_parse(request.headers.get("User-Agent"))
        user_os = web.platform.os_platform(user_agent.os.family)

        if cfg:
            anker_config = str(web.config.config_show(cfg))
            config_existing_email = cfg.account.email
            printer = cfg.printers[app.config["printer_index"]]
            country = cfg.account.country
            if not printer.ip_addr:
                flash("Printer IP address is not set yet, please complete the setup...",
                      "warning")
        else:
            anker_config = "No printers found, please load your login config..."
            config_existing_email = ""
            printer = None
            country = ""

        if ":" in request.host:
            request_host, request_port = request.host.split(":", 1)
        else:
            request_host = request.host
            request_port = "80"

        return render_template(
            "index.html",
            request_host=request_host,
            request_port=request_port,
            configure=app.config["login"],
            login_file_path=web.platform.login_path(user_os),
            anker_config=anker_config,
            video_supported=app.config["video_supported"],
            config_existing_email=config_existing_email,
            country_codes=json.dumps(cli.countrycodes.country_codes),
            current_country=country,
            printer=printer
        )


@app.get("/api/version")
def app_api_version():
    """
    Returns the version details of api and server as dictionary

    Orca Slicer hits this for "Test connection". After PC sleep, HTTP still
    works but PPPP is often dead — refresh services here so the next upload
    does not require manually restarting the ankerctl batch window.
    """
    try:
        if app.config.get("login") and getattr(app, "svc", None) and app.svc.svcs:
            from web.sleep_watch import ensure_services_fresh
            ensure_services_fresh(app, reason="api/version")
    except Exception as E:
        log.debug(f"ensure_services_fresh on /api/version: {E}")

    return {"api": "0.1", "server": "1.9.0", "text": "OctoPrint 1.9.0"}


@app.post("/api/ankerctl/config/updateip")
def app_api_ankerctl_config_update_ip_addresses():
    """
    Handles the uploading of configuration file to Flask server

    Returns:
        A HTML redirect response
    """
    if request.method != "POST":
        return web.util.flash_redirect(url_for('app_root'),
                                       f"Wrong request method {request.method}", "danger")

    message = None
    category = "info"
    url = url_for("app_root")
    config = app.config["config"]
    found_printers = dict(list(cli.pppp.pppp_find_printer_ip_addresses()))

    if found_printers:
        # update printer IP addresses
        log.debug(f"Checking configured printer IP addresses:")
        updated_printers = cli.config.update_printer_ip_addresses(config, found_printers)

        # determine the message to display to the user
        if updated_printers is not None:
            if updated_printers:
                category = "success"
                message = f"Successfully update IP addresses of printer(s) {', '.join(updated_printers)}"
                url = url_for("app_api_ankerctl_server_internal_reload")
            else:
                message = f"No IP addresses were updated."
        else:
            category = "danger"
            message = f"Internal error."
    else:
        category = "danger"
        message = "No printers responded within timeout. " \
                  "Are you connected to the same network as the printer?"

    return web.util.flash_redirect(url, message, category)


@app.post("/api/ankerctl/config/upload")
def app_api_ankerctl_config_upload():
    """
    Handles the uploading of configuration file to Flask server

    Returns:
        A HTML redirect response
    """
    if request.method != "POST":
        return web.util.flash_redirect(url_for('app_root'))
    if "login_file" not in request.files:
        return web.util.flash_redirect(url_for('app_root'), "No file found", "danger")
    file = request.files["login_file"]

    try:
        web.config.config_import(file, app.config["config"])
        return web.util.flash_redirect(url_for('app_api_ankerctl_server_internal_reload'),
                                       "AnkerMake Config Imported!", "success")
    except web.config.ConfigImportError as err:
        log.exception(f"Config import failed: {err}")
        return web.util.flash_redirect(url_for('app_root'), f"Error: {err}", "danger")
    except Exception as err:
        log.exception(f"Config import failed: {err}")
        return web.util.flash_redirect(url_for('app_root'), f"Unexpected Error occurred: {err}", "danger")


@app.post("/api/ankerctl/config/login")
def app_api_ankerctl_config_login():
    if request.method != "POST":
        flash(f"Invalid request method '{request.method}", "danger")
        return jsonify({"redirect": url_for('app_root')})

    # get form data
    form_data = request.form.to_dict()

    for key in ["login_email", "login_password", "login_country"]:
        if key not in form_data:
            return jsonify({"error": "Error: Missing form entry '{key}'"})

    if not cli.countrycodes.code_to_country(form_data["login_country"]):
        return jsonify({"error": f"Error: Invalid country code '{form_data['login_country']}'"})

    try:
        web.config.config_login(form_data['login_email'], form_data['login_password'],
                                form_data['login_country'],
                                form_data['login_captcha_id'], form_data['login_captcha_text'],
                                app.config["config"])
        flash("AnkerMake Config Imported!", "success")
        return jsonify({"redirect": url_for('app_api_ankerctl_server_reload')})
    except web.config.ConfigImportError as err:
        if err.captcha:
            # we have to solve a capture, display it
            return jsonify({"captcha_id": err.captcha["id"],
                            "captcha_url": err.captcha["img"]})
        # unknown import error
        log.exception(f"Config import failed: {err}")
        flash(f"Error: {err}", "danger")
        return jsonify({"redirect": url_for('app_root')})
    except Exception as err:
        # unknown error
        log.exception(f"Config import failed: {err}")
        flash(f"Unexpected error occurred: {err}", "danger")
        return jsonify({"redirect": url_for('app_root')})


@app.get("/api/ankerctl/server/reload")
def app_api_ankerctl_server_reload():
    """
    Reloads the Flask server

    Returns:
        A HTML redirect response
    """
    # clear any pending flash messages
    if "_flashes" in session:
        session["_flashes"].clear()

    config = app.config["config"]

    with config.open() as cfg:
        if not cfg:
            return web.util.flash_redirect(url_for('app_root'), "No printers found in config", "warning")

    return app_api_ankerctl_server_internal_reload("Ankerctl reloaded successfully")


@app.get("/api/ankerctl/server/intreload")
def app_api_ankerctl_server_internal_reload(success_message: str=None):
    """
    Internal variant for reloading the Flask server.

    This version shall be used as the forwarding target of actions displaying
    flash messages. The current function will not clear and overwrite such
    messages.

    Returns:
        A HTML redirect response
    """
    config = app.config["config"]

    with config.open() as cfg:
        app.config["login"] = bool(cfg)
        app.config["video_supported"] = any([printer.model not in PRINTERS_WITHOUT_CAMERA for printer in cfg.printers])
        if cfg.printers and not app.svc.svcs:
            register_services(app)
            start_persistent_services(app)

    try:
        app.svc.restart_all(await_ready=False)
    except Exception as err:
        log.exception(err)
        return web.util.flash_redirect(url_for('app_root'), f"Ankerctl could not be reloaded: {err}", "danger")

    return web.util.flash_redirect(url_for('app_root'), success_message, "success")


@app.post("/api/ankerctl/file/upload")
def app_api_ankerctl_file_upload():
    if request.method != "POST":
        return web.util.flash_redirect(url_for('app_root'))
    if "gcode_file" not in request.files:
        return web.util.flash_redirect(url_for('app_root'), "No file found", "danger")
    file = request.files["gcode_file"]

    try:
        web.util.upload_file_to_printer(app, file)
        return web.util.flash_redirect(url_for('app_root'),
                                       f"File {file.filename} sent to printer!", "success")
    except ConnectionError as err:
        return web.util.flash_redirect(url_for('app_root'),
                                       "Cannot connect to printer!\n"
                                       "Please verify that printer is online, and on the same network as ankerctl.\n"
                                       f"Exception information: {err}", "danger")
    except Exception as err:
        return web.util.flash_redirect(url_for('app_root'),
                                       f"Unknown error occurred: {err}", "danger")


@app.post("/api/files/local")
def app_api_files_local():
    """
    Handles the uploading of files to Flask server

    Returns:
        A dictionary containing file details
    """
    no_act = not cli.util.parse_http_bool(request.form["print"])

    if no_act:
        cli.util.http_abort(409, "Upload-only not supported by Ankermake M5")

    fd = request.files["file"]

    try:
        web.util.upload_file_to_printer(app, fd)
    except ConnectionError as E:
        log.error(f"Connection error: {E}")
        # This message will be shown in i.e. PrusaSlicer, so attempt to
        # provide a readable explanation.
        cli.util.http_abort(
            503,
            "Cannot connect to printer!\n" \
            "\n" \
            "Please verify that printer is online, and on the same network as ankerctl.\n" \
            "\n" \
            f"Exception information: {E}"
        )

    return {}


@app.get("/api/ankerctl/status")
def app_api_ankerctl_status() -> dict:
    """
    Returns the status of the services

    Returns:
        A dictionary containing the keys 'status', possible_states and 'services'
        status = 'ok' == some service is online, 'error' == no service is online
        services = {svc_name: {online: bool, state: str, state_value: int}}
        possible_states = {state_name: state_value}
        version = {api: str, server: str, text: str}
    """
    def get_svc_status(svc):
        # NOTE: Some services might not update their state on stop, so we can't rely on it to be 100% accurate
        state = svc.state
        if state == RunState.Running:
            return {'online': True, 'state': state.name, 'state_value': state.value}
        return {'online': False, 'state': state.name, 'state_value': state.value}

    svcs_status = {svc_name: get_svc_status(svc) for svc_name, svc in app.svc.svcs.items()}

    # If any service is online, the status is 'ok'
    ok = any([svc['online'] for svc_name, svc in svcs_status.items()])

    return {
        "status": "ok" if ok else "error",
        "services": svcs_status,
        "possible_states": {state.name: state.value for state in RunState},
        "version": app_api_version(),
    }


def register_services(app):
    app.svc.register("pppp", web.service.pppp.PPPPService())
    if app.config["video_supported"]:
        app.svc.register("videoqueue", web.service.video.VideoQueue())
    app.svc.register("mqttqueue", web.service.mqtt.MqttQueue())
    app.svc.register("filetransfer", web.service.filetransfer.FileTransferService())


def start_persistent_services(app):
    """
    Keep core services running for the lifetime of the webserver.

    Without this, the first websocket/stream that times out can call put()
    with refcount 0 and stop PPPP - which is exactly when Orca uploads hang.
    """
    for name in ("pppp", "mqttqueue"):
        if name not in app.svc:
            continue
        try:
            # Permanent reference: never put() these for server lifetime
            app.svc.get(name, ready=False)
            log.info(f"Persistent service acquired: {name}")
        except Exception as E:
            log.warning(f"Could not start persistent service {name}: {E}")


def webserver(config, printer_index, host, port, insecure=False, **kwargs):
    """
    Starts the Flask webserver

    Args:
        - config: A configuration object containing configuration information
        - host: A string containing host address to start the server
        - port: An integer specifying the port number of server
        - **kwargs: A dictionary containing additional configuration information

    Returns:
        - None
    """
    with config.open() as cfg:
        video_supported = False
        if cfg:
            if printer_index < len(cfg.printers):
                video_supported = cfg.printers[printer_index].model not in PRINTERS_WITHOUT_CAMERA
        else:
            if not cfg.printers:
                log.error("No printers found in config")
            else:
                log.critical(f"Printer number {printer_index} out of range, max printer number is {len(cfg.printers)-1} ")
        app.config["config"] = config
        app.config["login"] = bool(cfg)
        app.config["printer_index"] = printer_index
        app.config["video_supported"] = video_supported
        app.config["port"] = port
        app.config["host"] = host
        app.config["insecure"] = insecure
        app.config.update(kwargs)
        if cfg.printers:
            register_services(app)
            start_persistent_services(app)
        app.config.setdefault("needs_reconnect", False)
        app.config.setdefault("last_sleep_gap", None)
        try:
            from web.sleep_watch import start_sleep_watch
            start_sleep_watch(app)
        except Exception as E:
            log.warning(f"Sleep/wake watcher not started: {E}")
        # threaded=True is required so Orca/OctoPrint uploads are not blocked by
        # open browser websocket connections (mqtt/video/pppp-state).
        app.run(host=host, port=port, threaded=True)
