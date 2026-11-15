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
    hub = DeviceHub(poll_interval=0.1, silence_timeout=10)
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
    assert hub.session(MAC).suppress_polls
    p.send(CTL)
    await wait_for(lambda: any(codec.decode_frame(f).body == CTL for f in up.frames))
    assert codec.decode_frame(up.frames[0]).body == "A1000QMV0.0.0.13"  # buffered, flushed first
    down = codec.make_frame(HDR, MAC, "A1011QG00010001000C")
    up.writers[0].write(down)
    await wait_for(lambda: "C>P" in dirs)
    f = await p.recv_until("A1011QG00010001000C")
    assert f is not None
    assert "P>C" in dirs and "A>P" not in dirs[dirs.index("C>P") :]
    await asyncio.sleep(0.4)  # polls suppressed: nothing else sent locally
    assert hub.session(MAC).suppress_polls


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
    assert not hub.session(MAC).suppress_polls
    assert relay.upstream_status(MAC)["error"]
    await wait_for(lambda: up.conns == 2 and relay.upstream_status(MAC)["state"] == "up")
    assert hub.session(MAC).suppress_polls


async def test_unreachable_upstream_backoff(tmp_path):
    cert, key = ensure_cert(tmp_path)
    hub = DeviceHub(poll_interval=0.1)
    await hub.start("127.0.0.1", 0, build_ssl_context(cert, key))
    relay = Relay(hub, "127.0.0.1:1", backoff_base=0.05, backoff_cap=0.2)
    seen = []
    relay.add_status_callback(lambda m, s: seen.append(s))
    p = await Purifier.connect(hub.port)
    p.send("A1000QMV0.0.0.13")
    await wait_for(lambda: any(s["state"] == "down" for s in seen))
    assert not hub.session(MAC).suppress_polls
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
    await wait_for(lambda: new.suppress_polls)
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
    hub = DeviceHub(poll_interval=30, silence_timeout=10)
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
    hub = DeviceHub(poll_interval=30, silence_timeout=10)
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
