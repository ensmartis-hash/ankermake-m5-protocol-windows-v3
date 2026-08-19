"""Detect PC sleep/wake gaps and mark ankerctl services for reconnect.

UDP/PPPP sessions almost always die across Windows sleep. Orca's connection
test still passes (HTTP /api/version) while file upload fails until ankerctl
is restarted. This watcher sets app.config['needs_reconnect'] when wall-clock
jumps forward more than expected (typical of resume-from-sleep).
"""

from __future__ import annotations

import logging as log
import threading
import time


def start_sleep_watch(app, poll_seconds: float = 10.0, gap_seconds: float = 45.0):
    """Background thread: flag needs_reconnect after a sleep/wake time gap."""

    def _run():
        last = time.time()
        log.info(
            "Sleep/wake watcher started "
            f"(poll={poll_seconds}s, gap>{gap_seconds}s marks reconnect)"
        )
        while True:
            time.sleep(poll_seconds)
            now = time.time()
            gap = now - last
            # If we slept longer than poll + margin, Windows likely suspended us
            if gap > gap_seconds:
                app.config["needs_reconnect"] = True
                app.config["last_sleep_gap"] = gap
                log.warning(
                    f"Detected PC sleep/wake gap of {gap:.0f}s; "
                    "PPPP/MQTT will refresh on next Orca/API request"
                )
            last = now

    t = threading.Thread(target=_run, name="ankerctl-sleep-watch", daemon=True)
    t.start()
    return t


def ensure_services_fresh(app, reason: str = "request"):
    """
    If sleep/wake was detected (or PPPP is down), bounce core services.

    Safe to call from /api/version (Orca connection test) and before uploads.
    """
    from web.lib.service import RunState
    from web import util as web_util
    import cli.pppp
    import cli.config

    needs = bool(app.config.get("needs_reconnect"))
    pppp = app.svc.svcs.get("pppp") if getattr(app, "svc", None) else None
    mqtt = app.svc.svcs.get("mqttqueue") if getattr(app, "svc", None) else None

    pppp_bad = pppp is None or pppp.state != RunState.Running
    mqtt_bad = mqtt is None or mqtt.state != RunState.Running

    if not needs and not pppp_bad:
        return False

    gap = app.config.get("last_sleep_gap")
    log.info(
        f"Refreshing services after {reason} "
        f"(needs_reconnect={needs}, pppp_bad={pppp_bad}, mqtt_bad={mqtt_bad}, "
        f"sleep_gap={gap})"
    )

    # Best-effort: refresh printer LAN IP (DHCP may have changed after wake)
    try:
        config = app.config.get("config")
        if config:
            found = dict(list(cli.pppp.pppp_find_printer_ip_addresses()))
            if found:
                updated = cli.config.update_printer_ip_addresses(config, found)
                if updated:
                    log.info(f"Updated printer IP(s) after wake: {', '.join(updated)}")
    except Exception as E:
        log.debug(f"LAN IP refresh after wake failed: {E}")

    # Stop then start core services
    web_util._force_stop_service(app.svc.svcs.get("videoqueue"), "videoqueue", timeout=3)
    web_util._force_stop_service(pppp, "pppp", timeout=6)
    web_util._force_stop_service(mqtt, "mqttqueue", timeout=6)
    time.sleep(1.0)
    web_util._start_service(mqtt, "mqttqueue", await_ready=False, timeout=10)
    web_util._start_service(pppp, "pppp", await_ready=True, timeout=20)

    app.config["needs_reconnect"] = False
    app.config["last_reconnect_at"] = time.time()
    log.info("Service refresh complete")
    return True
