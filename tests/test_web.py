import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer
from coway_bridge.server import DeviceHub, build_ssl_context, ensure_cert
from coway_bridge.web import WebApp
from test_server import CTL, HDR, MAC, SEN, Purifier, wait_for

from coway_bridge import codec


@pytest.fixture
async def env(tmp_path):
    cert, key = ensure_cert(tmp_path)
    hub = DeviceHub(poll_interval=30, silence_timeout=10)
    await hub.start("127.0.0.1", 0, build_ssl_context(cert, key))
    web = WebApp(hub, ack_timeout=0.5, ring=50)
    client = TestClient(TestServer(web.app))
    await client.start_server()
    yield hub, web, client
    await client.close()
    await hub.stop()


async def connect(hub, web):
    p = await Purifier.connect(hub.port)
    p.send("A1000QMV0.0.0.13")
    p.send(CTL)
    p.send(SEN)
    await wait_for(lambda: hub.device_state(MAC)["sensor"].get(7) == 894)
    return p


async def test_index_and_state(env):
    hub, web, c = env
    r = await c.get("/")
    assert r.status == 200 and "coway-bridge" in await r.text()
    assert (await (await c.get("/api/state")).json())["devices"] == []
    p = await connect(hub, web)
    s = await (await c.get("/api/state")).json()
    assert s["bridge"]["relay_enabled"] is False
    d = s["devices"][0]
    assert d["mac"] == MAC and d["fw"] == "0.0.0.13" and d["online"]
    assert d["upstream"]["state"] == "disabled"
    regs = {(r["group"], r["reg"]): r for r in d["registers"]}
    assert regs[("control", "0001")]["decoded"] == "On" and regs[("control", "0001")]["known"]
    assert regs[("sensor", "0007")]["name"] == "lux"
    unk = regs[("control", "000E")]
    assert unk["known"] is False and unk["name"] == "reg_000E" and unk["decoded"] == "unknown"
    assert not p.w.is_closing()


async def test_ws_messages(env):
    hub, web, c = env
    ws = await c.ws_connect("/api/ws")
    first = await ws.receive_json()
    assert first["t"] == "state" and first["data"]["devices"] == []
    p = await Purifier.connect(hub.port)
    p.send(CTL)
    kinds = set()
    for _ in range(60):
        m = await asyncio.wait_for(ws.receive_json(), 3)
        kinds.add(m["t"])
        if m["t"] == "frame":
            fr = m["data"]
            assert fr["dir"] == "P>A" and fr["cmd"] == "A1010RM" and "power=On" in fr["summary"]
        if {"frame", "reg", "device"} <= kinds:
            break
    assert {"frame", "reg", "device"} <= kinds
    await ws.close()


async def test_ring_buffer_frames_capture(env):
    hub, web, c = env
    p = await Purifier.connect(hub.port)
    for _ in range(60):
        p.send("A1000QMV0.0.0.13")
    await wait_for(lambda: web._next_id > 60)
    assert len(web.frames) == 50 and len(web._raw) == 50
    j = await (await c.get(f"/api/frames?mac={MAC}&limit=10")).json()
    assert len(j["frames"]) == 10 and j["frames"][-1]["id"] == web._next_id - 1
    assert (await c.get("/api/frames?limit=x")).status == 400
    r = await c.get(f"/api/capture?mac={MAC}")
    assert (
        "attachment" in r.headers["Content-Disposition"] and MAC in r.headers["Content-Disposition"]
    )
    lines = (await r.text()).splitlines()
    assert len(lines) == 50
    assert codec.decode_frame(bytes.fromhex(lines[0])).body.startswith("A1000QM")


async def test_commands(env):
    hub, web, c = env
    url = f"/api/devices/{MAC}/command"
    r = await c.post(url, json={"type": "power", "value": "off"})
    assert r.status == 409 and (await r.json())["error"] == "Purifier offline"
    assert (await c.post(url, json={"type": "bogus"})).status == 400
    assert (await c.post(url, json={"type": "raw", "reg": "12", "val": "0001"})).status == 400
    assert (await c.post(url, json={"type": "speed", "value": 9})).status == 400
    assert (await c.post(url, data="nope")).status == 400
    assert MAC not in hub.macs()  # failed commands must not create devices
    p = await connect(hub, web)

    async def ack_loop():
        while True:
            f = await p.recv()
            if f is None:
                return
            if f.code == "A1011":
                p.send("A1011SM0000")
            elif f.code == "A1012":
                p.send("A1012SM0001000A0001")

    t = asyncio.create_task(ack_loop())
    r = await c.post(url, json={"type": "speed", "value": 2})
    j = await r.json()
    assert r.status == 200 and j["ok"] and j["acked"] and j["at"]
    assert (await c.post(url, json={"type": "raw", "reg": "0007", "val": "0002"})).status == 200
    assert (await c.post(url, json={"type": "light", "value": "off"})).status == 200
    assert (await c.post(url, json={"type": "snapshot"})).status == 200
    t.cancel()
    # no ack -> 504
    r = await c.post(url, json={"type": "power", "value": "on"})
    assert r.status == 504 and "No ack" in (await r.json())["error"]
    p.w.close()


async def test_ingress_guard(tmp_path, monkeypatch):
    cert, key = ensure_cert(tmp_path)
    hub = DeviceHub(poll_interval=30)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    assert WebApp(hub).enforce_ingress is False
    monkeypatch.setenv("SUPERVISOR_TOKEN", "x")
    web = WebApp(hub)
    assert web.enforce_ingress is True
    c = TestClient(TestServer(web.app))
    await c.start_server()
    try:
        assert (await c.get("/api/state")).status == 403  # remote is 127.0.0.1
        web.enforce_ingress = False
        assert (await c.get("/api/state")).status == 200
    finally:
        await c.close()


async def test_serial_in_state(env):
    hub, web, c = env
    p = await connect(hub, web)
    d = (await (await c.get("/api/state")).json())["devices"][0]
    assert d["serial"] == "00000TST0000000001"
    p.w.close()


async def test_cloud_ack_does_not_satisfy_web_wait(tmp_path):
    from coway_bridge.relay import Relay
    from test_relay import Upstream, _up

    up = Upstream()
    await up.start(tmp_path)
    cert, key = ensure_cert(tmp_path)
    hub = DeviceHub(poll_interval=30, silence_timeout=10)
    await hub.start("127.0.0.1", 0, build_ssl_context(cert, key))
    relay = Relay(hub, f"127.0.0.1:{up.port}", backoff_base=0.1)
    web = WebApp(hub, relay=relay, ack_timeout=3)
    c = TestClient(TestServer(web.app))
    await c.start_server()
    try:
        p = await Purifier.connect(hub.port)
        p.send("A1000QMV0.0.0.13")
        await _up(relay)
        up.writers[0].write(codec.make_frame(HDR, MAC, "A1011QG00010003000A"))
        await wait_for(lambda: relay._fifo.get(MAC))
        t = asyncio.create_task(
            c.post(f"/api/devices/{MAC}/command", json={"type": "power", "value": "on"})
        )
        await wait_for(lambda: relay.pending_acks(MAC) == 1)
        p.send("A1011SM0000")  # ack of the cloud's write
        await asyncio.sleep(0.3)
        assert not t.done()
        p.send("A1011SM0000")  # ack of the local one
        r = await asyncio.wait_for(t, 3)
        assert r.status == 200
    finally:
        await c.close()
        await relay.stop()
        await hub.stop()
        await up.stop()
