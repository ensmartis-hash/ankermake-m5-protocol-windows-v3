import logging as log
import time
import uuid
from datetime import datetime, timedelta

from flask import flash, redirect, request

from libflagship.pppp import FileTransfer, Duid, P2PCmdType
from libflagship.ppppapi import FileUploadInfo, PPPPError, AnkerPPPPApi, PPPPState

import cli.pppp
import cli.util

from web.lib.service import RunState


def flash_redirect(path: str, message: str | None = None, category="info"):
    if not path:
        raise ValueError("Redirect path is required")
    if message:
        flash(message, category)
    return redirect(path)


def _force_stop_service(svc, name, timeout=8.0):
    """Stop a service; never block longer than timeout seconds."""
    if not svc:
        return
    try:
        if svc.state == RunState.Stopped and not svc.wanted:
            return
        log.info(f"Stopping {name} for exclusive file transfer (state={svc.state.name})")
        svc.wanted = False
        svc.stop()

        # Force-close underlying PPPP socket if present so the printer frees the session
        if name == "pppp" and hasattr(svc, "_api"):
            try:
                from libflagship.pppp import PktClose
                svc._api.send(PktClose())
            except Exception:
                pass
            try:
                if hasattr(svc._api, "sock"):
                    svc._api.sock.close()
            except Exception:
                pass
            try:
                del svc._api
            except Exception:
                pass
            # Nudge state machine if worker_stop is stuck
            if svc.state != RunState.Stopped:
                svc.state = RunState.Stopped

        deadline = time.time() + timeout
        while time.time() < deadline:
            if svc.state == RunState.Stopped:
                log.info(f"{name} stopped")
                return
            time.sleep(0.1)

        log.warning(f"{name} did not reach Stopped in {timeout}s (state={svc.state}); continuing anyway")
        svc.state = RunState.Stopped
        svc.wanted = False
    except Exception as E:
        log.warning(f"Could not stop {name}: {E}")


def _start_service(svc, name, await_ready=True, timeout=15.0):
    if not svc:
        return
    try:
        log.info(f"Restarting {name} after file transfer")
        svc.start()
        if not await_ready:
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            if svc.state == RunState.Running:
                log.info(f"{name} running again")
                return
            time.sleep(0.2)
        log.warning(f"{name} not ready after {timeout}s (state={svc.state})")
    except Exception as E:
        log.warning(f"Could not restart {name}: {E}")


def _exclusive_pppp_connect(config, printer_index, timeout=20.0):
    """Open a dedicated AnkerPPPPApi session (same approach as CLI print-file)."""
    with config.open() as cfg:
        if not cfg or printer_index >= len(cfg.printers):
            raise ConnectionError("No printer configured")
        printer = cfg.printers[printer_index]
        if not printer.ip_addr:
            # try lan search quickly
            found = dict(list(cli.pppp.pppp_find_printer_ip_addresses()))
            if printer.p2p_duid in found:
                printer.ip_addr = found[printer.p2p_duid]
                log.info(f"Updated printer IP to {printer.ip_addr}")
            else:
                raise ConnectionError("Printer IP unknown and LAN search found nothing")

        duid = Duid.from_string(printer.p2p_duid)
        bind_addr = cli.pppp._pppp_pick_bind_addr(printer.ip_addr)
        api = AnkerPPPPApi.open_lan_broadcast(duid, bind_addr=bind_addr)
        log.info(
            f"Exclusive PPPP connect to {printer.name} "
            f"(expected {printer.ip_addr}, bind {bind_addr})"
        )
        api.connect_lan_search()
        api.start()

        deadline = datetime.now() + timedelta(seconds=timeout)
        while api.state != PPPPState.Connected:
            time.sleep(0.1)
            if api.stopped.is_set() or datetime.now() > deadline:
                try:
                    api.stop()
                except Exception:
                    pass
                raise ConnectionError(
                    "Printer did not accept PPPP connection. "
                    "Cancel any job on the M5, close eufyMake Studio, ensure printer is idle."
                )
        log.info(f"Exclusive PPPP connected at {api.addr}")
        return api


def _exclusive_send_file(api, fui, data):
    """Send gcode using timed channel writes (never hang forever)."""
    log.info("Requesting file transfer..")
    try:
        api.send_xzyh(
            str(uuid.uuid4())[:16].encode(),
            cmd=P2PCmdType.P2P_SEND_FILE,
            timeout=20,
        )
    except TimeoutError:
        log.warning("P2P_SEND_FILE ACK slow; continuing")

    log.info("Sending file metadata..")
    api.send_aabb(bytes(fui), frametype=FileTransfer.BEGIN, timeout=30)
    # Wait for AABB reply on channel 1 with timeout
    _wait_aabb_ok(api, timeout=30)

    log.info("Sending file contents..")
    blocksize = 16 * 1024
    total = len(data)
    for pos, chunk in cli.util.split_chunks(data, blocksize):
        api.send_aabb(chunk, frametype=FileTransfer.DATA, pos=pos, timeout=45)
        _wait_aabb_ok(api, timeout=45)
        if pos == 0 or (pos + len(chunk)) % (256 * 1024) < blocksize or (pos + len(chunk)) >= total:
            done = pos + len(chunk)
            log.info(f"Upload progress: {done}/{total} ({100 * done // max(total, 1)}%)")

    log.info("File upload complete. Requesting print start of job.")
    api.send_aabb(b"", frametype=FileTransfer.END, timeout=30)
    _wait_aabb_ok(api, timeout=30)
    log.info("Successfully sent print job")


def _wait_aabb_ok(api, timeout=30):
    """Read one AABB reply from channel 1 (file-transfer ACK)."""
    from libflagship.pppp import Aabb
    from libflagship.ppppapi import FileTransferReply

    ch = api.chans[1]
    deadline = time.time() + timeout
    while time.time() < deadline:
        # Pump TX retransmits / RX
        try:
            # AnkerPPPPApi runs its own thread; just poll channel buffer
            hdr = ch.peek(12, timeout=0.5)
        except Exception:
            hdr = None
        if not hdr or len(hdr) < 12:
            continue
        try:
            aabb = Aabb.parse(hdr)[0]
        except Exception:
            ch.read(1, timeout=0)
            continue
        total = 12 + aabb.len + 2
        frame = ch.peek(total, timeout=0.5)
        if not frame or len(frame) < total:
            continue
        frame = ch.read(total, timeout=0)
        if not frame:
            continue
        try:
            aabb, payload = Aabb.parse_with_crc(frame)[:2]
        except Exception:
            continue
        if len(payload) == 1:
            res = FileTransferReply(payload[0])
            if res != FileTransferReply.OK:
                raise PPPPError(res, f"Aabb request failed: {res.name}")
            return res
    raise ConnectionError("Timed out waiting for printer file-transfer ACK")


def upload_file_to_printer(app, file):
    """
    Upload gcode and start print using an exclusive PPPP session (CLI-style).

    Stops shared video/PPPP first so the browser cannot steal the session.
    All waits are bounded so Orca never hangs forever.
    """
    user_name = (request.headers.get("User-Agent", "ankerctl") or "ankerctl").split("/")[0]
    data = file.read()
    filename = getattr(file, "filename", None) or getattr(file, "name", "upload.gcode")

    if not data:
        raise ConnectionError("Empty file — nothing to upload")

    fui = FileUploadInfo.from_data(
        data, filename, user_name=user_name, user_id="-", machine_id="-"
    )
    log.info(f"Going to upload {fui.size} bytes as {fui.name!r} (exclusive transfer)")

    app.config["suspend_video"] = True
    app.config["transfer_in_progress"] = True

    vq = app.svc.svcs.get("videoqueue") if getattr(app, "svc", None) else None
    pppp = app.svc.svcs.get("pppp") if getattr(app, "svc", None) else None
    ft = app.svc.svcs.get("filetransfer") if getattr(app, "svc", None) else None

    api = None
    try:
        _force_stop_service(ft, "filetransfer", timeout=3)
        _force_stop_service(vq, "videoqueue", timeout=5)
        _force_stop_service(pppp, "pppp", timeout=8)

        # Let the printer drop the old session
        time.sleep(1.5)

        config = app.config["config"]
        printer_index = app.config.get("printer_index", 0)

        api = _exclusive_pppp_connect(config, printer_index, timeout=20)
        _exclusive_send_file(api, fui, data)

    except PPPPError as E:
        log.error(f"Could not send print job: {E}")
        raise ConnectionError(f"Printer rejected transfer: {E}") from E
    except ConnectionError:
        raise
    except Exception as E:
        log.exception(f"Upload failed: {E}")
        raise ConnectionError(str(E)) from E
    finally:
        if api is not None:
            try:
                api.running = False
            except Exception:
                pass
            try:
                api.stop()
            except Exception:
                pass
            try:
                if hasattr(api, "sock"):
                    api.sock.close()
            except Exception:
                pass

        app.config["suspend_video"] = False
        app.config["transfer_in_progress"] = False

        # Restore shared PPPP (video reconnects itself from the browser)
        _start_service(pppp, "pppp", await_ready=True, timeout=20)
