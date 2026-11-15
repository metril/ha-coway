"""Purifier-facing TLS server, sessions and the DeviceHub.

Integration surface (for relay.py / web.py / mqtt_bridge.py)
-----------------------------------------------------------
DeviceHub(allowed_macs=(), silence_timeout=150.0, snapshot_grace=2.0)
  await hub.start(host, port, ssl_context) / await hub.stop();  hub.port (bound port)
  hub.add_state_callback(cb(mac, state))        state = device_state() dict, after every report
  hub.add_online_callback(cb(mac, online, fw))  connected/disconnected; re-fired with fw once known
  hub.add_frame_observer(cb(mac, direction, raw, decoded))
        direction "P>A" (purifier to app) or "A>P" (app to purifier); a relay may emit its own
        directions via hub.notify_frame(). decoded is a codec.Frame or None if undecodable.
  hub.device_state(mac) -> {"mac","online","firmware","last_seen",
                            "control": {reg:val}, "sensor": {reg:val}, "snapshot": {reg:val}}
  hub.macs() -> list[str];  hub.session(mac) -> Session | None
  await hub.write(mac, {reg: val}) -> bool      A1011QG (dropped + warning when offline)
  await hub.request_snapshot(mac) -> bool       A1012QG
  await hub.send_body(mac, body) -> bool        arbitrary body (framed with learned HDR/MAC)
Session (one per live purifier connection)
  .mac .hdr .peer .online .firmware .last_seen .relayed (relay sets True while upstream is up: no local acks)
  await .send_body(body) / .send_raw(bytes) (relay forwarding) / .write(pairs) / .close()
Callbacks may be plain functions or coroutine functions. Legacy TLS (1.0, SECLEVEL=0) is confined
to this listener.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import ssl
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from . import codec
from .registers import CODE_CHANNEL

log = logging.getLogger(__name__)
LIVE_WINDOW = 30.0  # a session with a frame this recent is considered live
ACKED = ("A1000", "A1010", "A1020")  # purifier pushes (QM/RM) the cloud acks with SG


CERT_SUBJECT = "/C=KR/ST=Seoul/L=Seoul/O=COWAY/OU=SW Dev Unit/CN=Coway_iTrust"


def _is_sha1_cert(path: Path) -> bool:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    try:
        c = x509.load_pem_x509_certificate(path.read_bytes())
        return isinstance(c.signature_hash_algorithm, hashes.SHA1)
    except Exception:
        return False


def ensure_cert(data_dir: str | os.PathLike) -> tuple[str, str]:
    """Return (cert, key) paths; generate a SHA1-signed self-signed Coway-lookalike cert if needed.

    The purifier's old TLS stack drops the connection on SHA256-signed certs, so SHA1 is
    mandatory; it is produced with the openssl CLI (python-cryptography refuses SHA1 signing).
    """
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    cert, key = d / "coway.pem", d / "coway.key"
    if cert.exists() and key.exists():
        if _is_sha1_cert(cert):
            return str(cert), str(key)
        log.warning("existing cert %s is not SHA1-signed; regenerating", cert)
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(key), "-out", str(cert),
        "-days", "9000", "-nodes", "-sha1", "-subj", CERT_SUBJECT,
    ]  # fmt: skip
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"cannot run openssl to create the SHA1 certificate: {e}") from e
    if r.returncode != 0:
        raise RuntimeError(f"openssl failed to create the SHA1 certificate: {r.stderr.strip()}")
    if not _is_sha1_cert(cert):
        raise RuntimeError("generated certificate is not SHA1-signed (openssl policy forbids it?)")
    os.chmod(key, 0o600)
    return str(cert), str(key)


def build_ssl_context(certfile: str, keyfile: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_ciphers("ALL:@SECLEVEL=0")
    ctx.verify_mode = ssl.CERT_NONE
    ctx.load_cert_chain(certfile, keyfile)
    return ctx


async def _call(cb: Callable, *args) -> None:
    try:
        r = cb(*args)
        if inspect.isawaitable(r):
            await r
    except Exception:
        log.exception("callback failed")


class Session:
    def __init__(self, hub: DeviceHub, writer: asyncio.StreamWriter, hdr: str, mac: str):
        self.hub, self.writer, self.hdr, self.mac = hub, writer, hdr, mac
        self.peer = writer.get_extra_info("peername")
        self.online = True
        self.relayed = False  # upstream cloud is up and acks the purifier itself
        self.last_frame = time.time()
        self._lock = asyncio.Lock()

    @property
    def serial(self) -> str:
        """Unit serial: HDR is "0101" + serial."""
        return self.hdr[4:] if self.hdr.startswith("0101") else self.hdr

    @property
    def peer_ip(self) -> str | None:
        return self.peer[0] if self.peer else None

    @property
    def firmware(self) -> str | None:
        return self.hub._dev(self.mac)["firmware"]

    @property
    def last_seen(self) -> float | None:
        return self.hub._dev(self.mac)["last_seen"]

    async def send_raw(self, raw: bytes, notify: bool = True) -> bool:
        if not self.online or self.writer.is_closing():
            return False
        try:
            async with self._lock:
                self.writer.write(raw)
                await self.writer.drain()
        except (ConnectionError, OSError):
            return False
        if notify:
            await self.hub.notify_frame(self.mac, "A>P", raw)
        return True

    async def send_body(self, body: str) -> bool:
        return await self.send_raw(codec.make_frame(self.hdr, self.mac, body))

    async def write(self, pairs: dict[int, int]) -> bool:
        return await self.send_body(codec.write_body(pairs))

    async def close(self) -> None:
        self.online = False
        self.writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.writer.wait_closed(), 1)


class DeviceHub:
    def __init__(
        self,
        allowed_macs=(),
        silence_timeout: float = 150.0,
        snapshot_grace: float = 2.0,
    ):
        self.allowed = {m.upper() for m in allowed_macs}
        # the purifier pushes a sensor report every ~60 s on its own: 150 s of silence is offline
        self.silence_timeout = silence_timeout
        self.snapshot_grace = snapshot_grace  # relay mode: wait this long for upstream first
        self.relay_mode = False  # set by Relay
        self._sessions: dict[str, Session] = {}
        self._devices: dict[str, dict] = {}
        self._state_cbs: list[Callable] = []
        self._online_cbs: list[Callable] = []
        self._frame_cbs: list[Callable] = []
        self._server: asyncio.Server | None = None
        self._ssl: ssl.SSLContext | None = None
        self._tasks: set[asyncio.Task] = set()
        self.port: int | None = None

    # --- registration -------------------------------------------------------------
    def add_state_callback(self, cb: Callable) -> None:
        self._state_cbs.append(cb)

    def add_online_callback(self, cb: Callable) -> None:
        self._online_cbs.append(cb)

    def add_frame_observer(self, cb: Callable) -> None:
        self._frame_cbs.append(cb)

    # --- queries / commands ------------------------------------------------------
    def _dev(self, mac: str) -> dict:
        return self._devices.setdefault(
            mac,
            {
                "mac": mac,
                "online": False,
                "firmware": None,
                "last_seen": None,
                "control": {},
                "sensor": {},
                "snapshot": {},
            },
        )

    def device_state(self, mac: str) -> dict:
        d = self._dev(mac)
        return {
            **d,
            "control": dict(d["control"]),
            "sensor": dict(d["sensor"]),
            "snapshot": dict(d["snapshot"]),
        }

    def macs(self) -> list[str]:
        return list(self._devices)

    def session(self, mac: str) -> Session | None:
        return self._sessions.get(mac)

    async def send_body(self, mac: str, body: str) -> bool:
        s = self._sessions.get(mac)
        if not s or not s.online:
            log.warning("dropping %s for %s: purifier offline", body[:7], mac)
            return False
        return await s.send_body(body)

    async def write(self, mac: str, pairs: dict[int, int]) -> bool:
        return await self.send_body(mac, codec.write_body(pairs))

    async def request_snapshot(self, mac: str) -> bool:
        return await self.send_body(mac, "A1012QG")

    async def notify_frame(self, mac: str, direction: str, raw: bytes) -> None:
        try:
            decoded = codec.decode_frame(raw)
        except codec.FrameError:
            decoded = None
        for cb in self._frame_cbs:
            await _call(cb, mac, direction, raw, decoded)

    # --- lifecycle ---------------------------------------------------------------
    async def start(self, host: str, port: int, ssl_context: ssl.SSLContext | None) -> None:
        # TLS is negotiated in _handle (not by start_server) so handshake failures are logged
        self._ssl = ssl_context
        self._server = await asyncio.start_server(self._handle, host, port)
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("listening on %s:%s", host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
        for t in list(self._tasks):
            t.cancel()
        sessions = list(self._sessions.values())
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                asyncio.gather(*(s.close() for s in sessions), return_exceptions=True), 2
            )
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=2)
        if self._server:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2)

    # --- connection handling -----------------------------------------------------
    async def _set_online(self, mac: str, online: bool) -> None:
        d = self._dev(mac)
        if d["online"] != online:
            log.info("purifier %s is now %s", mac, "online" if online else "offline")
        d["online"] = online
        for cb in self._online_cbs:
            await _call(cb, mac, online, d["firmware"])

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._tasks.add(task)
        session: Session | None = None
        snap: asyncio.Task | None = None
        buf = b""
        pn = writer.get_extra_info("peername")
        peer_ip = pn[0] if pn else "?"
        reason = "closed by peer"
        try:
            if self._ssl is not None:
                loop = asyncio.get_running_loop()
                proto = writer.transport.get_protocol()
                try:
                    transport = await loop.start_tls(
                        writer.transport,
                        proto,
                        self._ssl,
                        server_side=True,
                        ssl_handshake_timeout=10,
                    )
                except (OSError, ssl.SSLError, TimeoutError) as e:  # SSLError is an OSError
                    log.warning("TLS handshake with %s failed: %s", peer_ip, e or type(e).__name__)
                    return
                writer = asyncio.StreamWriter(transport, proto, reader, loop)
            while True:
                try:
                    chunk = await asyncio.wait_for(reader.read(4096), self.silence_timeout)
                except TimeoutError:
                    reason = f"no data for {self.silence_timeout:g}s"
                    log.warning(
                        "no data for %ss, dropping %s (%s)",
                        self.silence_timeout,
                        session.mac if session else "unidentified peer",
                        peer_ip,
                    )
                    break
                if not chunk:
                    break
                buf += chunk
                raws, buf = codec.split_frames(buf)
                if len(buf) > 65536:
                    reason = "frame buffer overflow"
                    log.warning("frame buffer overflow from %s, dropping", peer_ip)
                    break
                for raw in raws:
                    try:
                        frame = codec.decode_frame(raw)
                    except codec.FrameError as e:
                        log.warning("bad frame from %s: %s (%r)", peer_ip, e, raw[:80])
                        if session:
                            await self.notify_frame(session.mac, "P>A", raw)
                        continue
                    if session is None:
                        mac = frame.mac.upper()
                        if self.allowed and mac not in self.allowed:
                            reason = "MAC not allowed"
                            log.warning(
                                "rejecting MAC %s from %s (not in allowed_macs)", mac, peer_ip
                            )
                            return
                        old = self._sessions.get(mac)
                        new_peer = writer.get_extra_info("peername")
                        new_ip = new_peer[0] if new_peer else None
                        if (
                            old
                            and old.online
                            and old.peer_ip != new_ip
                            and time.time() - old.last_frame < LIVE_WINDOW
                        ):
                            log.warning(
                                "refusing new connection for %s from %s: live session from %s",
                                mac,
                                new_ip,
                                old.peer_ip,
                            )
                            reason = "refused: live session from another peer"
                            return
                        session = Session(self, writer, frame.hdr, mac)
                        self._sessions[mac] = session
                        log.info(
                            "purifier connected: ip=%s mac=%s serial=%s fw=%s",
                            new_ip,
                            mac,
                            session.serial,
                            self._dev(mac)["firmware"] or "unknown",
                        )
                        if old:
                            log.info(
                                "replacing session for %s (old peer %s, new peer %s)",
                                mac,
                                old.peer_ip,
                                new_ip,
                            )
                            await old.close()
                            await self._set_online(mac, True)  # let observers re-attach
                        snap = asyncio.create_task(self._initial_snapshot(session))
                    await self._on_frame(session, raw, frame)
        except (ConnectionError, OSError, ssl.SSLError) as e:
            reason = f"connection error: {e}"
            log.warning("connection error with %s: %s", peer_ip, e)
        finally:
            if snap:
                snap.cancel()
            writer.close()
            if session:
                log.info(
                    "purifier disconnected: ip=%s mac=%s serial=%s reason=%s",
                    peer_ip,
                    session.mac,
                    session.serial,
                    reason,
                )
                session.online = False
                if self._sessions.get(session.mac) is session:
                    del self._sessions[session.mac]
                    await self._set_online(session.mac, False)
            self._tasks.discard(task)

    async def _on_frame(self, s: Session, raw: bytes, frame: codec.Frame) -> None:
        d = self._dev(s.mac)
        d["last_seen"] = s.last_frame = time.time()
        if not d["online"]:
            await self._set_online(s.mac, True)
        await self.notify_frame(s.mac, "P>A", raw)
        if frame.code == "A1000" and frame.verb == "QM":
            d["firmware"] = frame.data.lstrip("V")
            log.info("purifier %s firmware %s", s.mac, d["firmware"])
            await self._set_online(s.mac, True)
            await self._ack(s, frame)
            return
        channel = CODE_CHANNEL.get(frame.code)
        if channel and frame.verb in ("RM", "SM"):
            try:
                regs = frame.regs()
            except codec.FrameError as e:
                log.warning("bad reg data in %s: %s", frame.code, e)
                return
            d[channel].update(regs)
            for cb in self._state_cbs:
                await _call(cb, s.mac, self.device_state(s.mac))
        await self._ack(s, frame)

    async def _ack(self, s: Session, frame: codec.Frame) -> None:
        """Local mode: ack purifier pushes like the cloud does (never while upstream is up)."""
        if frame.code in ACKED and frame.verb in ("QM", "RM") and not s.relayed:
            await s.send_body(codec.ack_body(frame.code))

    async def _initial_snapshot(self, s: Session) -> None:
        """Local mode only: ask for every register once on connect (the cloud does this too)."""
        try:
            if self.relay_mode:  # give upstream a moment: when it is up the cloud owns the session
                for _ in range(int(self.snapshot_grace / 0.02)):
                    if s.relayed:
                        return
                    await asyncio.sleep(0.02)
            if s.online and not s.relayed:
                await s.send_body("A1012QG")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("initial snapshot failed")
