"""Purifier-facing TLS server, sessions and the DeviceHub.

Integration surface (for relay.py / web.py / mqtt_bridge.py)
-----------------------------------------------------------
DeviceHub(allowed_macs=(), poll_interval=10.0, silence_timeout=60.0)
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
  .mac .hdr .peer .online .firmware .last_seen .suppress_polls (relay sets True to stop local polls)
  await .send_body(body) / .send_raw(bytes) (relay forwarding) / .write(pairs) / .close()
Callbacks may be plain functions or coroutine functions. Legacy TLS (1.0, SECLEVEL=0) is confined
to this listener.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import inspect
import logging
import os
import ssl
import time
from collections.abc import Callable
from pathlib import Path

from . import codec
from .registers import CODE_CHANNEL

log = logging.getLogger(__name__)
LIVE_WINDOW = 30.0  # a session with a frame this recent is considered live
POLLS = ("A1000", "A1010", "A1020")


def ensure_cert(data_dir: str | os.PathLike) -> tuple[str, str]:
    """Return (cert, key) paths, generating a self-signed Coway-lookalike cert if missing."""
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    cert, key = d / "coway.pem", d / "coway.key"
    if cert.exists() and key.exists():
        return str(cert), str(key)
    from cryptography import x509
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID as N

    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [
            x509.NameAttribute(N.COUNTRY_NAME, "KR"),
            x509.NameAttribute(N.STATE_OR_PROVINCE_NAME, "Seoul"),
            x509.NameAttribute(N.LOCALITY_NAME, "Seoul"),
            x509.NameAttribute(N.ORGANIZATION_NAME, "COWAY"),
            x509.NameAttribute(N.ORGANIZATIONAL_UNIT_NAME, "SW Dev Unit"),
            x509.NameAttribute(N.COMMON_NAME, "Coway_iTrust"),
        ]
    )
    now = dt.datetime.now(dt.UTC)
    b = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(k.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=9000))
    )
    try:  # original cert is SHA1; some OpenSSL builds refuse SHA1 signing (never verified anyway)
        c = b.sign(k, hashes.SHA1())
    except UnsupportedAlgorithm:
        c = b.sign(k, hashes.SHA256())
    key.write_bytes(
        k.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    os.chmod(key, 0o600)
    cert.write_bytes(c.public_bytes(serialization.Encoding.PEM))
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
        self.suppress_polls = False
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
            await self.writer.wait_closed()


class DeviceHub:
    def __init__(
        self,
        allowed_macs=(),
        poll_interval: float = 10.0,
        silence_timeout: float = 60.0,
    ):
        self.allowed = {m.upper() for m in allowed_macs}
        self.poll_interval, self.silence_timeout = poll_interval, silence_timeout
        self._sessions: dict[str, Session] = {}
        self._devices: dict[str, dict] = {}
        self._state_cbs: list[Callable] = []
        self._online_cbs: list[Callable] = []
        self._frame_cbs: list[Callable] = []
        self._server: asyncio.Server | None = None
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
        self._server = await asyncio.start_server(
            self._handle, host, port, ssl=ssl_context, ssl_handshake_timeout=10
        )
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("listening on %s:%s", host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
        for s in list(self._sessions.values()):
            await s.close()
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2)

    # --- connection handling -----------------------------------------------------
    async def _set_online(self, mac: str, online: bool) -> None:
        d = self._dev(mac)
        d["online"] = online
        for cb in self._online_cbs:
            await _call(cb, mac, online, d["firmware"])

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._tasks.add(task)
        session: Session | None = None
        poller: asyncio.Task | None = None
        buf = b""
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(reader.read(4096), self.silence_timeout)
                except TimeoutError:
                    log.warning(
                        "no data for %ss, dropping %s",
                        self.silence_timeout,
                        session.mac if session else "unidentified peer",
                    )
                    break
                if not chunk:
                    break
                buf += chunk
                raws, buf = codec.split_frames(buf)
                if len(buf) > 65536:
                    break
                for raw in raws:
                    try:
                        frame = codec.decode_frame(raw)
                    except codec.FrameError as e:
                        log.warning("bad frame: %s (%r)", e, raw[:80])
                        if session:
                            await self.notify_frame(session.mac, "P>A", raw)
                        continue
                    if session is None:
                        mac = frame.mac.upper()
                        if self.allowed and mac not in self.allowed:
                            log.warning("rejecting MAC %s (not in allowed_macs)", mac)
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
                            return
                        session = Session(self, writer, frame.hdr, mac)
                        self._sessions[mac] = session
                        if old:
                            log.info(
                                "replacing session for %s (old peer %s, new peer %s)",
                                mac,
                                old.peer_ip,
                                new_ip,
                            )
                            await old.close()
                            await self._set_online(mac, True)  # let observers re-attach
                        poller = asyncio.create_task(self._poll_loop(session))
                    await self._on_frame(session, raw, frame)
        except (ConnectionError, OSError, ssl.SSLError) as e:
            log.info("connection error: %s", e)
        finally:
            if poller:
                poller.cancel()
            writer.close()
            if session:
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
            await self._set_online(s.mac, True)
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

    async def _poll_loop(self, s: Session) -> None:
        try:
            await s.send_body("A1012QG")
            while s.online:
                if not s.suppress_polls:
                    for code in POLLS:
                        await s.send_body(codec.poll_body(code))
                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("poll loop failed")
