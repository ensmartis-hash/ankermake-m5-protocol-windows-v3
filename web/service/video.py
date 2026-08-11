import logging as log
import time

from ..lib.service import Service, ServiceRestartSignal, RunState
from .. import app

from libflagship.pppp import P2PSubCmdType, Xzyh


class VideoQueue(Service):

    def api_start_live(self):
        self.pppp.api_command(P2PSubCmdType.START_LIVE, data={
            "encryptkey": "x",
            "accountId": "y",
        })

    def api_stop_live(self):
        self.pppp.api_command(P2PSubCmdType.CLOSE_LIVE)

    def api_light_state(self, light):
        self.saved_light_state = light
        self.pppp.api_command(P2PSubCmdType.LIGHT_STATE_SWITCH, data={
            "open": light,
        })

    def api_video_mode(self, mode):
        self.saved_video_mode = mode
        self.pppp.api_command(P2PSubCmdType.LIVE_MODE_SET, data={
            "mode": mode
        })

    def _handler(self, data):
        chan, msg = data

        if chan != 1:
            return

        if not isinstance(msg, Xzyh):
            return

        # Only forward payload bytes used by JMuxer
        if msg.data:
            self.notify(msg)
            self._last_frame_at = time.time()

    def worker_init(self):
        self.saved_light_state = None
        self.saved_video_mode = None
        self._last_frame_at = 0
        self._last_restart_live = 0
        self._handler_attached = False

    def _attach_handler(self):
        if not self._handler_attached:
            self.pppp.handlers.append(self._handler)
            self._handler_attached = True

    def _detach_handler(self):
        if self._handler_attached:
            try:
                self.pppp.handlers.remove(self._handler)
            except ValueError:
                pass
            self._handler_attached = False

    def _bind_pppp_and_start_live(self):
        """(Re)bind to current PPPP session and request a live stream."""
        if not hasattr(self.pppp, "_api") or not self.pppp.connected:
            return False

        self._detach_handler()
        self.api_id = id(self.pppp._api)
        self._attach_handler()

        try:
            self.api_start_live()
            if self.saved_light_state is not None:
                self.api_light_state(self.saved_light_state)
            if self.saved_video_mode is not None:
                self.api_video_mode(self.saved_video_mode)
            else:
                # Prefer SD by default - more stable over Wi‑Fi than HD
                self.api_video_mode(0)
            self._last_restart_live = time.time()
            self._last_frame_at = time.time()
            log.info(f"{self.name}: live stream started on pppp session {self.api_id}")
            return True
        except Exception as E:
            log.warning(f"{self.name}: START_LIVE failed: {E}")
            return False

    def worker_start(self):
        if app.config.get("suspend_video") or app.config.get("transfer_in_progress"):
            from web.lib.service import ServiceStoppedError
            raise ServiceStoppedError("video suspended during file transfer")

        self.pppp = app.svc.get("pppp")
        # Wait briefly for PPPP to be up (persistent service may still be connecting)
        deadline = time.time() + 15
        while time.time() < deadline:
            if app.config.get("suspend_video") or app.config.get("transfer_in_progress"):
                from web.lib.service import ServiceStoppedError
                raise ServiceStoppedError("video suspended during file transfer")
            if self.pppp.connected and hasattr(self.pppp, "_api"):
                break
            time.sleep(0.25)

        if not self._bind_pppp_and_start_live():
            # Stay running and retry in worker_run - do not fail start (that
            # would tear down the websocket and flash "loading please wait").
            log.warning(f"{self.name}: PPPP not ready yet; will retry START_LIVE")
            self.api_id = None

    def worker_run(self, timeout):
        self.idle(timeout=timeout)

        if app.config.get("suspend_video") or app.config.get("transfer_in_progress"):
            return

        # If PPPP dropped, do NOT restart this service (that kills /ws/video).
        # Wait for PPPP to come back and re-issue START_LIVE.
        if not hasattr(self.pppp, "_api") or not self.pppp.connected:
            return

        current_id = id(self.pppp._api)
        if self.api_id != current_id:
            log.info(f"{self.name}: new PPPP session detected, rebinding live stream")
            self._bind_pppp_and_start_live()
            return

        # If we haven't received frames for a while, nudge the camera again
        now = time.time()
        if self._last_frame_at and (now - self._last_frame_at) > 8:
            if (now - self._last_restart_live) > 5:
                log.warning(f"{self.name}: no frames for {now - self._last_frame_at:.0f}s; re-START_LIVE")
                try:
                    self.api_start_live()
                    if self.saved_video_mode is not None:
                        self.api_video_mode(self.saved_video_mode)
                    else:
                        self.api_video_mode(0)
                    self._last_restart_live = now
                    self._last_frame_at = now  # avoid tight loop if still quiet
                except Exception as E:
                    log.warning(f"{self.name}: re-START_LIVE failed: {E}")

    def worker_stop(self):
        try:
            self.api_stop_live()
        except Exception as E:
            log.warning(f"{self.name}: Failed to send stop command ({E})")

        self._detach_handler()

        try:
            app.svc.put("pppp")
        except Exception:
            pass
