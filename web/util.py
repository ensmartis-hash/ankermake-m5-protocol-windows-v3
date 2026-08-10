import logging as log
import time

from flask import flash, redirect, request

from libflagship.pppp import FileTransfer
from libflagship.ppppapi import FileUploadInfo, PPPPError

import cli.pppp

from web.lib.service import RunState


def flash_redirect(path: str, message: str | None = None, category="info"):
    """
    Flashes a message and redirects the user to the specified path.
    """
    if not path:
        raise ValueError("Redirect path is required")

    if message:
        flash(message, category)

    return redirect(path)


def _stop_service(svc, name):
    if not svc:
        return
    try:
        if svc.state not in (RunState.Stopped,):
            log.info(f"Stopping {name} for exclusive file transfer")
            svc.stop()
            svc.await_stopped()
    except Exception as E:
        log.warning(f"Could not stop {name}: {E}")


def _start_service(svc, name, await_ready=True):
    if not svc:
        return
    try:
        log.info(f"Restarting {name} after file transfer")
        svc.start()
        if await_ready:
            try:
                svc.await_ready()
            except Exception as E:
                log.warning(f"{name} not ready yet after restart: {E}")
    except Exception as E:
        log.warning(f"Could not restart {name}: {E}")


def upload_file_to_printer(app, file):
    """
    Upload a gcode file and start printing.

    Uses the same exclusive PPPP path as `ankerctl.py pppp print-file` (which is
    known to work). Shared video/PPPP services are paused first so the browser
    cannot steal the session mid-upload via /ws/video reconnects.
    """
    user_name = request.headers.get("User-Agent", "ankerctl").split("/")[0] or "ankerctl"
    data = file.read()
    filename = getattr(file, "filename", None) or getattr(file, "name", "upload.gcode")

    if not data:
        raise ConnectionError("Empty file — nothing to upload")

    fui = FileUploadInfo.from_data(
        data, filename, user_name=user_name, user_id="-", machine_id="-"
    )
    log.info(f"Going to upload {fui.size} bytes as {fui.name!r} (exclusive transfer)")

    # Block video websocket from restarting camera during the transfer
    app.config["suspend_video"] = True
    app.config["transfer_in_progress"] = True

    vq = app.svc.svcs.get("videoqueue") if getattr(app, "svc", None) else None
    pppp = app.svc.svcs.get("pppp") if getattr(app, "svc", None) else None
    ft = app.svc.svcs.get("filetransfer") if getattr(app, "svc", None) else None

    api = None
    try:
        # Stop anything that holds the printer PPPP session
        _stop_service(ft, "filetransfer")
        _stop_service(vq, "videoqueue")
        _stop_service(pppp, "pppp")

        # Printer needs a moment to free the previous PPPP session
        time.sleep(1.0)

        config = app.config["config"]
        printer_index = app.config.get("printer_index", 0)

        log.info("Opening exclusive PPPP connection for upload..")
        api = cli.pppp.pppp_open(config, printer_index, timeout=25)
        log.info(f"Exclusive PPPP connected to {api.addr}")

        log.info("Requesting file transfer..")
        cli.pppp.pppp_send_file(api, fui, data)

        log.info("File upload complete. Requesting print start of job.")
        api.aabb_request(b"", frametype=FileTransfer.END)
        log.info("Successfully sent print job")

    except PPPPError as E:
        log.error(f"Could not send print job: {E}")
        raise ConnectionError(f"Printer rejected transfer: {E}") from E
    except ConnectionRefusedError as E:
        log.error(f"PPPP connect failed: {E}")
        raise ConnectionError(
            "Cannot connect to printer for upload. "
            "Cancel any active job on the M5, close eufyMake Studio, then retry."
        ) from E
    except Exception as E:
        log.exception(f"Upload failed: {E}")
        raise
    finally:
        if api is not None:
            try:
                api.stop()
            except Exception:
                pass

        app.config["suspend_video"] = False
        app.config["transfer_in_progress"] = False

        # Bring shared services back (video stays off until browser reconnects)
        _start_service(pppp, "pppp", await_ready=True)
