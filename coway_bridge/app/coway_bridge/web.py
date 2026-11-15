"""Debug web UI (HA ingress): aiohttp app, REST + WebSocket, in-memory frame ring buffer.

All URLs are relative to the ingress root. See docs/ui-spec.md for the API shapes.
`group` is added to `reg` WS messages (control and sensor register numbers overlap).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from collections import deque
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from aiohttp import WSMsgType, web

from . import codec
from . import registers as R
from .config import normalize_mac

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
RING = 2000
ONLINE_WINDOW = 60.0
HEX4 = re.compile(r"^[0-9A-Fa-f]{4}$")
SUPERVISOR_IP = "172.30.32.2"  # HA ingress proxy
DIRS = ("P>A", "A>P", "C>P", "P>C")

CONTROL_NAMES = {
    R.R_POWER: "power",
    R.R_MODE: "mode",
    R.R_SPEED: "speed",
    R.R_LIGHT: "light",
    R.R_TIMER: "timer",
    R.R_SENS: "sensitivity",
}
SENSOR_NAMES = {
    R.S_PM10: "pm10",
    R.S_LUX: "lux",
    R.S_PREFILTER: "prefilter_used",
    R.S_HEPA: "hepa_used",
}


def _ver() -> str:
    try:
        return version("coway-bridge")
    except PackageNotFoundError:
        return "0.1.0"


def iso(ts: float | None = None) -> str | None:
    if ts is None:
        ts = time.time()
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def decode_reg(group: str, reg: int, val: int) -> tuple[str, str, bool]:
    """-> (name, decoded, known)."""
    if group == "control" and reg in CONTROL_NAMES:
        name = CONTROL_NAMES[reg]
        d = R.decode_control({reg: val})
        v = next(iter(d.values()))
        if isinstance(v, bool):
            v = "On" if v else "Off"
        elif name == "timer":
            v = R.TIMER.get(val, f"{val} min")
        return name, str(v), True
    if group == "sensor" and reg in SENSOR_NAMES:
        return SENSOR_NAMES[reg], str(val), True
    return f"reg_{reg:04X}", "unknown", False


def summarize(frame: codec.Frame | None) -> tuple[str, str, dict]:
    if frame is None:
        return "", "undecodable", {}
    cmd = frame.body[:7]
    fields: dict[str, str] = {}
    parts: list[str] = []
    ch = R.CODE_CHANNEL.get(frame.code)
    if frame.verb in ("RM", "SM", "QG") and (ch or frame.code == "A1011"):
        try:
            regs = frame.regs()
        except codec.FrameError:
            regs = {}
        group = "sensor" if ch == "sensor" else "control"
        for r, v in regs.items():
            fields[f"{r:04X}"] = f"{v:04X}"
            n, d, known = decode_reg(group, r, v)
            parts.append(f"{n}={d}" if known else f"{n}={v:04X}")
    elif frame.code == "A1000" and frame.verb == "QM":
        parts.append(f"fw={frame.data.lstrip('V')}")
    elif not parts and frame.verb == "SG":
        parts.append("poll")
    return cmd, " ".join(parts), fields


class WebApp:
    def __init__(
        self,
        hub,
        cfg=None,
        relay=None,
        ack_timeout: float = 10.0,
        ring: int = RING,
        enforce_ingress: bool | None = None,
    ):
        self.hub, self.cfg, self.relay, self.ack_timeout = hub, cfg, relay, ack_timeout
        # under HAOS only the Supervisor ingress proxy may reach the UI
        self.enforce_ingress = (
            bool(os.environ.get("SUPERVISOR_TOKEN")) if enforce_ingress is None else enforce_ingress
        )
        self._serials: dict[str, str] = {}
        self.frames: deque[dict] = deque(maxlen=ring)
        self._raw: dict[int, bytes] = {}
        self._next_id = 1
        self._clients: set[asyncio.Queue] = set()
        self._regs: dict[str, dict[tuple[str, int], dict]] = {}
        self._since: dict[str, float] = {}
        self._waiters: dict[str, list[tuple[str, asyncio.Future]]] = {}
        hub.add_frame_observer(self._on_frame)
        hub.add_state_callback(self._on_state)
        hub.add_online_callback(self._on_online)
        if relay:
            relay.add_status_callback(self._on_upstream)
            relay.add_ack_callback(self._on_local_ack)
        self.app = self._build()

    # --- model -------------------------------------------------------------------
    def _upstream(self, mac: str) -> dict:
        if not self.relay:
            return {"state": "disabled", "since": None, "retry_in": None, "error": None}
        return self.relay.upstream_status(mac)

    def device(self, mac: str) -> dict:
        st = self.hub.device_state(mac)
        regs = sorted(self._regs.get(mac, {}).values(), key=lambda r: (r["group"], r["reg"]))
        ls = st["last_seen"]
        s = self.hub.session(mac)
        if s is not None:
            self._serials[mac] = s.serial
        return {
            "mac": mac,
            "serial": self._serials.get(mac),
            "fw": st["firmware"],
            "online": bool(st["online"]) and ls is not None and time.time() - ls < ONLINE_WINDOW,
            "last_seen": iso(ls) if ls else None,
            "connected_since": iso(self._since[mac]) if mac in self._since else None,
            "upstream": self._upstream(mac),
            "registers": regs,
        }

    def snapshot(self) -> dict:
        return {
            "bridge": {"version": _ver(), "relay_enabled": self.relay is not None},
            "devices": [self.device(m) for m in self.hub.macs()],
        }

    # --- hub hooks ---------------------------------------------------------------
    def _push(self, msg: dict) -> None:
        data = json.dumps(msg)
        for q in list(self._clients):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:  # slow client: drop it
                self._clients.discard(q)
                while not q.empty():
                    q.get_nowait()
                q.put_nowait(None)

    def _on_frame(self, mac: str, direction: str, raw: bytes, decoded) -> None:
        cmd, summary, fields = summarize(decoded)
        f = {
            "id": self._next_id,
            "mac": mac,
            "ts": iso(),
            "dir": direction,
            "raw": raw.decode("latin-1"),
            "cmd": cmd,
            "summary": summary,
            "fields": fields,
        }
        self._next_id += 1
        if len(self.frames) == self.frames.maxlen:
            self._raw.pop(self.frames[0]["id"], None)
        self.frames.append(f)
        self._raw[f["id"]] = raw
        self._push({"t": "frame", "data": f})
        if self.relay is None and direction == "P>A" and decoded and decoded.verb == "SM":
            self._resolve(mac, decoded.code)  # with a relay, only acks of local commands count

    def _on_local_ack(self, mac: str, code: str) -> None:
        self._resolve(mac, code)

    def _resolve(self, mac: str, code: str) -> None:
        for c, fut in list(self._waiters.get(mac, [])):
            if c == code and not fut.done():
                fut.set_result(True)
                break

    def _on_state(self, mac: str, state: dict) -> None:
        now = iso()
        regs = self._regs.setdefault(mac, {})
        groups = {"control": {**state["snapshot"], **state["control"]}, "sensor": state["sensor"]}
        for group, vals in groups.items():
            for reg, val in vals.items():
                raw = f"{val:04X}"
                cur = regs.get((group, reg))
                if cur and cur["raw"] == raw:
                    continue
                name, dec, known = decode_reg(group, reg, val)
                ent = {
                    "reg": f"{reg:04X}",
                    "name": name,
                    "group": group,
                    "raw": raw,
                    "decoded": dec,
                    "known": known,
                    "updated": now,
                }
                regs[(group, reg)] = ent
                self._push({"t": "reg", "data": {"mac": mac, **ent}})
        self._push({"t": "device", "data": self.device(mac)})

    def _on_online(self, mac: str, online: bool, fw) -> None:
        if online and mac not in self._since:
            self._since[mac] = time.time()
        elif not online:
            self._since.pop(mac, None)
        self._push({"t": "device", "data": self.device(mac)})

    def _on_upstream(self, mac: str, status: dict) -> None:
        self._push({"t": "device", "data": self.device(mac)})

    # --- http --------------------------------------------------------------------
    def _build(self) -> web.Application:
        app = web.Application(middlewares=[self._guard])
        app.add_routes(
            [
                web.get("/", self.h_index),
                web.get("/api/state", self.h_state),
                web.get("/api/frames", self.h_frames),
                web.get("/api/capture", self.h_capture),
                web.get("/api/ws", self.h_ws),
                web.post("/api/devices/{mac}/command", self.h_command),
            ]
        )
        return app

    @web.middleware
    async def _guard(self, request: web.Request, handler):
        if self.enforce_ingress and request.remote != SUPERVISOR_IP:
            return web.Response(status=403, text="forbidden")
        return await handler(request)

    async def h_index(self, request: web.Request) -> web.StreamResponse:
        return web.Response(
            body=(STATIC / "index.html").read_bytes(),
            content_type="text/html",
            charset="utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    async def h_state(self, request: web.Request) -> web.Response:
        return web.json_response(self.snapshot())

    def _select(self, request: web.Request) -> list[dict]:
        mac = normalize_mac(request.query.get("mac", ""))
        return [f for f in self.frames if not mac or f["mac"] == mac]

    async def h_frames(self, request: web.Request) -> web.Response:
        try:
            limit = max(1, min(int(request.query.get("limit", RING)), RING))
        except ValueError:
            return web.json_response({"ok": False, "error": "bad limit"}, status=400)
        return web.json_response({"frames": self._select(request)[-limit:]})

    async def h_capture(self, request: web.Request) -> web.Response:
        sel = self._select(request)
        text = "".join(self._raw[f["id"]].hex() + "\n" for f in sel)
        mac = normalize_mac(request.query.get("mac", "")) or "all"
        name = f"coway-capture-{mac}-{datetime.now(UTC):%Y%m%dT%H%M%S}.txt"
        return web.Response(
            text=text,
            content_type="text/plain",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    def close_clients(self) -> None:
        """Wake every WebSocket handler so it closes its socket (called on shutdown)."""
        for q in list(self._clients):
            while True:
                try:
                    q.put_nowait(None)
                    break
                except asyncio.QueueFull:
                    q.get_nowait()

    async def h_ws(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._clients.add(q)
        await ws.send_str(json.dumps({"t": "state", "data": self.snapshot()}))

        async def reader() -> None:
            async for m in ws:
                if m.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break

        rt = asyncio.create_task(reader())
        try:
            while not rt.done():
                g = asyncio.create_task(q.get())
                done, _ = await asyncio.wait({g, rt}, return_when=asyncio.FIRST_COMPLETED)
                if g in done:
                    if g.result() is None:
                        break
                    await ws.send_str(g.result())
                else:
                    g.cancel()
        except (ConnectionError, RuntimeError):
            pass
        finally:
            rt.cancel()
            self._clients.discard(q)
            await ws.close()
        return ws

    def _writes(self, mac: str, body: dict) -> tuple[str, dict[int, int] | None, str | None]:
        """-> (kind, writes, error). kind 'snapshot' => writes None."""
        t = body.get("type")
        v = body.get("value")
        ctl = self.hub.device_state(mac)["control"] if mac in self.hub.macs() else {}
        if t == "snapshot":
            return t, None, None
        if t == "power" and v in ("on", "off"):
            return t, {R.R_POWER: int(v == "on")}, None
        if t == "speed" and isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 3:
            w = {R.R_SPEED: v}
            if not ctl.get(R.R_POWER):
                w = {R.R_POWER: 1, **w}
            return t, w, None
        if t == "light" and v in ("on", "off"):
            return t, {R.R_LIGHT: R.LIGHT_ON if v == "on" else R.LIGHT_OFF}, None
        if t == "raw":
            reg, val = body.get("reg"), body.get("val")
            if (
                isinstance(reg, str)
                and isinstance(val, str)
                and HEX4.match(reg)
                and HEX4.match(val)
            ):
                return t, {int(reg, 16): int(val, 16)}, None
            return t, None, "reg and val must be 4 hex digits"
        return str(t), None, "invalid command"

    async def h_command(self, request: web.Request) -> web.Response:
        mac = normalize_mac(request.match_info["mac"])
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)
        kind, writes, err = self._writes(mac, body)
        if err:
            return web.json_response({"ok": False, "error": err}, status=400)
        s = self.hub.session(mac)
        if s is None or not s.online:
            return web.json_response({"ok": False, "error": "Purifier offline"}, status=409)
        code = "A1012" if kind == "snapshot" else "A1011"
        fut = asyncio.get_running_loop().create_future()
        entry = (code, fut)
        self._waiters.setdefault(mac, []).append(entry)
        try:
            sent = (
                await self.hub.request_snapshot(mac)
                if writes is None
                else await self.hub.write(mac, writes)
            )
            if not sent:
                return web.json_response({"ok": False, "error": "Purifier offline"}, status=409)
            try:
                await asyncio.wait_for(fut, self.ack_timeout)
            except TimeoutError:
                return web.json_response(
                    {"ok": False, "error": f"No ack within {self.ack_timeout:g}s"}, status=504
                )
            return web.json_response({"ok": True, "acked": True, "at": iso()})
        finally:
            with contextlib.suppress(ValueError):
                self._waiters[mac].remove(entry)


async def start_web(webapp: WebApp, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(webapp.app, shutdown_timeout=1.0)
    await runner.setup()
    await web.TCPSite(runner, host, port, shutdown_timeout=1.0).start()
    return runner


def bound_port(runner: web.AppRunner) -> int:
    return runner.addresses[0][1]
