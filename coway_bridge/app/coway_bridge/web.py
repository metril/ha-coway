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
from .aqi import AqiTracker, aqi_band
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


REG_LABELS = {
    ("control", R.R_POWER): ("Power", "Settings register 0001: power (0 off, 1 on)"),
    ("control", R.R_MODE): ("Mode", "Settings register 0002: mode (1 smart, 2 bedtime, 0 manual)"),
    ("control", R.R_SPEED): ("Fan speed", "Settings register 0003: fan speed 1-3"),
    ("control", R.R_LIGHT): ("Display light", "Settings register 0007: light (0 off, 2 on)"),
    ("control", R.R_TIMER): ("Off timer", "Settings register 0008: timer in minutes (hex)"),
    ("control", R.R_SENS): ("Sensitivity", "Settings register 000A: 1 high, 2 medium, 3 low"),
    ("sensor", R.S_PM10): ("PM10 dust", "Sensor register 0002: PM10 in \u00b5g/m\u00b3"),
    ("sensor", R.S_LUX): ("Light sensor", "Sensor register 0007: ambient light in lux"),
    ("sensor", R.S_PREFILTER): ("Pre-filter used", "Sensor register 0011: % of pre-filter used"),
    ("sensor", R.S_HEPA): ("Max2 filter used", "Sensor register 0012: % of Max2 filter used"),
}
CONFLICT = "\0"  # _writes error prefix: valid command, wrong device state (HTTP 409)
SENS_LABEL = {"insensitive": "Low", "moderate": "Medium", "sensitive": "High"}


def display_reg(group: str, reg: int, val: int) -> str:
    """Human value for the register table (units included)."""
    if group == "control" and reg == R.R_SENS:
        return SENS_LABEL.get(R.SENSITIVITY.get(val, ""), str(val))
    if group == "control" and reg == R.R_MODE:
        return R.MODE_NAMES.get(val, "manual").capitalize()
    if group == "control" and reg == R.R_TIMER:
        return {0: "Off"}.get(val) or R.TIMER.get(val, f"{val} min")
    if group == "control" and reg in CONTROL_NAMES:
        return decode_reg(group, reg, val)[1]
    if group == "sensor" and reg == R.S_PM10:
        return f"{val} \u00b5g/m\u00b3"
    if group == "sensor" and reg == R.S_LUX:
        return f"{val} lx"
    if group == "sensor" and reg in (R.S_PREFILTER, R.S_HEPA):
        return f"{val}%"
    return "?"


def _on_off(v: int) -> str:
    return "on" if v else "off"


def _control_words(regs: dict[int, int]) -> list[str]:
    """Settings registers as short phrases, e.g. ['on', 'manual', 'speed 2']."""
    out: list[str] = []
    unknown = 0
    for r, v in regs.items():
        if r == R.R_POWER:
            out.append(_on_off(v))
        elif r == R.R_MODE:
            out.append(R.MODE_NAMES.get(v, "manual"))
        elif r == R.R_SPEED:
            out.append(f"speed {v}")
        elif r == R.R_LIGHT:
            out.append(f"light {_on_off(v == R.LIGHT_ON)}")
        elif r == R.R_TIMER:
            out.append(f"timer {R.TIMER.get(v, f'{v} min')}")
        elif r == R.R_SENS:
            out.append(f"{SENS_LABEL.get(R.SENSITIVITY.get(v, ''), v)} sensitivity".lower())
        else:
            unknown += 1
    if unknown:
        out.append(f"{unknown} other value{'s' if unknown != 1 else ''}")
    return out


def _command_words(regs: dict[int, int]) -> list[str]:
    """What a write command asks for, e.g. ['set fan speed \u2192 3']."""
    out: list[str] = []
    for r, v in regs.items():
        if r == R.R_POWER:
            out.append(f"turn {_on_off(v)}")
        elif r == R.R_MODE:
            out.append(f"set mode \u2192 {R.MODE_NAMES.get(v, 'manual')}")
        elif r == R.R_SPEED:
            out.append(f"set fan speed \u2192 {v}")
        elif r == R.R_LIGHT:
            out.append(f"set light \u2192 {_on_off(v == R.LIGHT_ON)}")
        elif r == R.R_TIMER:
            out.append(f"set timer \u2192 {R.TIMER.get(v, f'{v} min')}")
        elif r == R.R_SENS:
            out.append(
                f"set sensitivity \u2192 {SENS_LABEL.get(R.SENSITIVITY.get(v, ''), v).lower()}"
            )
        else:
            out.append(f"write register {r:04X} \u2192 {v:04X}")
    return out


def _sensor_words(regs: dict[int, int]) -> list[str]:
    out: list[str] = []
    for r, v in regs.items():
        if r == R.S_PM10:
            out.append(f"PM10 {v} \u00b5g/m\u00b3")
        elif r == R.S_LUX:
            out.append(f"{v} lx")
        elif r == R.S_PREFILTER:
            out.append(f"pre-filter {max(0, 100 - v)}% left")
        elif r == R.S_HEPA:
            out.append(f"Max2 filter {max(0, 100 - v)}% left")
        else:
            out.append(f"sensor {r:04X}={v:04X}")
    return out


def describe(frame: codec.Frame) -> tuple[str, str]:
    """(title, summary): plain-language description of a frame for the message log."""
    code, verb = frame.code, frame.verb
    try:
        regs = frame.regs() if verb in ("RM", "SM", "QG") else {}
    except codec.FrameError:
        regs = {}
    if code == "A1000":
        if verb == "QM":
            return "Hello", f"firmware {frame.data.lstrip('V')}"
        if verb == "SG":
            return "Keep-alive", "still there?"
    if code == "A1010":
        if verb == "SG":
            return "Poll settings", "asked for current settings"
        if verb == "RM":
            return "Settings report", " \u00b7 ".join(_control_words(regs)) or "no changes"
    if code == "A1011":
        if verb == "QG":
            return "Command", ", ".join(_command_words(regs)) or "write"
        if verb == "SM":
            return "Acknowledged", "command accepted"
    if code == "A1012":
        if verb == "QG":
            return "Read all", "asked for every register"
        if verb == "SM":
            n = len(regs)
            return "All registers", f"{n} value{'s' if n != 1 else ''}"
    if code == "A1020":
        if verb == "SG":
            return "Poll sensors", "asked for sensor readings"
        if verb == "RM":
            return "Sensor report", " \u00b7 ".join(_sensor_words(regs)) or "no data"
    return f"Message {code}", verb


def summarize(frame: codec.Frame | None) -> tuple[str, str, dict, str, str]:
    """-> (cmd, summary, fields, title, code). `fields` maps register hex -> value hex."""
    if frame is None:
        return "", "couldn't decode this message", {}, "Unreadable message", ""
    fields: dict[str, str] = {}
    if frame.verb in ("RM", "SM", "QG"):
        try:
            for r, v in frame.regs().items():
                fields[f"{r:04X}"] = f"{v:04X}"
        except codec.FrameError:
            pass
    title, summary = describe(frame)
    return frame.body[:7], summary, fields, title, frame.code


class WebApp:
    def __init__(
        self,
        hub,
        cfg=None,
        relay=None,
        ack_timeout: float = 10.0,
        ring: int = RING,
        enforce_ingress: bool | None = None,
        mqtt=None,
    ):
        self.hub, self.cfg, self.relay, self.ack_timeout = hub, cfg, relay, ack_timeout
        self.mqtt = mqtt
        self.aqi = AqiTracker.for_hub(hub)  # before our own state callback: sample first
        # under HAOS only the Supervisor ingress proxy may reach the UI
        self.enforce_ingress = (
            bool(os.environ.get("SUPERVISOR_TOKEN")) if enforce_ingress is None else enforce_ingress
        )
        self._serials: dict[str, str] = {}
        self._peers: dict[str, str] = {}
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
        aqi = self.aqi.aqi(mac)
        s = self.hub.session(mac)
        if s is not None:
            self._serials[mac] = s.serial
            if s.peer_ip:
                self._peers[mac] = s.peer_ip
        return {
            "mac": mac,
            "serial": self._serials.get(mac),
            "peer_ip": self._peers.get(mac),
            "aqi": aqi,
            "aqi_band": aqi_band(aqi),
            "fw": st["firmware"],
            "online": bool(st["online"]) and ls is not None and time.time() - ls < ONLINE_WINDOW,
            "last_seen": iso(ls) if ls else None,
            "connected_since": iso(self._since[mac]) if mac in self._since else None,
            "upstream": self._upstream(mac),
            "registers": regs,
        }

    def connections(self, macs: list[str]) -> dict:
        """MQTT and Coway-cloud connection status (never includes passwords)."""
        c = self.cfg
        if self.mqtt is not None:
            mqtt = {"enabled": True, **self.mqtt.status()}
        else:
            mqtt = {
                "enabled": False,
                "source": c.mqtt_source if c else "none",
                "host": c.mqtt_host or None if c else None,
                "port": c.mqtt_port if c else None,
                "tls": bool(c.mqtt_tls) if c else False,
                "username": (c.mqtt_username or None) if c else None,
                "connected": False,
                "error": (c.mqtt_error or None) if c else None,
            }
        if self.relay is None:
            cloud = {
                "enabled": False,
                "host": c.cloud_host if c else None,
                "state": "off",
                "error": None,
            }
        else:
            ups = [self.relay.upstream_status(m) for m in macs]
            states = {u["state"] for u in ups}
            state = (
                "idle"
                if not ups
                else "on"
                if "up" in states
                else "error"
                if "down" in states
                else "connecting"
            )
            cloud = {
                "enabled": True,
                "host": f"{self.relay.host}:{self.relay.port}",
                "state": state,
                "error": next((u["error"] for u in ups if u.get("error")), None),
            }
        return {"mqtt": mqtt, "cloud": cloud}

    def snapshot(self) -> dict:
        macs = self.hub.macs()
        return {
            "bridge": {
                "version": _ver(),
                "relay_enabled": self.relay is not None,
                "listen_port": self.hub.port,
            },
            "connections": self.connections(macs),
            "devices": [self.device(m) for m in macs],
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
        cmd, summary, fields, title, code = summarize(decoded)
        f = {
            "id": self._next_id,
            "mac": mac,
            "ts": iso(),
            "dir": direction,
            "raw": raw.decode("latin-1"),
            "cmd": cmd,
            "code": code,
            "title": title,
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
                    "label": REG_LABELS.get((group, reg), ("Unknown", ""))[0],
                    "tip": REG_LABELS.get(
                        (group, reg),
                        (
                            "",
                            "Not decoded yet. Watch it change in the log to work out what it means.",
                        ),
                    )[1],
                    "display": display_reg(group, reg, val),
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
        self.push_connections()

    def push_connections(self) -> None:
        self._push({"t": "connections", "data": self.connections(self.hub.macs())})

    def attach_mqtt(self, bridge) -> None:
        self.mqtt = bridge
        bridge.add_status_callback(self.push_connections)

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
        if t in ("mode", "timer", "sensitivity"):
            try:
                w = R.writes_for(t, v, ctl) if isinstance(v, str) else None
            except R.CommandRejected as e:
                return t, None, f"{CONFLICT}{e}"
            return (t, w, None) if w else (t, None, "invalid command")
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
            if err.startswith(CONFLICT):
                return web.json_response({"ok": False, "error": err[len(CONFLICT) :]}, status=409)
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
