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
    assert (s["light"], s["sensitivity"], s["timer"]) == ("ON", "sensitive", "2h")
    assert (s["pm10"], s["lux"], s["prefilter"], s["hepa"]) == (5, 894, 70, 0)
    assert s["reg_000E"] == 1 and s["sreg_0030"] == 7
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
    assert c("sensitivity", "insensitive", DEV) == {0xA: 1} and c("sensitivity", "x", DEV) is None
    assert [c("timer", t, DEV)[8] for t in ("off", "1h", "2h", "4h", "8h")] == [
        0,
        60,
        120,
        240,
        480,
    ]
    assert c("nope", "1", DEV) is None


def test_discovery():
    msgs = m.discovery_messages(MAC, "0.0.0.13", (0x30,))
    topics = {t for t, _ in msgs}
    for t in (
        "fan/coway_0008dc000001/fan",
        "switch/coway_0008dc000001/light",
        "select/coway_0008dc000001/sensitivity",
        "select/coway_0008dc000001/timer",
        "sensor/coway_0008dc000001/pm10",
        "sensor/coway_0008dc000001/lux",
        "sensor/coway_0008dc000001/prefilter",
        "sensor/coway_0008dc000001/hepa",
        "sensor/coway_0008dc000001/reg_0022",
        "sensor/coway_0008dc000001/sreg_0030",
    ):
        assert f"homeassistant/{t}/config" in topics
    ids = [p["unique_id"] for _, p in msgs]
    assert len(ids) == len(set(ids))
    fan = dict(msgs)["homeassistant/fan/coway_0008dc000001/fan/config"]
    assert fan["default_entity_id"] == "fan.coway_0008dc000001_fan"
    assert all("object_id" not in p for _, p in msgs)
    assert fan["preset_modes"] == ["smart", "bedtime", "manual"]
    d = fan["device"]
    assert (d["manufacturer"], d["model"], d["sw_version"]) == ("Coway", "AP-2015E", "0.0.0.13")
    assert d["connections"] == [["mac", "00:08:dc:00:00:01"]]
    assert fan["availability_mode"] == "all" and len(fan["availability"]) == 2
    diag = dict(msgs)["homeassistant/sensor/coway_0008dc000001/reg_000E/config"]
    assert (
        diag["entity_category"] == "diagnostic" and diag["entity_registry_enabled_default"] is False
    )
    json.dumps(msgs)
    assert "sw_version" not in m.discovery_messages(MAC)[0][1]["device"]


class FakeClient:
    instances: list = []
    fail_first_publish = False

    def __init__(self, *a, **kw):
        self.published = []
        self.messages = self._msgs()
        FakeClient.instances.append(self)

    async def _msgs(self):
        await asyncio.Event().wait()
        yield  # pragma: no cover

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        self.closed = True

    async def subscribe(self, *a):
        pass

    async def publish(self, topic, payload, retain=False):
        if FakeClient.fail_first_publish and self is FakeClient.instances[0] and "/state" in topic:
            raise RuntimeError("boom")
        self.published.append((topic, payload, retain))


@pytest.fixture
def fake_mqtt(monkeypatch):
    import aiomqtt

    FakeClient.instances = []
    FakeClient.fail_first_publish = False
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
