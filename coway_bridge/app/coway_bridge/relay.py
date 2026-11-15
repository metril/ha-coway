"""Optional cloud relay: per purifier session, keep a TLS connection upstream to the Coway cloud.

Purifier frames are forwarded up raw, cloud frames down raw (observer directions "P>C" / "C>P").
While upstream is up the local poll loop is suppressed. Locally injected A1011QG/A1012QG commands
are tracked in a FIFO (with cloud-issued ones, in issuance order); only the acks matching a local
command are swallowed. The session is re-resolved from the hub on every send, so a replaced
session is picked up. Purifier frames seen before upstream is up are buffered (bounded).
Upstream host is resolved per connect by the system resolver; nothing is hardcoded.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import math
import ssl
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime

from . import codec

log = logging.getLogger(__name__)
ACK_CODES = ("A1011", "A1012")
BUFFER_MAX = 50


def client_ssl_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1
    ctx.set_ciphers("ALL:@SECLEVEL=0")
    return ctx


def split_host(cloud_host: str, default_port: int = 9090) -> tuple[str, int]:
    host, sep, port = cloud_host.strip().rpartition(":")
    if not sep or not port.isdigit():
        return cloud_host.strip(), default_port
    return host, int(port)


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class Relay:
    def __init__(
        self,
        hub,
        cloud_host: str,
        ssl_context: ssl.SSLContext | None = None,
        backoff_base: float = 1.0,
        backoff_cap: float = 300.0,
        connect_timeout: float = 10.0,
        stable_after: float = 60.0,
    ):
        self.hub = hub
        self.host, self.port = split_host(cloud_host)
        self.ctx = ssl_context or client_ssl_context()
        self.backoff_base, self.backoff_cap = backoff_base, backoff_cap
        self.connect_timeout = connect_timeout
        self.stable_after = stable_after
        self._tasks: dict[str, asyncio.Task] = {}
        self._up: dict[str, asyncio.StreamWriter] = {}
        self._fifo: dict[str, deque] = {}  # pending write/snapshot commands: (origin, code)
        self._buf: dict[str, deque] = {}  # purifier frames received before upstream is up
        self._sess: dict[str, object] = {}
        self.delays: dict[str, float] = {}  # current reconnect delay per mac
        self._ack_cbs: list[Callable] = []
        self._status: dict[str, dict] = {}
        self._cbs: list[Callable] = []
        hub.add_online_callback(self._on_online)
        hub.add_frame_observer(self._on_frame)

    # --- public ------------------------------------------------------------------
    def add_status_callback(self, cb: Callable) -> None:
        """cb(mac, status_dict) on every upstream state change."""
        self._cbs.append(cb)

    def upstream_status(self, mac: str) -> dict:
        return dict(
            self._status.get(mac)
            or {"state": "connecting", "since": None, "retry_in": None, "error": None}
        )

    def add_ack_callback(self, cb: Callable) -> None:
        """cb(mac, code) when the purifier acks a locally issued command (swallowed)."""
        self._ack_cbs.append(cb)

    def pending_acks(self, mac: str) -> int:
        return sum(1 for o, _ in self._fifo.get(mac, ()) if o == "local")

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    # --- hub hooks ---------------------------------------------------------------
    async def _on_online(self, mac: str, online: bool, fw) -> None:
        t = self._tasks.get(mac)
        if online and (t is None or t.done()):
            self._tasks[mac] = asyncio.create_task(self._run(mac))
        elif online and mac in self._up:  # session replaced while upstream is up
            self._attach(mac)
        elif not online and t:
            t.cancel()
            self._tasks.pop(mac, None)
            self._fifo.pop(mac, None)
            self._buf.pop(mac, None)
            self._status.pop(mac, None)

    def _attach(self, mac: str):
        """Current session for mac (re-resolved each time); resets tracking if it changed."""
        s = self.hub.session(mac)
        if s is not self._sess.get(mac):
            self._sess[mac] = s
            self._fifo.pop(mac, None)
        if s is not None and mac in self._up:
            s.suppress_polls = True
        return s

    def _fifo_for(self, mac: str) -> deque:
        return self._fifo.setdefault(mac, deque(maxlen=100))

    async def _on_frame(self, mac: str, direction: str, raw: bytes, decoded) -> None:
        if direction in ("A>P", "C>P"):
            self._attach(mac)
            if decoded and decoded.verb == "QG" and decoded.code in ACK_CODES:
                origin = "local" if direction == "A>P" else "cloud"
                self._fifo_for(mac).append((origin, decoded.code))
            return
        if direction != "P>A":
            return
        self._attach(mac)
        if decoded and decoded.verb == "SM" and decoded.code in ACK_CODES:
            fifo = self._fifo_for(mac)
            for i, (origin, code) in enumerate(fifo):
                if code == decoded.code:
                    del fifo[i]
                    if origin == "local":  # ack for our own command: never forward to the cloud
                        self._fire_ack(mac, decoded.code)
                        return
                    break
        w = self._up.get(mac)
        if w is None:
            self._buf.setdefault(mac, deque(maxlen=BUFFER_MAX)).append(raw)
            return
        try:
            w.write(raw)
            await w.drain()
        except (ConnectionError, OSError):
            return
        await self.hub.notify_frame(mac, "P>C", raw)

    def _fire_ack(self, mac: str, code: str) -> None:
        for cb in self._ack_cbs:
            try:
                r = cb(mac, code)
                if inspect.isawaitable(r):
                    asyncio.ensure_future(r)
            except Exception:
                log.exception("relay ack callback failed")

    # --- upstream connection -----------------------------------------------------
    def _set(self, mac: str, state: str, since=None, retry_in=None, error=None) -> None:
        st = {
            "state": state,
            "since": _iso(since),
            "retry_in": retry_in,
            "error": error,
        }
        prev = self._status.get(mac)
        if prev is None or prev["state"] != state:
            log.info("upstream relay for %s: %s", mac, state)
        self._status[mac] = st
        for cb in self._cbs:
            try:
                r = cb(mac, dict(st))
                if inspect.isawaitable(r):
                    asyncio.ensure_future(r)
            except Exception:
                log.exception("relay status callback failed")

    def _live(self, mac: str) -> bool:
        s = self.hub.session(mac)
        return s is not None and s.online

    async def _run(self, mac: str) -> None:
        delay = self.backoff_base
        while True:
            if not self._live(mac):
                return
            self._set(mac, "connecting")
            err = None
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        self.host, self.port, ssl=self.ctx, server_hostname=self.host
                    ),
                    self.connect_timeout,
                )
            except (OSError, ssl.SSLError, TimeoutError) as e:
                err = str(e) or type(e).__name__
            else:
                t0 = time.monotonic()
                err = await self._pump(mac, reader, writer)
                if time.monotonic() - t0 >= self.stable_after:
                    delay = self.backoff_base  # only a stable upstream resets the backoff
            s = self.hub.session(mac)
            if s is not None:
                s.suppress_polls = False
            self.delays[mac] = delay
            log.warning("upstream for %s down (%s), retry in %.0fs", mac, err, delay)
            remaining = delay
            while remaining > 0:
                self._set(mac, "down", retry_in=math.ceil(remaining), error=err)
                step = min(1.0, remaining)
                await asyncio.sleep(step)
                remaining -= step
                if not self._live(mac):
                    return
            delay = min(delay * 2, self.backoff_cap)

    async def _pump(self, mac, reader, writer) -> str | None:
        # flush frames buffered before upstream was up (sync: no interleaving with new frames)
        buffered = list(self._buf.pop(mac, ()))
        for raw in buffered:
            writer.write(raw)
        self._up[mac] = writer
        self._attach(mac)
        self._set(mac, "up", since=time.time())
        err = None
        buf = b""
        try:
            if buffered:
                await writer.drain()
                for raw in buffered:
                    await self.hub.notify_frame(mac, "P>C", raw)
            while True:
                if not self._live(mac):
                    err = "purifier offline"
                    break
                chunk = await reader.read(4096)
                if not chunk:
                    err = "closed by cloud"
                    break
                buf += chunk
                raws, buf = codec.split_frames(buf)
                for raw in raws:
                    await self.hub.notify_frame(mac, "C>P", raw)
                    s = self._attach(mac)  # re-resolved: the session may have been replaced
                    if s is not None:
                        await s.send_raw(raw, notify=False)
        except (ConnectionError, OSError, ssl.SSLError) as e:
            err = str(e) or type(e).__name__
        finally:
            self._up.pop(mac, None)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        return err
