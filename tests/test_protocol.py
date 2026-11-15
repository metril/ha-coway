"""Protocol-correct behaviour, modelled on a real purifier<->cloud capture:

The purifier pushes A1020RM every ~60 s (A1010RM on setting changes); the cloud acks each with the
matching SG and is otherwise silent. Commands: C->P A1011QG -> P->C A1011SM (+ A1010RM) -> C->P
A1010SG. Snapshot: C->P A1012QG -> P->C A1012SM. Time is compressed: silences use short sleeps and
the hub/relay time-outs are far longer than the test, so a (wrongly) timed-out upstream shows up.
"""

import asyncio

import pytest
from coway_bridge.relay import Relay
from coway_bridge.server import DeviceHub, build_ssl_context, ensure_cert
from test_relay import Upstream, _up
from test_server import CTL, HDR, MAC, SEN, Purifier, wait_for

from coway_bridge import codec

HELLO = "A1000QMV0.0.0.13"


def bodies(frames):
    return [codec.decode_frame(f).body for f in frames]


class Capture(Purifier):
    """Purifier that records every frame the bridge sends it."""

    got: list

    async def pump(self):
        self.got = []
        try:
            while f := await self.recv(30):
                self.got.append(f.body)
        except (TimeoutError, asyncio.CancelledError, ConnectionError, OSError):
            pass


async def cap(port):
    p = await Capture.connect(port)
    p.task = asyncio.create_task(p.pump())
    await asyncio.sleep(0)
    return p


async def settle(p, relay):
    """Upstream is up. The hello arrived before it was, so it was acked locally once: forget it."""
    await _up(relay)
    await wait_for(lambda: p.got == ["A1000SG"])
    p.got.clear()


@pytest.fixture
async def relay_env(tmp_path):
    up = Upstream()
    await up.start(tmp_path)
    cert, key = ensure_cert(tmp_path)
    # long time-outs: nothing may time out during the compressed "90 s" of cloud silence
    hub = DeviceHub(silence_timeout=60, snapshot_grace=0.3)
    await hub.start("127.0.0.1", 0, build_ssl_context(cert, key))
    relay = Relay(hub, f"127.0.0.1:{up.port}", backoff_base=0.1, backoff_cap=0.2, write_timeout=5)
    yield hub, relay, up
    await relay.stop()
    await hub.stop()
    await up.stop()


@pytest.fixture
async def local_hub(tmp_path):
    cert, key = ensure_cert(tmp_path)
    hub = DeviceHub(silence_timeout=60)
    await hub.start("127.0.0.1", 0, build_ssl_context(cert, key))
    yield hub
    await hub.stop()


async def test_relay_exchange_silence_and_no_local_traffic(relay_env):
    hub, relay, up = relay_env
    p = await cap(hub.port)
    p.send(HELLO)
    await settle(p, relay)
    await wait_for(lambda: bodies(up.frames)[:1] == [HELLO])
    up.writers[0].write(codec.make_frame(HDR, MAC, "A1000SG"))  # cloud ack
    p.send(SEN)  # periodic sensor push
    p.send(CTL)  # settings push
    await wait_for(lambda: bodies(up.frames)[1:] == [SEN, CTL])
    for sg in ("A1020SG", "A1010SG"):
        up.writers[0].write(codec.make_frame(HDR, MAC, sg))
    # cloud-issued command round trip
    up.writers[0].write(codec.make_frame(HDR, MAC, "A1011QG00010001000C"))
    await wait_for(lambda: "A1011QG00010001000C" in p.got)
    for b in ("A1011SM0000", CTL):
        p.send(b)
    up.writers[0].write(codec.make_frame(HDR, MAC, "A1010SG"))
    await wait_for(lambda: bodies(up.frames)[-2:] == ["A1011SM0000", CTL])
    # cloud snapshot request is forwarded down and its reply up
    up.writers[0].write(codec.make_frame(HDR, MAC, "A1012QG"))
    await wait_for(lambda: "A1012QG" in p.got)
    p.send("A1012SM0001000A0001")
    await wait_for(lambda: bodies(up.frames)[-1] == "A1012SM0001000A0001")
    # long cloud silence: upstream stays connected, nothing flips
    states = []
    relay.add_status_callback(lambda m, s: states.append(s["state"]))
    await asyncio.sleep(1.0)
    assert relay.upstream_status(MAC)["state"] == "up" and up.conns == 1 and not states
    assert hub.session(MAC).relayed
    p.send(SEN)
    await wait_for(lambda: bodies(up.frames)[-1] == SEN)
    # the purifier received exactly what the cloud sent: no local SG, no local A1012QG
    assert p.got == [
        "A1000SG",  # the cloud's
        "A1020SG",
        "A1010SG",
        "A1011QG00010001000C",
        "A1010SG",
        "A1012QG",
    ]
    assert up.conns == 1


async def test_user_command_and_ack_swallow_in_relay_mode(relay_env):
    hub, relay, up = relay_env
    p = await cap(hub.port)
    p.send(HELLO)
    await settle(p, relay)
    assert await hub.write(MAC, {3: 2})
    await wait_for(lambda: "A1011QG000100030002" in p.got)
    p.send("A1011SM0000")  # ack of our command: swallowed
    p.send(SEN)
    await wait_for(lambda: bodies(up.frames)[-1] == SEN)
    assert "A1011SM0000" not in bodies(up.frames)
    assert p.got == ["A1011QG000100030002"]  # and still no local SG / snapshot


async def test_reconnect_replays_hello_first(relay_env):
    hub, relay, up = relay_env
    p = await cap(hub.port)
    p.send(HELLO)
    await _up(relay)
    p.send(SEN)
    await wait_for(lambda: bodies(up.frames) == [HELLO, SEN])
    up.writers[0].close()  # cloud drops us
    await wait_for(lambda: relay.upstream_status(MAC)["state"] != "up")
    p.send(CTL)  # arrives while upstream is down
    await wait_for(lambda: up.conns == 2 and relay.upstream_status(MAC)["state"] == "up")
    await wait_for(lambda: len(up.frames) >= 4)
    assert bodies(up.frames)[2:4] == [HELLO, CTL]  # hello first, then buffered report
    assert up.conns == 2


async def test_local_mode_acks_each_push_once_no_periodic(local_hub):
    hub = local_hub
    p = await cap(hub.port)
    p.send(HELLO)
    p.send(SEN)
    p.send(CTL)
    p.send("A1011SM0000")  # command ack, snapshot reply: never acked by the bridge
    p.send("A1012SM0001000A0001")
    await wait_for(lambda: len(p.got) >= 4)
    await asyncio.sleep(0.6)  # no periodic unsolicited SG afterwards
    assert sorted(p.got) == sorted(["A1000SG", "A1020SG", "A1010SG", "A1012QG"])
    p.send(SEN)
    await wait_for(lambda: p.got.count("A1020SG") == 2)
    assert len(p.got) == 5


async def test_switch_local_relay_local(relay_env, tmp_path):
    hub, relay, up = relay_env
    p = await cap(hub.port)
    p.send(HELLO)
    await settle(p, relay)
    p.send(SEN)
    await wait_for(lambda: len(up.frames) == 2)
    await asyncio.sleep(0.2)
    assert p.got == []  # relay mode: cloud acks, bridge silent
    # upstream goes away mid-session: the bridge acks locally at once
    port = up.port
    up.server.close()
    up.writers[0].close()
    await wait_for(lambda: not hub.session(MAC).relayed)
    p.send(SEN)
    await wait_for(lambda: p.got == ["A1020SG"])
    p.send(CTL)
    await wait_for(lambda: p.got == ["A1020SG", "A1010SG"])
    # upstream returns: hello replayed first, local acks stop
    n = len(up.frames)
    await up.start(tmp_path, port)
    await _up(relay)
    await wait_for(lambda: len(up.frames) >= n + 3)
    assert bodies(up.frames)[n] == HELLO
    p.send(SEN)
    await wait_for(lambda: bodies(up.frames)[-1] == SEN)
    await asyncio.sleep(0.2)
    assert p.got == ["A1020SG", "A1010SG"]
