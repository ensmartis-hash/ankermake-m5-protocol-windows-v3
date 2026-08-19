import logging as log
import time
import uuid
from datetime import datetime, timedelta

from flask import flash, redirect, request

from libflagship.pppp import FileTransfer, Duid, P2PCmdType, Aabb
from libflagship.ppppapi import (
    FileUploadInfo,
    PPPPError,
    AnkerPPPPApi,
    PPPPState,
    FileTransferReply,
)

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


def _close_api(api):
    if api is None:
        return
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


# Try fast large blocks first (CLI default is 32KiB). On ACK failure, reconnect
# and step down. Small blocks are a fallback for flaky Wi-Fi — not the default.
UPLOAD_BLOCK_SCHEDULE = (
    # blocksize, max_in_flight, inter_chunk_sleep_s
    (32 * 1024, 16, 0.0),
    (16 * 1024, 12, 0.005),
    (8 * 1024, 8, 0.010),
    (4 * 1024, 6, 0.015),
)


def _exclusive_pppp_connect(config, printer_index, timeout=20.0, max_in_flight=16):
    """Open a dedicated AnkerPPPPApi session (same approach as CLI print-file)."""
    with config.open() as cfg:
        if not cfg or printer_index >= len(cfg.printers):
            raise ConnectionError("No printer configured")
        printer = cfg.printers[printer_index]
        if not printer.ip_addr:
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
                _close_api(api)
                raise ConnectionError(
                    "Printer did not accept PPPP connection. "
                    "Cancel any job on the M5, close eufyMake Studio, ensure printer is idle. "
                    "If the PC just woke from sleep, wait a few seconds and retry "
                    "(ankerctl will auto-refresh PPPP on the next request)."
                )

        for ch in api.chans:
            ch.max_in_flight = max_in_flight
            ch.timeout = timedelta(seconds=0.35)

        log.info(f"Exclusive PPPP connected at {api.addr} (max_in_flight={max_in_flight})")
        return api


def _aabb_request(api, data, frametype, pos=0, timeout=40.0):
    """
    Send one AABB frame and wait for FileTransferReply OK.

    Uses the same shape as CLI aabb_request, but with timeouts so a stalled
    printer cannot hang Orca forever.

    IMPORTANT: never retry a failed frame on the same session - unacked DRW
    indices poison Channel.tx_ack (acked stays at N while need climbs N+5, N+10...).
    """
    api.send_aabb(data, frametype=frametype, pos=pos, timeout=timeout)
    return _wait_aabb_ok(api, timeout=timeout)


def _wait_aabb_ok(api, timeout=40.0):
    """Read one AABB reply from channel 1 (file-transfer ACK)."""
    ch = api.chans[1]
    deadline = time.time() + timeout
    while time.time() < deadline:
        if getattr(api, "stopped", None) is not None and api.stopped.is_set():
            raise ConnectionError(
                "PPPP session stopped mid-upload. Usually the M5 is busy "
                "(already printing), eufyMake stole the link, or Wi-Fi dropped. "
                "Cancel any job on the printer, close eufyMake, wait until idle, retry."
            )
        if api.state != PPPPState.Connected:
            raise ConnectionError(
                "PPPP session dropped mid-upload. Cancel any active print on the M5, "
                "close eufyMake Studio, check Wi-Fi, then retry."
            )

        with ch.lock:
            hdr = ch.peek(12, timeout=0.25)
            if not hdr or len(hdr) < 12:
                continue
            try:
                aabb = Aabb.parse(hdr)[0]
            except Exception:
                # Do not drop bytes - wait for a full valid frame
                continue
            need = 12 + aabb.len + 2
            frame = ch.peek(need, timeout=0.25)
            if not frame or len(frame) < need:
                continue
            frame = ch.read(need, timeout=0)
            if not frame:
                continue
            try:
                aabb, payload = Aabb.parse_with_crc(frame)[:2]
            except Exception as E:
                log.debug(f"AABB CRC/parse error (will wait for next frame): {E}")
                continue
            if len(payload) != 1:
                log.debug(f"Ignoring non-reply AABB len={len(payload)}")
                continue
            res = FileTransferReply(payload[0])
            if res != FileTransferReply.OK:
                raise PPPPError(res, f"Aabb request failed: {res.name}")
            return res

    raise ConnectionError("Timed out waiting for printer file-transfer ACK")


def _exclusive_send_file(api, fui, data, blocksize, pace_s):
    """Send gcode over one clean PPPP session (no mid-session chunk retries)."""
    log.info("Requesting file transfer..")
    try:
        api.send_xzyh(
            str(uuid.uuid4())[:16].encode(),
            cmd=P2PCmdType.P2P_SEND_FILE,
            timeout=25,
        )
    except TimeoutError:
        log.warning("P2P_SEND_FILE ACK slow; continuing with metadata")

    log.info("Sending file metadata..")
    _aabb_request(api, bytes(fui), FileTransfer.BEGIN, pos=0, timeout=40)

    total = len(data)
    log.info(
        f"Sending file contents ({total} bytes, {blocksize}-byte blocks"
        f"{f', pace={pace_s*1000:.0f}ms' if pace_s else ', no pacing'}).."
    )

    for pos, chunk in cli.util.split_chunks(data, blocksize):
        _aabb_request(api, chunk, FileTransfer.DATA, pos=pos, timeout=40)

        done = pos + len(chunk)
        if pos == 0 or done % (256 * 1024) < blocksize or done >= total:
            log.info(f"Upload progress: {done}/{total} ({100 * done // max(total, 1)}%)")

        if pace_s:
            time.sleep(pace_s)

    log.info("File upload complete. Requesting print start of job.")
    _aabb_request(api, b"", FileTransfer.END, pos=0, timeout=40)
    log.info("Successfully sent print job")


def upload_file_to_printer(app, file):
    """
    Upload gcode and start print using an exclusive PPPP session (CLI-style).

    Stops shared video/PPPP first so the browser cannot steal the session.
    Tries large blocks first (32KiB like CLI), then steps down to 16/8/4KiB
    with slower pacing if ACK timeouts occur (often after PC sleep/Wi-Fi blips).
    """
    # Orca hits /api/version first; also refresh here in case upload is direct
    try:
        from web.sleep_watch import ensure_services_fresh
        ensure_services_fresh(app, reason="upload")
    except Exception as E:
        log.debug(f"ensure_services_fresh before upload: {E}")

    user_name = (request.headers.get("User-Agent", "ankerctl") or "ankerctl").split("/")[0]
    data = file.read()
    filename = getattr(file, "filename", None) or getattr(file, "name", "upload.gcode")

    if not data:
        raise ConnectionError("Empty file - nothing to upload")

    # Orca footer has human "estimated printing time"; M5 wants ;TIME:seconds
    # near G28 or the panel/eufyMake show +1000h-style nonsense.
    try:
        from cli.gcode_meta import inject_ankermake_print_meta
        data = inject_ankermake_print_meta(data)
    except Exception as E:
        log.debug(f"gcode TIME/LAYER_COUNT inject skipped: {E}")

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

        # Extra settle time if we just woke from sleep
        settle = 3.0 if app.config.get("last_sleep_gap") else 1.5
        time.sleep(settle)

        config = app.config["config"]
        printer_index = app.config.get("printer_index", 0)

        # Adaptive block size: fast first, slower/safer on failure.
        # Never retry DATA on the same poisoned channel — always new session.
        last_err = None
        for attempt, (blocksize, max_in_flight, pace_s) in enumerate(UPLOAD_BLOCK_SCHEDULE, 1):
            try:
                _close_api(api)
                api = None

                if attempt > 1:
                    log.warning(
                        f"Retrying upload with smaller blocks "
                        f"(attempt {attempt}/{len(UPLOAD_BLOCK_SCHEDULE)}, "
                        f"blocksize={blocksize}, in_flight={max_in_flight}, pace={pace_s}s).."
                    )
                    time.sleep(1.5 * attempt)

                api = _exclusive_pppp_connect(
                    config, printer_index, timeout=20, max_in_flight=max_in_flight
                )
                time.sleep(0.3)
                _exclusive_send_file(api, fui, data, blocksize=blocksize, pace_s=pace_s)
                last_err = None
                break
            except (PPPPError, ConnectionError, TimeoutError, OSError) as E:
                last_err = E
                log.error(
                    f"Upload attempt {attempt}/{len(UPLOAD_BLOCK_SCHEDULE)} "
                    f"({blocksize}B blocks) failed: {E}"
                )
                _close_api(api)
                api = None
                if attempt >= len(UPLOAD_BLOCK_SCHEDULE):
                    raise ConnectionError(str(E)) from E

        if last_err is not None:
            raise ConnectionError(str(last_err)) from last_err

    except ConnectionError:
        raise
    except Exception as E:
        log.exception(f"Upload failed: {E}")
        raise ConnectionError(str(E)) from E
    finally:
        _close_api(api)

        app.config["suspend_video"] = False
        app.config["transfer_in_progress"] = False
        app.config["last_sleep_gap"] = None

        _start_service(pppp, "pppp", await_ready=True, timeout=20)
