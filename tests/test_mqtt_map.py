import asyncio
import json

import pytest
from coway_bridge.config import Config
from coway_bridge.server import DeviceHub

from coway_bridge import mqtt_bridge as m

MAC = "0008DC000001"
DEV = {
    "control": {1: 1, 2: 0, 3: 2, 7: 2, 8: 120, 0xA: 3, 0xE: 1, 0x18: 0},
    "sensor": {2: 5, 7: 894, 0x11: 30, 0x12: 100, 0x30: 7},
}


def test_derive_state():
    s = m.derive_state(DEV)
    assert s["power"] == "ON" and s["percentage"] == 66 and s["preset"] == "manual"
    assert (s["light"], s["sensitivity"], s["timer"]) == ("ON", "insensitive", "2h")
    assert (s["pm10"], s["light_sensor"], s["pre_filter"], s["max2_filter"]) == (5, 894, 70, 0)
    assert not any(k.startswith(("reg_", "sreg_")) for k in s)
    off = m.derive_state({"control": {1: 0, 3: 3, 2: 1}, "sensor": {}})
    assert off["power"] == "OFF" and off["percentage"] == 0 and off["preset"] == "smart"
    assert m.derive_state({"control": {}, "sensor": {}}) == {}


def test_commands():
    c = m.command_to_writes
    assert c("power", "ON", DEV) == {1: 1} and c("power", "OFF", DEV) == {1: 0}
    assert c("power", "x", DEV) is None
    assert c("percentage", "33", DEV) == {3: 1}
    assert c("percentage", "50", DEV) == {3: 2} and c("percentage", "100", DEV) == {3: 3}
    assert c("percentage", "0", DEV) == {1: 0}
    assert c("percentage", "66", {"control": {1: 0}}) == {1: 1, 3: 2}
    assert c("preset", "smart", DEV) == {2: 1} and c("preset", "bedtime", DEV) == {2: 2}
    assert c("preset", "manual", DEV) == {3: 2} and c("preset", "bogus", DEV) is None
    assert c("light", "ON", DEV) == {7: 2} and c("light", "OFF", DEV) == {7: 0}
    smart = {"control": {**DEV["control"], 2: 1}}
    bed = {"control": {**DEV["control"], 2: 2}}
    assert c("sensitivity", "insensitive", smart) == {0xA: 3}
    assert c("sensitivity", "x", smart) is None
    assert [c("timer", t, bed)[8] for t in ("off", "1h", "2h", "4h", "8h")] == [
        0,
        60,
        120,
        240,
        480,
    ]
    assert c("nope", "1", DEV) is None


def test_discovery():
    msgs = m.discovery_messages(MAC, "0.0.0.13")
    topics = {t for t, _ in msgs}
    for t in (
        "fan/airmega_400s_0008dc000001/fan",
        "light/airmega_400s_0008dc000001/light",
        "select/airmega_400s_0008dc000001/sensitivity",
        "select/airmega_400s_0008dc000001/timer",
        "sensor/airmega_400s_0008dc000001/pm10",
        "sensor/airmega_400s_0008dc000001/light_sensor",
        "sensor/airmega_400s_0008dc000001/pre_filter",
        "sensor/airmega_400s_0008dc000001/max2_filter",
    ):
        assert f"homeassistant/{t}/config" in topics
    ids = [p["unique_id"] for _, p in msgs]
    assert len(ids) == len(set(ids))
    fan = dict(msgs)["homeassistant/fan/airmega_400s_0008dc000001/fan/config"]
    assert fan["name"] is None
    assert all("object_id" not in p for _, p in msgs)
    assert fan["preset_modes"] == ["smart", "bedtime", "manual"]
    d = fan["device"]
    assert (d["manufacturer"], d["model"], d["sw_version"]) == ("Coway", "AP-2015E", "0.0.0.13")
    assert d["name"] == "Airmega 400S" and d["identifiers"] == ["airmega_400s_0008dc000001"]
    assert fan["unique_id"] == "airmega_400s_0008dc000001_fan"
    assert all("default_entity_id" not in p and "object_id" not in p for _, p in msgs)
    assert "Coway" not in " ".join(p["name"] or "" for _, p in msgs)
    assert d["connections"] == [["mac", "00:08:dc:00:00:01"]]
    assert fan["availability_mode"] == "all" and len(fan["availability"]) == 2
    assert not any("reg_" in t for t in topics)
    assert not any("coway" in t.lower() for t in topics)
    json.dumps(msgs)
    assert "sw_version" not in m.discovery_messages(MAC)[0][1]["device"]


class _FakePaho:
    """Minimal paho stand-in: publish() returns an info with a mid; acks go to on_publish."""

    def __init__(self):
        self.mid = 0
        self.on_publish = lambda *a: None

    def publish(self, topic, *a, **kw):
        self.mid += 1
        return type("Info", (), {"mid": self.mid})()


class FakeClient:
    instances: list = []
    fail_first_publish = False
    sub_rc: list = [1]
    pub_rc = None
    puback_rc = None

    def __init__(self, *a, **kw):
        self.published = []
        self.kw = kw
        self.messages = self._msgs()
        self._client = _FakePaho()
        FakeClient.instances.append(self)

    async def _msgs(self):
        await asyncio.Event().wait()
        yield  # pragma: no cover

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        self.closed = True

    async def subscribe(self, *a, **kw):
        return FakeClient.sub_rc

    async def publish(self, topic, payload, qos=0, retain=False):
        if FakeClient.fail_first_publish and self is FakeClient.instances[0] and "/state" in topic:
            raise RuntimeError("boom")
        self.published.append((topic, payload, retain))
        info = self._client.publish(topic, payload)
        if FakeClient.puback_rc is not None:  # v5 PUBACK reason code via paho callback
            self._client.on_publish(None, None, info.mid, FakeClient.puback_rc, None)
        return FakeClient.pub_rc


@pytest.fixture
def fake_mqtt(monkeypatch):
    import aiomqtt

    FakeClient.instances = []
    FakeClient.fail_first_publish = False
    FakeClient.sub_rc = [1]
    FakeClient.pub_rc = None
    FakeClient.puback_rc = None
    monkeypatch.setattr(aiomqtt, "Client", FakeClient)
    return FakeClient


def _bridge():
    hub = DeviceHub()
    hub._dev(MAC)["online"] = True
    hub._dev(MAC)["control"] = {1: 1}
    b = m.MqttBridge(hub, Config(mqtt_host="x"))
    b.retry_delay = 0.05
    return b


async def test_clean_shutdown_publishes_retained_offline(fake_mqtt):
    b = _bridge()
    task = asyncio.create_task(b.run())
    for _ in range(100):
        if fake_mqtt.instances and any(
            t.endswith("/online") for t, *_ in fake_mqtt.instances[0].published
        ):
            break
        await asyncio.sleep(0.02)
    b.request_stop()
    await asyncio.wait_for(task, 3)
    c = fake_mqtt.instances[0]
    assert c.closed
    assert c.published[-2:] == [
        ("coway_bridge/status", "offline", True),
        (f"coway_bridge/{MAC.lower()}/online", "offline", True),
    ]


async def test_publisher_error_triggers_reconnect(fake_mqtt):
    fake_mqtt.fail_first_publish = True
    b = _bridge()
    task = asyncio.create_task(b.run())
    for _ in range(150):
        if len(fake_mqtt.instances) >= 2:
            break
        await asyncio.sleep(0.02)
    assert len(fake_mqtt.instances) >= 2
    b.request_stop()
    await asyncio.wait_for(task, 3)


async def test_subscribe_refused_logs_error_and_reconnects_with_backoff(fake_mqtt, caplog):
    fake_mqtt.sub_rc = [0x80]
    b = _bridge()
    b.cfg.mqtt_username = "coway"
    task = asyncio.create_task(b.run())
    for _ in range(150):
        if len(fake_mqtt.instances) >= 3:
            break
        await asyncio.sleep(0.02)
    assert len(fake_mqtt.instances) >= 3
    b.request_stop()
    await asyncio.wait_for(task, 3)
    errs = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errs and "coway_bridge/+/+/set" in errs[0].getMessage()
    assert "coway" in errs[0].getMessage()


async def test_backoff_capped_at_60s():
    assert m.MAX_RETRY_DELAY == 60.0
    assert min(5.0 * 2**10, m.MAX_RETRY_DELAY) == 60.0


async def test_publish_refused_logged_once_per_topic(fake_mqtt, caplog):
    fake_mqtt.pub_rc = 0x87  # v5 "Not authorized"
    b = _bridge()
    task = asyncio.create_task(b.run())
    await asyncio.sleep(0.3)
    b._put("coway_bridge/x/state", "1")
    b._put("coway_bridge/x/state", "2")
    await asyncio.sleep(0.2)
    b.request_stop()
    await asyncio.wait_for(task, 3)
    msgs = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert sum("coway_bridge/x/state" in x for x in msgs) == 1
    assert any("coway_bridge/status" in x for x in msgs)


async def test_uses_mqtt_v5(fake_mqtt):
    import aiomqtt

    b = _bridge()
    task = asyncio.create_task(b.run())
    await asyncio.sleep(0.2)
    b.request_stop()
    await asyncio.wait_for(task, 3)
    assert fake_mqtt.instances[0].kw["protocol"] == aiomqtt.ProtocolVersion.V5


async def test_puback_refused_via_paho_hook_logs_acl_error_once(fake_mqtt, caplog):
    fake_mqtt.puback_rc = 0x87  # Not authorized
    b = _bridge()
    b.cfg.mqtt_username = "coway"
    task = asyncio.create_task(b.run())
    await asyncio.sleep(0.3)
    b._put("coway_bridge/x/state", "1")
    b._put("coway_bridge/x/state", "2")
    await asyncio.sleep(0.2)
    b.request_stop()
    await asyncio.wait_for(task, 3)
    msgs = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    mine = [x for x in msgs if "coway_bridge/x/state" in x]
    assert len(mine) == 1
    assert "check broker ACL for user 'coway'" in mine[0]


def test_aqi_and_names():
    assert m.derive_state({"control": {}, "sensor": {2: 12}}, aqi=11)["aqi"] == 11
    assert "aqi" not in m.derive_state({"control": {}, "sensor": {2: 12}})
    msgs = {t.split("/")[-2]: p for t, p in m.discovery_messages(MAC)}
    assert msgs["aqi"]["name"] == "AQI" and msgs["aqi"]["device_class"] == "aqi"
    assert "unit_of_measurement" not in msgs["aqi"]
    assert msgs["aqi"]["unique_id"] == f"airmega_400s_{MAC.lower()}_aqi"
    assert (
        msgs["light_sensor"]["name"] == "Light sensor"
        and msgs["light_sensor"]["unit_of_measurement"] == "lx"
    )
    assert (
        msgs["pre_filter"]["name"] == "Pre-filter" and msgs["max2_filter"]["name"] == "Max2 filter"
    )
    assert msgs["max2_filter"]["unique_id"] == f"airmega_400s_{MAC.lower()}_max2_filter"


def test_writes_for_shared():
    from coway_bridge import registers as R

    off = {1: 0, 3: 2}
    assert R.writes_for("mode", "smart", off) == {1: 1, 2: 1}
    assert R.writes_for("preset", "manual", off) == {1: 1, 3: 2}
    assert R.writes_for("mode", "manual", {1: 1}) == {3: 1}
    assert R.writes_for("mode", "x", off) is None
    bed, smart = {1: 1, 2: 2}, {1: 1, 2: 1}
    assert R.writes_for("timer", "8h", bed) == {8: 480} and R.writes_for("timer", "9h", bed) is None
    assert R.writes_for("sensitivity", "moderate", smart) == {0xA: 2}
    assert R.writes_for("sensitivity", "sensitive", bed) == {0xA: 1}
    assert R.writes_for("sensitivity", "insensitive", {1: 0, 2: 0}) == {0xA: 3}
    for ent, val, ctl, msg in [
        ("timer", "8h", smart, "Timer only works in Bedtime mode"),
        ("timer", "8h", {1: 0, 2: 2}, "Timer only works in Bedtime mode"),
    ]:
        with pytest.raises(R.CommandRejected, match=msg):
            R.writes_for(ent, val, ctl)
    assert R.writes_for("bogus", "x", off) is None


async def test_discovery_summary_logged_once_per_connect(fake_mqtt, caplog):
    import logging

    caplog.set_level(logging.INFO)
    b = _bridge()
    task = asyncio.create_task(b.run())
    await asyncio.sleep(0.3)
    b._on_state(MAC, b.hub.device_state(MAC))  # repeated state must not re-log
    b._on_state(MAC, b.hub.device_state(MAC))
    await asyncio.sleep(0.1)
    b.request_stop()
    await asyncio.wait_for(task, 3)
    lines = [r.getMessage() for r in caplog.records if "published discovery" in r.getMessage()]
    assert len(lines) == 1
    assert MAC in lines[0] and "configs (fan, light, sensitivity, timer, pm10, aqi" in lines[0]
    assert "homeassistant/" in lines[0]


@pytest.mark.parametrize(
    "rc,text",
    [
        (135, "not authorized. Set mqtt_username and mqtt_password (or check the broker's ACL)."),
        (134, "bad username or password."),
        (5, "not authorized."),
        (4, "bad username or password."),
    ],
)
async def test_connack_refused_actionable_error(monkeypatch, caplog, rc, text):
    import aiomqtt

    n = []

    class Refusing:
        def __init__(self, *a, **kw):
            n.append(1)

        async def __aenter__(self):
            raise aiomqtt.MqttCodeError(rc)

        async def __aexit__(self, *a):
            pass

    monkeypatch.setattr(aiomqtt, "Client", Refusing)
    b = _bridge()
    task = asyncio.create_task(b.run())
    for _ in range(150):
        if len(n) >= 3:
            break
        await asyncio.sleep(0.02)
    assert len(n) >= 3  # backoff retries continue
    assert f"MQTT broker refused the connection: {text}" in b.status()["error"]
    b.request_stop()
    await asyncio.wait_for(task, 3)
    errs = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert errs and f"MQTT broker refused the connection: {text}" in errs[0]


def test_fan_icon():
    msgs = {t.split("/")[-2]: p for t, p in m.discovery_messages(MAC)}
    assert msgs["fan"]["icon"] == "mdi:air-purifier"


async def test_periodic_republish_of_availability(fake_mqtt):
    b = _bridge()
    b.keepalive_interval = 0.05
    b.hub._dev("AABBCCDDEEFF")["online"] = False
    task = asyncio.create_task(b.run())
    await asyncio.sleep(0.4)
    b.request_stop()
    await asyncio.wait_for(task, 3)
    pub = fake_mqtt.instances[0].published
    assert sum(p == ("coway_bridge/status", "online", True) for p in pub) >= 3
    mine = (f"coway_bridge/{MAC.lower()}/online", "online", True)
    assert sum(p == mine for p in pub) >= 3
    assert not any(
        t.startswith("coway_bridge/aabbccddeeff/online") and p == "online" for t, p, _ in pub
    )


async def test_shutdown_only_offlines_connected_devices(fake_mqtt):
    b = _bridge()
    b.hub._dev("AABBCCDDEEFF")["online"] = False
    task = asyncio.create_task(b.run())
    await asyncio.sleep(0.2)
    b.request_stop()
    await asyncio.wait_for(task, 3)
    pub = fake_mqtt.instances[0].published
    assert (f"coway_bridge/{MAC.lower()}/online", "offline", True) in pub[-2:]
    assert not any(
        t == "coway_bridge/aabbccddeeff/online" and p == "offline" for t, p, _ in pub[-2:]
    )


def test_mode_dependent_availability():
    msgs = dict(m.discovery_messages(MAC))
    base = [{"topic": "coway_bridge/status"}, {"topic": f"coway_bridge/{MAC.lower()}/online"}]
    sens = msgs[f"homeassistant/select/airmega_400s_{MAC.lower()}/sensitivity/config"]
    assert sens["name"] == "Smart sensitivity" and sens["availability"] == base
    assert sens["options"] == ["insensitive", "moderate", "sensitive"]
    p = msgs[f"homeassistant/select/airmega_400s_{MAC.lower()}/timer/config"]
    assert p["name"] == "Bedtime timer" and p["availability_mode"] == "all"
    assert p["unique_id"] == f"airmega_400s_{MAC.lower()}_timer"
    a = p["availability"]
    assert a[:2] == base
    assert a[2]["topic"] == f"coway_bridge/{MAC.lower()}/state"
    assert "value_json.preset == 'bedtime'" in a[2]["value_template"]
    assert "value_json.power == 'ON'" in a[2]["value_template"]
    assert (a[2]["payload_available"], a[2]["payload_not_available"]) == ("online", "offline")
    fan = msgs[f"homeassistant/fan/airmega_400s_{MAC.lower()}/fan/config"]
    assert len(fan["availability"]) == 2


def test_light_is_light_platform():
    msgs = dict(m.discovery_messages(MAC))
    light = msgs["homeassistant/light/airmega_400s_0008dc000001/light/config"]
    assert light["state_value_template"] == "{{ value_json.light }}"
    assert "value_template" not in light
