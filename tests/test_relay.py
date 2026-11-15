import asyncio

import pytest
from coway_bridge.relay import Relay, split_host
from coway_bridge.server import DeviceHub, build_ssl_context, ensure_cert
from test_server import CTL, HDR, MAC, Purifier, wait_for

from coway_bridge import codec


class Upstream:
    """Fake cloud: TLS server that records frames and lets the test push frames down."""

    def __init__(self):
        self.frames: list[bytes] = []
        self.writers: list[asyncio.StreamWriter] = []
        self.conns = 0

    async def start(self, tmp_path, port=0):
        cert, key = ensure_cert(tmp_path)
        self.server = await asyncio.start_server(
            self._h, "127.0.0.1", port, ssl=build_ssl_context(cert, key)
        )
        self.port = self.server.sockets[0].getsockname()[1]

    async def _h(self, r, w):
        self.conns += 1
        self.writers.append(w)
        buf = b""
        try:
            while d := await r.read(4096):
                fr, buf = codec.split_frames(buf + d)
                self.frames += fr
        except (ConnectionError, OSError):
            pass

    async def stop(self):
        self.server.close()
        for w in self.writers:
            w.close()


@pytest.fixture
async def env(tmp_path):
    up = Upstream()
    await up.start(tmp_path)
    cert, key = ensure_cert(tmp_path)
    hub = DeviceHub(silence_timeout=10)
    await hub.start("127.0.0.1", 0, build_ssl_context(cert, key))
    relay = Relay(hub, f"127.0.0.1:{up.port}", backoff_base=0.1, backoff_cap=0.4)
    dirs = []
    hub.add_frame_observer(lambda m, d, raw, dec: dirs.append(d))
    yield hub, relay, up, dirs
    await relay.stop()
    await hub.stop()
    await up.stop()


def test_split_host():
    assert split_host("a.b:1234") == ("a.b", 1234)
    assert split_host("a.b") == ("a.b", 9090)


async def test_forward_both_ways_and_suppress(env):
    hub, relay, up, dirs = env
    p = await Purifier.connect(hub.port)
    p.send("A1000QMV0.0.0.13")
    await wait_for(lambda: relay.upstream_status(MAC)["state"] == "up")
    assert hub.session(MAC).relayed
    p.send(CTL)
    await wait_for(lambda: any(codec.decode_frame(f).body == CTL for f in up.frames))
    assert codec.decode_frame(up.frames[0]).body == "A1000QMV0.0.0.13"  # buffered, flushed first
    down = codec.make_frame(HDR, MAC, "A1011QG00010001000C")
    up.writers[0].write(down)
    await wait_for(lambda: "C>P" in dirs)
    f = await p.recv_until("A1011QG00010001000C")
    assert f is not None
    assert "P>C" in dirs and "A>P" not in dirs[dirs.index("C>P") :]
    await asyncio.sleep(0.4)  # no local traffic while upstream is up
    assert hub.session(MAC).relayed


async def test_local_write_ack_swallowed(env):
    hub, relay, up, dirs = env
    p = await Purifier.connect(hub.port)
    p.send("A1000QMV0.0.0.13")
    await wait_for(lambda: relay.upstream_status(MAC)["state"] == "up")
    await _clear_snapshot_poll(p, relay)
    assert await hub.write(MAC, {3: 2})
    assert relay.pending_acks(MAC) == 1
    p.send("A1011SM0000")  # ack for our write: swallowed
    await wait_for(lambda: relay.pending_acks(MAC) == 0)
    p.send("A1011SM0000")  # unsolicited (cloud's own write): forwarded
    await wait_for(lambda: any(codec.decode_frame(x).body == "A1011SM0000" for x in up.frames))
    assert sum(codec.decode_frame(x).body == "A1011SM0000" for x in up.frames) == 1


async def test_upstream_drop_reverts_and_reconnects(env):
    hub, relay, up, dirs = env
    p = await Purifier.connect(hub.port)
    p.send("A1000QMV0.0.0.13")
    await wait_for(lambda: relay.upstream_status(MAC)["state"] == "up")
    up.writers[0].close()
    await wait_for(lambda: relay.upstream_status(MAC)["state"] == "down")
    assert not hub.session(MAC).relayed
    assert relay.upstream_status(MAC)["error"]
    await wait_for(lambda: up.conns == 2 and relay.upstream_status(MAC)["state"] == "up")
    assert hub.session(MAC).relayed


async def test_unreachable_upstream_backoff(tmp_path):
    cert, key = ensure_cert(tmp_path)
    hub = DeviceHub()
    await hub.start("127.0.0.1", 0, build_ssl_context(cert, key))
    relay = Relay(hub, "127.0.0.1:1", backoff_base=0.05, backoff_cap=0.2)
    seen = []
    relay.add_status_callback(lambda m, s: seen.append(s))
    p = await Purifier.connect(hub.port)
    p.send("A1000QMV0.0.0.13")
    await wait_for(lambda: any(s["state"] == "down" for s in seen))
    assert not hub.session(MAC).relayed
    await wait_for(lambda: sum(s["state"] == "connecting" for s in seen) >= 3)
    await relay.stop()
    await hub.stop()


async def _clear_snapshot_poll(p, relay):
    """The hub's initial A1012QG poll is a local command too: ack it first."""
    p.send("A1012SM0001000A0001")
    await wait_for(lambda: relay.pending_acks(MAC) == 0)


async def _up(relay, mac=MAC):
    await wait_for(lambda: relay.upstream_status(mac)["state"] == "up")


async def test_session_replacement_reattaches(env):
    hub, relay, up, dirs = env
    p1 = await Purifier.connect(hub.port)
    p1.send("A1000QMV0.0.0.13")
    await _up(relay)
    first = hub.session(MAC)
    p2 = await Purifier.connect(hub.port)
    p2.send("A1000QMV0.0.0.13")
    await wait_for(lambda: hub.session(MAC) is not first)
    new = hub.session(MAC)
    await wait_for(lambda: new.relayed)
    assert relay.upstream_status(MAC)["state"] == "up" and up.conns == 1
    up.writers[0].write(codec.make_frame(HDR, MAC, "A1011QG00010001000C"))
    assert await p2.recv_until("A1011QG00010001000C")  # delivered to the new session
    n = len(up.frames)
    p2.send(CTL)
    await wait_for(lambda: len(up.frames) > n)  # purifier frames still forwarded up


async def test_buffer_before_upstream_and_flush(tmp_path):
    up = Upstream()
    await up.start(tmp_path)
    port = up.port
    up.server.close()
    await up.server.wait_closed()
    cert, key = ensure_cert(tmp_path)
    hub = DeviceHub(silence_timeout=10)
    await hub.start("127.0.0.1", 0, build_ssl_context(cert, key))
    relay = Relay(hub, f"127.0.0.1:{port}", backoff_base=0.3, backoff_cap=0.3)
    p = await Purifier.connect(hub.port)
    for i in range(60):
        p.send(f"A1020RM0001{i:04X}0000")
    await wait_for(lambda: len(relay._buf.get(MAC, ())) == 50)
    await asyncio.sleep(0.05)
    assert len(relay._buf[MAC]) == 50  # bounded
    await up.start(tmp_path, port)
    await _up(relay)
    await wait_for(lambda: len(up.frames) >= 50)
    assert not relay._buf.get(MAC)
    bodies = [codec.decode_frame(f).body for f in up.frames]
    assert bodies[-1] == f"A1020RM0001{59:04X}0000"
    await relay.stop()
    await hub.stop()
    await up.stop()


async def test_ack_fifo_matches_local_commands(env):
    hub, relay, up, dirs = env
    p = await Purifier.connect(hub.port)
    p.send("A1000QMV0.0.0.13")
    await _up(relay)
    await _clear_snapshot_poll(p, relay)
    acks = []
    relay.add_ack_callback(lambda m, c: acks.append(c))
    up.writers[0].write(codec.make_frame(HDR, MAC, "A1011QG00010003000A"))  # cloud write first
    await wait_for(lambda: "C>P" in dirs)
    assert await hub.write(MAC, {3: 2})  # then a local one
    assert relay.pending_acks(MAC) == 1

    def n_sm():
        return sum(codec.decode_frame(x).body == "A1011SM0000" for x in up.frames)

    p.send("A1011SM0000")  # ack of the cloud's write: forwarded
    await wait_for(lambda: n_sm() == 1)
    assert not acks
    p.send("A1011SM0000")  # ack of the local write: swallowed
    await wait_for(lambda: acks == ["A1011"])
    await asyncio.sleep(0.2)
    assert n_sm() == 1 and relay.pending_acks(MAC) == 0
    p.send("A1011SM0000")  # nothing pending: forwarded
    await wait_for(lambda: n_sm() == 2)


@pytest.mark.parametrize(("stable", "expect"), [(999.0, 0.4), (0.0, 0.1)])
async def test_backoff_resets_only_after_stable(tmp_path, stable, expect):
    up = Upstream()
    await up.start(tmp_path)
    cert, key = ensure_cert(tmp_path)
    hub = DeviceHub(silence_timeout=10)
    await hub.start("127.0.0.1", 0, build_ssl_context(cert, key))
    relay = Relay(
        hub, f"127.0.0.1:{up.port}", backoff_base=0.1, backoff_cap=0.4, stable_after=stable
    )
    p = await Purifier.connect(hub.port)
    p.send("A1000QMV0.0.0.13")
    for n in range(1, 4):
        await wait_for(lambda n=n: len(up.writers) >= n, 6)
        await _up(relay)
        up.writers[n - 1].close()
        await wait_for(lambda: relay.upstream_status(MAC)["state"] == "down", 6)
    assert relay.delays[MAC] == pytest.approx(expect)
    await relay.stop()
    await hub.stop()
    await up.stop()


# --- isolation: a misbehaving upstream must never affect the purifier path ----------------
async def _plain_server(handler, rcvbuf=None):
    import socket

    sock = socket.socket()
    if rcvbuf:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    sock.bind(("127.0.0.1", 0))
    return await asyncio.start_server(handler, sock=sock), sock.getsockname()[1]


async def _scenario(kind, tmp_path):
    """Returns (host:port, cleanup) for a misbehaving upstream."""
    cert, key = ensure_cert(tmp_path)
    if kind == "refuse":
        return "127.0.0.1:1", None
    if kind == "dns":
        return "no-such-host.invalid:9090", None
    if kind == "tls_hang":

        async def h(r, w):
            await asyncio.sleep(30)

        srv, port = await _plain_server(h)
        return f"127.0.0.1:{port}", srv
    ctx = build_ssl_context(cert, key)
    if kind == "no_read":

        async def h(r, w):
            w.transport.pause_reading()
            await asyncio.sleep(30)

        import socket

        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        sock.bind(("127.0.0.1", 0))
        srv = await asyncio.start_server(h, sock=sock, ssl=ctx)
        return f"127.0.0.1:{sock.getsockname()[1]}", srv

    async def h(r, w):  # drop: accept, then close mid-session
        await asyncio.sleep(0.2)
        w.close()

    srv = await asyncio.start_server(h, "127.0.0.1", 0, ssl=ctx)
    return f"127.0.0.1:{srv.sockets[0].getsockname()[1]}", srv


@pytest.mark.parametrize("kind", ["refuse", "dns", "tls_hang", "no_read", "drop"])
async def test_bad_upstream_never_affects_local(tmp_path, kind):
    host, srv = await _scenario(kind, tmp_path)
    cert, key = ensure_cert(tmp_path)
    hub = DeviceHub(silence_timeout=30)
    await hub.start("127.0.0.1", 0, build_ssl_context(cert, key))
    relay = Relay(
        hub,
        host,
        backoff_base=0.1,
        backoff_cap=0.2,
        connect_timeout=0.3,
        write_timeout=0.3,
    )
    states = []
    hub.add_state_callback(lambda m, s: states.append(s))
    p = await Purifier.connect(hub.port)
    try:
        p.send("A1000QMV0.0.0.13")
        await wait_for(lambda: hub.session(MAC) is not None)
        # flood frames (fills the upstream's buffers in the no_read case), reports must keep flowing
        flood = b"".join(
            codec.make_frame(HDR, MAC, f"A1020RM0001{i % 65535:04X}0000") for i in range(4000)
        )
        p.w.write(flood)
        n0 = len(states)
        t0 = asyncio.get_running_loop().time()
        await wait_for(lambda: len(states) >= n0 + 100, 1)
        assert asyncio.get_running_loop().time() - t0 < 1
        # web/MQTT command is delivered promptly and acked
        t0 = asyncio.get_running_loop().time()
        assert await asyncio.wait_for(hub.write(MAC, {3: 2}), 1)
        p.send("A1011SM0000")
        assert asyncio.get_running_loop().time() - t0 < 1
        # local acks (re)start within a short window once upstream is not up
        seen = []

        async def drain():
            while True:
                f = await p.recv(5)
                if f is None:
                    return
                seen.append(f.code)

        async def feed():  # the purifier keeps reporting; once upstream is down it gets acks
            while True:
                p.send("A1020RM0001000100aa")
                await asyncio.sleep(0.05)

        d, fd = asyncio.create_task(drain()), asyncio.create_task(feed())
        if kind != "no_read":  # there the link is up (just not draining): cloud acks, we stay quiet
            await wait_for(lambda: seen.count("A1020") >= 3, 3)
        else:
            await asyncio.sleep(0.3)
        d.cancel()
        fd.cancel()
        assert hub.session(MAC).online
        assert hub.session(MAC).relayed == (relay.upstream_status(MAC)["state"] == "up")
    finally:
        await relay.stop()
        await hub.stop()
        if srv:
            srv.close()


async def test_stalled_writer_drops_oldest_with_warning(tmp_path, caplog):
    host, srv = await _scenario("no_read", tmp_path)
    cert, key = ensure_cert(tmp_path)
    hub = DeviceHub(silence_timeout=30)
    await hub.start("127.0.0.1", 0, build_ssl_context(cert, key))
    relay = Relay(hub, host, connect_timeout=1, write_timeout=5, queue_max=20)
    p = await Purifier.connect(hub.port)
    p.send("A1000QMV0.0.0.13")
    await _up(relay)
    for i in range(3000):
        p.send(f"A1020RM0001{i:04X}0000")
    await wait_for(lambda: any("dropping oldest" in r.message for r in caplog.records), 3)
    await relay.stop()
    await hub.stop()
    srv.close()


async def _next(ws, kind):
    while True:
        m = await asyncio.wait_for(ws.receive_json(), 5)
        if m["t"] == kind:
            return m


async def test_ws_pushes_connections_on_upstream_and_mqtt(env):
    from aiohttp.test_utils import TestClient, TestServer
    from coway_bridge.config import Config
    from coway_bridge.mqtt_bridge import MqttBridge
    from coway_bridge.web import WebApp

    hub, relay, up, _ = env
    web = WebApp(hub, Config(mqtt_host="h", mqtt_port=1883, mqtt_tls=False), relay)
    mq = MqttBridge(hub, web.cfg)
    web.attach_mqtt(mq)
    client = TestClient(TestServer(web.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/api/ws")
        assert (await ws.receive_json())["t"] == "state"
        p = await Purifier.connect(hub.port)
        p.send("A1000QMV0.0.0.13")
        while True:
            m = await _next(ws, "connections")
            if m["data"]["cloud"]["state"] == "on":
                break
        mq.connected = True
        m = await _next(ws, "connections")
        assert m["data"]["mqtt"]["connected"] is True
        mq.error = "boom"
        m = await _next(ws, "connections")
        assert m["data"]["mqtt"]["error"] == "boom"
        await ws.close()
    finally:
        await client.close()
