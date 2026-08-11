import json
import logging as log

from datetime import datetime, timedelta

from ..lib.service import Service, ServiceRestartSignal, ServiceStoppedError
from .. import app

from libflagship.pktdump import PacketWriter
from libflagship.pppp import P2PCmdType, PktClose, Duid, Type, Xzyh, Aabb
from libflagship.ppppapi import AnkerPPPPAsyncApi, PPPPState


class PPPPService(Service):

    def api_command(self, commandType, **kwargs):
        if not hasattr(self, "_api"):
            raise ConnectionError("No pppp connection")
        cmd = {
            "commandType": commandType,
            **kwargs
        }
        return self._api.send_xzyh(
            json.dumps(cmd).encode(),
            cmd=P2PCmdType.P2P_JSON_CMD,
            block=False
        )

    def worker_start(self):
        config = app.config["config"]

        # V3 printers can take a few seconds to answer broadcast discovery
        deadline = datetime.now() + timedelta(seconds=10)

        with config.open() as cfg:
            if not cfg:
                raise ServiceStoppedError("No config available")
            printer = cfg.printers[app.config["printer_index"]]

        if not printer.ip_addr:
            raise ServiceStoppedError("Printer IP address not available")

        # Prefer interface on the same subnet as the printer (Windows needs an explicit bind)
        bind_addr = None
        try:
            import ifaddr
            prefix = ".".join(printer.ip_addr.split(".")[:3]) + "."
            for adapter in ifaddr.get_adapters():
                for ip in adapter.ips:
                    if isinstance(ip.ip, str) and not ip.ip.startswith("127.") and ip.ip.startswith(prefix):
                        bind_addr = ip.ip
                        break
                if bind_addr:
                    break
            if bind_addr is None:
                for adapter in ifaddr.get_adapters():
                    for ip in adapter.ips:
                        if isinstance(ip.ip, str) and not ip.ip.startswith("127."):
                            bind_addr = ip.ip
                            break
                    if bind_addr:
                        break
        except Exception as E:
            log.debug(f"Could not pick bind address: {E}")

        duid = Duid.from_string(printer.p2p_duid)
        # Broadcast discovery: V3 firmware often ignores unicast LanSearch on :32108
        api = AnkerPPPPAsyncApi.open_lan_broadcast(duid, bind_addr=bind_addr)
        if app.config["pppp_dump"]:
            dumpfile = app.config["pppp_dump"]
            log.info(f"Logging all pppp traffic to {dumpfile!r}")
            pktwr = PacketWriter.open(dumpfile)
            api.set_dumper(pktwr)

        log.info(
            f"Trying connect to printer {printer.name} ({printer.p2p_duid}) over pppp "
            f"(broadcast LAN, expected ip {printer.ip_addr}, bind {bind_addr})"
        )

        api.connect_lan_search()
        last_search = datetime.now()

        while api.state != PPPPState.Connected:
            remaining = (deadline - datetime.now()).total_seconds()
            if remaining <= 0:
                raise ConnectionRefusedError("Connection rejected by device (timeout)")
            try:
                msg = api.recv(timeout=min(remaining, 0.5))
                api.process(msg)
            except TimeoutError:
                # Re-broadcast discovery every 2s until we get a PunchPkt/P2P_RDY
                if api.state == PPPPState.Connecting and (datetime.now() - last_search).total_seconds() >= 2:
                    # After first reply, api.addr becomes the printer ephemeral port;
                    # force search back to the broadcast address.
                    api.addr = ("255.255.255.255", 32108)
                    api.connect_lan_search()
                    last_search = datetime.now()
            except StopIteration:
                raise ConnectionRefusedError("Connection rejected by device")

        log.info(
            f"Successfully connected to printer {printer.name} ({printer.p2p_duid}) "
            f"over pppp at {api.addr}"
        )
        log.info("Established pppp connection")
        self._api = api

    def _try_recv_aabb(self, fd, timeout=0.05):
        """Non-blocking-ish AABB read. Returns (aabb, payload) or (None, None)."""
        hdr = fd.peek(12, timeout=timeout)
        if not hdr or len(hdr) < 12:
            return None, None
        try:
            aabb = Aabb.parse(hdr)[0]
        except Exception:
            # Not a valid AABB header — drop 1 byte to resync
            fd.read(1, timeout=0)
            return None, None
        total = 12 + aabb.len + 2
        frame = fd.peek(total, timeout=timeout)
        if not frame or len(frame) < total:
            return None, None
        frame = fd.read(total, timeout=0)
        if not frame:
            return None, None
        try:
            aabb, data = Aabb.parse_with_crc(frame)[:2]
        except Exception as E:
            log.debug(f"{self.name}: bad aabb crc/frame: {E}")
            return None, None
        return aabb, data

    def worker_run(self, timeout):
        try:
            msg = self._api.poll(timeout=timeout)
        except ConnectionResetError:
            # Printer closed the session (normal after a print, or contention).
            # Restart with holdoff — do not tight-loop.
            log.warning(f"{self.name}: printer closed PPPP session; will reconnect")
            raise ServiceRestartSignal()
        except OSError as E:
            # WinError 10038 etc. when socket was closed for exclusive upload — expected
            if not hasattr(self, "_api"):
                return
            log.debug(f"{self.name}: socket error during stop/reconnect: {E}")
            raise ServiceRestartSignal()
        except ConnectionError as E:
            log.warning(f"{self.name}: PPPP connection error: {E}")
            raise ServiceRestartSignal()

        if not msg:
            return

        if msg.type != Type.DRW:
            # forward messages other than Type.DRW without further processing
            self.notify((getattr(msg, "chan", None), msg))
            return

        ch = self._api.chans[msg.chan]

        # Never block forever here: a stuck read freezes poll/retransmit and
        # deadlocks file uploads waiting for DRW ACKs on the request thread.
        with ch.lock:
            # Drain as many complete frames as are currently available
            for _ in range(32):
                data = ch.peek(4, timeout=0)
                if not data or len(data) < 2:
                    return

                if len(data) >= 4 and data[:4] == b'XZYH':
                    hdr = ch.peek(16, timeout=0)
                    if not hdr or len(hdr) < 16:
                        return
                    try:
                        xzyh = Xzyh.parse(hdr)[0]
                    except Exception:
                        ch.read(1, timeout=0)
                        continue
                    frame = ch.read(xzyh.len + 16, timeout=0)
                    if not frame:
                        return
                    xzyh.data = frame[16:]
                    self.notify((msg.chan, xzyh))
                elif data[:2] == b'\xAA\xBB':
                    aabb, payload = self._try_recv_aabb(ch, timeout=0)
                    if aabb is None:
                        # Incomplete AABB — wait for more data; do NOT drop bytes
                        # (dropping corrupts the video XZYH stream on channel 1).
                        return
                    # File-transfer ACKs are 1 byte
                    if len(payload) == 1:
                        aabb.data = payload
                        self.notify((msg.chan, aabb))
                else:
                    # Incomplete prefix of XZYH/AABB — wait for more bytes.
                    # Never discard data here; video frames are easily corrupted.
                    return

    def worker_stop(self):
        if hasattr(self, "_api"):
            try:
                self._api.send(PktClose())
            except Exception as E:
                log.debug(f"{self.name}: close during stop: {E}")
            try:
                del self._api
            except Exception:
                pass

    @property
    def connected(self):
        if not hasattr(self, "_api"):
            return False
        return self._api.state == PPPPState.Connected
