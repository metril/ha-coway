"""Home Assistant MQTT discovery + state/command bridge.

Mapping is done by pure functions (derive_state, command_to_writes, discovery_messages) so they
can be tested without a broker. MqttBridge wires them to a DeviceHub and an aiomqtt client.

Topics: <base>/status (bridge LWT), <base>/<mac>/online, <base>/<mac>/state (JSON),
        <base>/<mac>/<entity>/set (commands).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import ssl

from . import registers as R
from .aqi import AqiTracker
from .server import DeviceHub

log = logging.getLogger(__name__)
BASE = "coway_bridge"  # default topic prefix
NODE = "airmega_400s"
PREFIX = "homeassistant"
SPEEDS = {1: 33, 2: 66, 3: 100}
TIMER_OPTIONS = list(R.TIMER.values())
SENS_OPTIONS = list(R.SENSITIVITY.values())


def dev_id(mac: str) -> str:
    return mac.lower()


def speed_from_percentage(p: int) -> int:
    return 1 if p <= 33 else 2 if p <= 66 else 3


def derive_state(dev: dict, aqi: int | None = None) -> dict:
    """device_state() dict -> flat JSON-able state published to <base>/<mac>/state."""
    c, s = dev.get("control", {}), dev.get("sensor", {})
    out: dict = {}
    power = c.get(R.R_POWER)
    if power is not None:
        out["power"] = "ON" if power else "OFF"
        out["percentage"] = SPEEDS.get(c.get(R.R_SPEED), 0) if power else 0
    if R.R_MODE in c:
        out["preset"] = R.MODE_NAMES.get(c[R.R_MODE], "manual")
    if R.R_LIGHT in c:
        out["light"] = "ON" if c[R.R_LIGHT] == R.LIGHT_ON else "OFF"
    if c.get(R.R_SENS) in R.SENSITIVITY:
        out["sensitivity"] = R.SENSITIVITY[c[R.R_SENS]]
    if c.get(R.R_TIMER) in R.TIMER:
        out["timer"] = R.TIMER[c[R.R_TIMER]]
    if R.S_PM10 in s:
        out["pm10"] = s[R.S_PM10]
    if aqi is not None:
        out["aqi"] = aqi
    if R.S_LUX in s:
        out["light_sensor"] = s[R.S_LUX]
    if R.S_PREFILTER in s:
        out["pre_filter"] = max(0, 100 - s[R.S_PREFILTER])
    if R.S_HEPA in s:
        out["max2_filter"] = max(0, 100 - s[R.S_HEPA])
    return out


def command_to_writes(entity: str, payload: str, dev: dict) -> dict[int, int] | None:
    """Map an MQTT command to {reg: val} writes; None if invalid/unsupported."""
    c = dev.get("control", {})
    p = payload.strip()
    if entity == "power":
        if p.upper() not in ("ON", "OFF"):
            return None
        return {R.R_POWER: int(p.upper() == "ON")}
    if entity == "percentage":
        try:
            pct = int(float(p))
        except ValueError:
            return None
        if pct <= 0:
            return {R.R_POWER: 0}
        w = {R.R_SPEED: speed_from_percentage(pct)}
        if not c.get(R.R_POWER):
            w = {R.R_POWER: 1, **w}
        return w
    if entity in ("preset", "timer", "sensitivity"):
        return R.writes_for(entity, p, c)
    if entity == "light":
        if p.upper() not in ("ON", "OFF"):
            return None
        return {R.R_LIGHT: R.LIGHT_ON if p.upper() == "ON" else R.LIGHT_OFF}
    return None


def discovery_messages(
    mac: str,
    firmware: str | None = None,
    base: str = BASE,
    prefix: str = PREFIX,
) -> list[tuple[str, dict]]:
    """[(config_topic, payload)] for all entities of one device."""
    did = dev_id(mac)
    t = f"{base}/{did}"
    device = {
        "identifiers": [f"{NODE}_{did}"],
        "connections": [["mac", ":".join(mac[i : i + 2] for i in range(0, 12, 2)).lower()]],
        "manufacturer": "Coway",
        "model": "AP-2015E",
        "name": "Airmega 400S",
    }
    if firmware:
        device["sw_version"] = firmware
    common = {
        "device": device,
        "availability": [{"topic": f"{base}/status"}, {"topic": f"{t}/online"}],
        "availability_mode": "all",
        "state_topic": f"{t}/state",
    }
    msgs: list[tuple[str, dict]] = []

    def add(platform: str, key: str, payload: dict) -> None:
        payload = {
            **common,
            "unique_id": f"{NODE}_{did}_{key}",
            **payload,
        }
        msgs.append((f"{prefix}/{platform}/{NODE}_{did}/{key}/config", payload))

    add(
        "fan",
        "fan",
        {
            "icon": "mdi:air-purifier",
            "name": None,  # main entity takes the device name -> fan.airmega_400s
            "state_value_template": "{{ value_json.power }}",
            "command_topic": f"{t}/power/set",
            "percentage_state_topic": f"{t}/state",
            "percentage_value_template": "{{ value_json.percentage }}",
            "percentage_command_topic": f"{t}/percentage/set",
            "preset_mode_state_topic": f"{t}/state",
            "preset_mode_value_template": "{{ value_json.preset }}",
            "preset_mode_command_topic": f"{t}/preset/set",
            "preset_modes": list(R.PRESETS),
        },
    )
    add(
        "switch",
        "light",
        {
            "name": "Light",
            "value_template": "{{ value_json.light }}",
            "command_topic": f"{t}/light/set",
            "icon": "mdi:lightbulb",
        },
    )

    def bedtime_only() -> dict:
        return {
            "availability": [
                *common["availability"],
                {
                    "topic": f"{t}/state",
                    "value_template": (
                        "{{ 'online' if value_json.power == 'ON' "
                        "and value_json.preset == 'bedtime' else 'offline' }}"
                    ),
                    "payload_available": "online",
                    "payload_not_available": "offline",
                },
            ],
            "availability_mode": "all",
        }

    add(
        "select",
        "sensitivity",
        {
            "name": "Smart sensitivity",
            "value_template": "{{ value_json.sensitivity }}",
            "command_topic": f"{t}/sensitivity/set",
            "options": SENS_OPTIONS,
            "icon": "mdi:tune",
        },
    )
    add(
        "select",
        "timer",
        {
            **bedtime_only(),
            "name": "Bedtime timer",
            "value_template": "{{ value_json.timer }}",
            "command_topic": f"{t}/timer/set",
            "options": TIMER_OPTIONS,
            "icon": "mdi:timer-outline",
        },
    )
    add(
        "sensor",
        "pm10",
        {
            "name": "PM10",
            "value_template": "{{ value_json.pm10 }}",
            "device_class": "pm10",
            "unit_of_measurement": "µg/m³",
            "state_class": "measurement",
        },
    )
    add(
        "sensor",
        "aqi",
        {
            "name": "AQI",
            "value_template": "{{ value_json.aqi }}",
            "device_class": "aqi",
            "state_class": "measurement",
        },
    )
    add(
        "sensor",
        "light_sensor",
        {
            "name": "Light sensor",
            "value_template": "{{ value_json.light_sensor }}",
            "device_class": "illuminance",
            "unit_of_measurement": "lx",
            "state_class": "measurement",
        },
    )
    add(
        "sensor",
        "pre_filter",
        {
            "name": "Pre-filter",
            "value_template": "{{ value_json.pre_filter }}",
            "unit_of_measurement": "%",
            "icon": "mdi:air-filter",
            "state_class": "measurement",
        },
    )
    add(
        "sensor",
        "max2_filter",
        {
            "name": "Max2 filter",
            "value_template": "{{ value_json.max2_filter }}",
            "unit_of_measurement": "%",
            "icon": "mdi:air-filter",
            "state_class": "measurement",
        },
    )
    return msgs


MAX_RETRY_DELAY = 60.0


def _rc_value(rc) -> int:
    """Reason/return code as int (aiomqtt returns ints for MQTT 3.1.1, ReasonCode for v5)."""
    v = getattr(rc, "value", rc)
    return v if isinstance(v, int) else 0


def rc_failed(rc) -> bool:
    return _rc_value(rc) >= 0x80


class SubscribeRefused(Exception):
    pass


_NOT_AUTH = (
    "MQTT broker refused the connection: not authorized. "
    "Set mqtt_username and mqtt_password (or check the broker's ACL)."
)
_BAD_CRED = "MQTT broker refused the connection: bad username or password."


def _connack_refusal(e: Exception) -> str | None:
    """Actionable text for a CONNACK refusal (v5 reason 134/135, v3.1.1 rc 4/5)."""
    rc = getattr(e, "rc", None)
    code = getattr(rc, "value", rc)
    if code in (135, 5):
        return _NOT_AUTH
    if code in (134, 4):
        return _BAD_CRED
    return None


class MqttBridge:
    def __init__(self, hub: DeviceHub, cfg, base: str | None = None, prefix: str = PREFIX):
        self.hub, self.cfg, self.prefix = hub, cfg, prefix
        self.base = base or getattr(cfg, "mqtt_base_topic", "") or BASE
        self._q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._stop = asyncio.Event()
        self.keepalive_interval = 300.0  # re-publish retained availability this often
        self.retry_delay = 5.0  # initial backoff; doubles per failure up to MAX_RETRY_DELAY
        self._pub_refused: set[str] = set()
        self._logged_discovery: dict[str, tuple] = {}
        self._firmware: dict[str, str | None] = {}
        self.aqi = AqiTracker.for_hub(hub)
        self.connected = False
        self.error: str | None = None
        hub.add_state_callback(self._on_state)
        hub.add_online_callback(self._on_online)

    def status(self) -> dict:
        """Connection status for the web UI (never includes the password)."""
        c = self.cfg
        return {
            "source": c.mqtt_source,
            "host": c.mqtt_host,
            "port": c.mqtt_port,
            "tls": bool(c.mqtt_tls),
            "username": c.mqtt_username or None,
            "connected": self.connected,
            "error": self.error,
        }

    def _state_json(self, mac: str, dev: dict) -> str:
        return json.dumps(derive_state(dev, self.aqi.aqi(mac)))

    def _put(self, topic: str, payload: str, retain: bool = True) -> None:
        try:
            self._q.put_nowait((topic, payload, retain))
        except asyncio.QueueFull:
            log.warning("mqtt queue full, dropping %s", topic)

    def _publish_discovery(self, mac: str) -> None:
        msgs = discovery_messages(mac, self._firmware.get(mac), self.base, self.prefix)
        for topic, payload in msgs:
            self._put(topic, json.dumps(payload))
        key = (mac, tuple(t for t, _ in msgs))
        if key != self._logged_discovery.get(mac):  # once per (re)connect, not per state change
            self._logged_discovery[mac] = key
            log.info(
                "published discovery for %s: %d configs (%s) under %s/",
                mac,
                len(msgs),
                ", ".join(t.split("/")[-2] for t, _ in msgs),
                self.prefix,
            )

    def _on_state(self, mac: str, dev: dict) -> None:
        self._put(f"{self.base}/{dev_id(mac)}/state", self._state_json(mac, dev))

    def _on_online(self, mac: str, online: bool, firmware: str | None) -> None:
        if firmware != self._firmware.get(mac) or mac not in self._firmware:
            self._firmware[mac] = firmware
            self._publish_discovery(mac)
        self._put(f"{self.base}/{dev_id(mac)}/online", "online" if online else "offline")

    def _republish_all(self) -> None:
        for mac in self.hub.macs():
            dev = self.hub.device_state(mac)
            self._firmware.setdefault(mac, dev["firmware"])
            self._publish_discovery(mac)
            self._put(f"{self.base}/{dev_id(mac)}/state", self._state_json(mac, dev))
            self._put(f"{self.base}/{dev_id(mac)}/online", "online" if dev["online"] else "offline")

    async def handle_command(self, topic: str, payload: str) -> None:
        parts = topic.split("/")
        if len(parts) != 4 or parts[0] != self.base or parts[3] != "set":
            return
        mac = next((m for m in self.hub.macs() if dev_id(m) == parts[1]), None)
        if mac is None:
            return
        dev = self.hub.device_state(mac)
        try:
            writes = command_to_writes(parts[2], payload, dev)
        except R.CommandRejected as e:
            log.warning("ignoring command %s=%r: %s", topic, payload, e)
            self._put(f"{self.base}/{dev_id(mac)}/state", self._state_json(mac, dev))
            return
        if not writes:
            log.warning("ignoring command %s=%r", topic, payload)
            return
        await self.hub.write(mac, writes)

    def request_stop(self) -> None:
        """Ask run() to publish retained offline topics, disconnect cleanly and return."""
        self._stop.set()

    def _check_pub(self, topic: str, rc) -> None:
        """Log (once per topic) if the broker refused a publish (v5 PUBACK reason code >= 0x80)."""
        if rc_failed(rc):
            if topic not in self._pub_refused:
                self._pub_refused.add(topic)
                log.error(
                    "mqtt publish to %s refused by broker (reason code 0x%02x): "
                    "check broker ACL for user %r",
                    topic,
                    _rc_value(rc),
                    self.cfg.mqtt_username or None,
                )
        else:
            self._pub_refused.discard(topic)

    def _hook_puback(self, client) -> None:
        """aiomqtt drops PUBACK reason codes; tap paho's on_publish to see ACL refusals."""
        pc = getattr(client, "_client", None)
        if pc is None or not hasattr(pc, "on_publish"):
            return
        topics: dict[int, str] = {}
        orig_publish, orig_cb = pc.publish, pc.on_publish

        def publish(topic, *a, **kw):
            info = orig_publish(topic, *a, **kw)
            if getattr(info, "mid", None) is not None:
                topics[info.mid] = topic
            return info

        def on_publish(c, userdata, mid, reason_code=None, properties=None):
            t = topics.pop(mid, None)
            if t is not None and reason_code is not None:
                self._check_pub(t, reason_code)
            return orig_cb(c, userdata, mid, reason_code, properties)

        pc.publish, pc.on_publish = publish, on_publish

    async def _pub(self, client, topic: str, payload, retain: bool = True) -> None:
        """QoS1 publish; refusals are logged via the PUBACK hook (or the return value)."""
        rc = await client.publish(topic, payload, qos=1, retain=retain)
        if rc is not None:
            self._check_pub(topic, rc)

    async def _subscribe(self, client, topic: str) -> None:
        rcs = await client.subscribe(topic, qos=1)
        if not isinstance(rcs, (list, tuple)):
            rcs = [rcs]
        bad = [rc for rc in rcs if rc_failed(rc)]
        if bad:
            log.error(
                "mqtt subscribe to %s refused by broker (reason code 0x%02x): "
                "check broker ACL for user %r",
                topic,
                _rc_value(bad[0]),
                self.cfg.mqtt_username or None,
            )
            raise SubscribeRefused(topic)

    async def _publish_offline(self, client) -> None:
        await self._pub(client, f"{self.base}/status", "offline")
        for mac in self.hub.macs():
            if self.hub.device_state(mac)["online"]:  # only devices connected to this bridge
                await self._pub(client, f"{self.base}/{dev_id(mac)}/online", "offline")

    async def _heartbeat(self, client) -> None:
        """Re-publish retained availability so another bridge's LWT/shutdown can't stick."""
        while True:
            await asyncio.sleep(self.keepalive_interval)
            await self._pub(client, f"{self.base}/status", "online")
            for mac in self.hub.macs():
                if self.hub.device_state(mac)["online"]:
                    await self._pub(client, f"{self.base}/{dev_id(mac)}/online", "online")

    async def _listen(self, client) -> None:
        async for m in client.messages:
            await self.handle_command(str(m.topic), m.payload.decode(errors="replace"))

    async def _wait_stop(self) -> None:
        await self._stop.wait()
        raise _Stop

    async def run(self) -> None:
        import aiomqtt

        cfg = self.cfg
        delay = self.retry_delay
        while not self._stop.is_set():
            try:
                tls = ssl.create_default_context() if cfg.mqtt_tls else None
                async with aiomqtt.Client(
                    cfg.mqtt_host,
                    cfg.mqtt_port,
                    username=cfg.mqtt_username or None,
                    password=cfg.mqtt_password or None,
                    tls_context=tls,
                    protocol=aiomqtt.ProtocolVersion.V5,
                    will=aiomqtt.Will(f"{self.base}/status", "offline", retain=True),
                ) as client:
                    self._hook_puback(client)
                    log.info("mqtt connected")
                    self.connected, self.error = True, None
                    self._logged_discovery.clear()
                    while not self._q.empty():
                        self._q.get_nowait()
                    await self._pub(client, f"{self.base}/status", "online")
                    await self._subscribe(client, f"{self.base}/+/+/set")
                    delay = self.retry_delay
                    self._republish_all()
                    stopping = False
                    try:
                        async with asyncio.TaskGroup() as tg:
                            tg.create_task(self._publisher(client))
                            tg.create_task(self._listen(client))
                            tg.create_task(self._heartbeat(client))
                            tg.create_task(self._wait_stop())
                    except* _Stop:
                        stopping = True
                    except* Exception as eg:  # publisher/listener failure: reconnect
                        self.error = f"session failed: {eg.exceptions[0]!r}"
                        log.warning("mqtt session failed: %s; reconnecting", eg.exceptions)
                    self.connected = False
                    if stopping:
                        await self._publish_offline(client)  # before the clean disconnect
                        return
            except (aiomqtt.MqttError, SubscribeRefused) as e:
                self.connected = False
                refused = _connack_refusal(e)
                if refused:
                    self.error = refused
                    log.error("%s Retrying in %gs.", refused, delay)
                else:
                    self.error = str(e) or type(e).__name__
                    log.warning("mqtt error: %r; retrying in %gs", e, delay)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), delay)
            delay = min(delay * 2, MAX_RETRY_DELAY)

    async def _publisher(self, client) -> None:
        while True:
            topic, payload, retain = await self._q.get()
            await self._pub(client, topic, payload, retain)


class _Stop(Exception):
    pass
