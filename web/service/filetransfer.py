import uuid
import logging as log
from queue import Empty

from multiprocessing import Queue

from ..lib.service import Service, RunState
from .. import app

from libflagship.pppp import P2PCmdType, Aabb, FileTransfer
from libflagship.ppppapi import FileUploadInfo, PPPPError

import cli.util


class FileTransferService(Service):

    def api_aabb(self, api, frametype, msg=b"", pos=0, timeout=60):
        # Prefer timed blocking write so we never hang the HTTP request forever
        api.send_aabb(msg, frametype=frametype, pos=pos, timeout=timeout)

    def api_aabb_request(self, api, frametype, msg=b"", pos=0, timeout=60):
        # Clear any stale ACKs from a previous transfer
        while True:
            try:
                self._tap.get_nowait()
            except Exception:
                break

        self.api_aabb(api, frametype, msg, pos, timeout=timeout)
        try:
            resp = self._tap.get(timeout=timeout)
        except Empty as E:
            raise ConnectionError(
                f"Timed out waiting for printer file-transfer ACK (frametype={frametype})"
            ) from E
        log.debug(f"{self.name}: Aabb response: {resp}")
        return resp

    def _pause_video(self):
        """Video shares PPPP channel 1 with file transfer; pause it during uploads."""
        vq = app.svc.svcs.get("videoqueue")
        if not vq:
            return None
        if vq.state in (RunState.Running, RunState.Starting):
            log.info(f"{self.name}: Pausing video feed for file transfer")
            vq.stop()
            try:
                vq.await_stopped()
            except Exception as E:
                log.warning(f"{self.name}: Video stop wait: {E}")
            return vq
        return None

    def _resume_video(self, vq):
        if not vq:
            return
        try:
            log.info(f"{self.name}: Resuming video feed")
            vq.start()
        except Exception as E:
            log.warning(f"{self.name}: Could not resume video: {E}")

    def send_file(self, fd, user_name):
        try:
            api = self.pppp._api
        except AttributeError:
            raise ConnectionError("No pppp connection to printer")

        if not self.pppp.connected:
            raise ConnectionError("PPPP is not connected to printer")

        data = fd.read()
        filename = getattr(fd, "filename", None) or getattr(fd, "name", "upload.gcode")
        try:
            from cli.gcode_meta import inject_ankermake_print_meta
            data = inject_ankermake_print_meta(data)
        except Exception as E:
            log.debug(f"{self.name}: gcode TIME inject skipped: {E}")
        fui = FileUploadInfo.from_data(data, filename, user_name=user_name, user_id="-", machine_id="-")
        log.info(f"Going to upload {fui.size} bytes as {fui.name!r}")

        video = self._pause_video()
        try:
            # Give PPPP a moment after pausing video / prior session churn
            import time
            time.sleep(0.5)
            if not self.pppp.connected:
                raise ConnectionError(
                    "PPPP dropped before upload (printer busy or reconnecting). "
                    "Cancel any active print on the M5, wait for idle, then retry."
                )

            # Match CLI path: request transfer, then BEGIN/DATA/END with ACKs.
            # Timeout on the initial XZYH write - this is where Orca was hanging
            # when the PPPP worker was stuck parsing video frames.
            log.info("Requesting file transfer..")
            try:
                api.send_xzyh(
                    str(uuid.uuid4())[:16].encode(),
                    cmd=P2PCmdType.P2P_SEND_FILE,
                    timeout=30,
                )
            except TimeoutError:
                # Some firmwares ACK poorly on chan0 control frames; continue anyway
                log.warning(f"{self.name}: P2P_SEND_FILE ACK slow; continuing with metadata")

            log.info("Sending file metadata..")
            # NOTE: bytes(fui) already ends with a NUL; do not append another
            self.api_aabb_request(api, FileTransfer.BEGIN, bytes(fui), timeout=60)

            log.info("Sending file contents..")
            blocksize = 1024 * 16  # smaller chunks = more reliable on V3
            total = len(data)
            sent = 0
            for pos, chunk in cli.util.split_chunks(data, blocksize):
                if not self.pppp.connected:
                    raise ConnectionError("PPPP connection lost during upload")
                self.api_aabb_request(api, FileTransfer.DATA, chunk, pos, timeout=90)
                sent = pos + len(chunk)
                # Progress every ~256KB so large Orca jobs show movement in logs
                if sent == total or (sent % (256 * 1024)) < blocksize:
                    log.info(f"Upload progress: {sent}/{total} bytes ({100 * sent // max(total,1)}%)")

            log.info("File upload complete. Requesting print start of job.")
            self.api_aabb_request(api, FileTransfer.END, b"", timeout=60)
        except PPPPError as E:
            log.error(f"Could not send print job: {E}")
            raise
        except TimeoutError as E:
            log.error(f"File transfer timed out: {E}")
            raise ConnectionError(
                f"{E}. If the M5 is printing or shows an error, cancel/clear it and retry."
            ) from E
        else:
            log.info("Successfully sent print job")
        finally:
            self._resume_video(video)

    def handler(self, data):
        chan, msg = data
        if isinstance(msg, Aabb):
            try:
                self._tap.put_nowait(msg)
            except Exception:
                pass

    def worker_start(self):
        self.pppp = app.svc.get("pppp")
        self._tap = Queue()
        self.pppp.handlers.append(self.handler)

    def worker_run(self, timeout):
        self.idle(timeout=timeout)

    def worker_stop(self):
        try:
            self.pppp.handlers.remove(self.handler)
        except ValueError:
            pass
        try:
            del self._tap
        except Exception:
            pass
        app.svc.put("pppp")
