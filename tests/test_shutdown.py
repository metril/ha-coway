import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "coway_bridge" / "app"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_sigterm_exits_promptly(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(APP)}
    p = subprocess.Popen(
        [
            sys.executable, "-m", "coway_bridge",
            "--config", str(tmp_path / "none.json"),
            "--data-dir", str(tmp_path),
            "--ingress-port", str(_free_port()),
            "--listen-port", str(_free_port()),
            "--mqtt-host", "127.0.0.1",
            "--mqtt-port", str(_free_port()),  # nothing listens here
        ],
        env=env,
        stderr=subprocess.PIPE,
        text=True,
    )  # fmt: skip
    try:
        time.sleep(2.0)  # let it start and hit the MQTT retry loop
        assert p.poll() is None, p.stderr.read()
        p.send_signal(signal.SIGTERM)
        t0 = time.time()
        p.wait(timeout=8)
        assert time.time() - t0 < 8
    finally:
        if p.poll() is None:
            p.kill()
        p.stderr.close()


async def _fake_broker(host="127.0.0.1"):
    """Minimal MQTT 3.1.1 broker: acks CONNECT/SUBSCRIBE/PUBLISH(qos1)/PINGREQ, keeps sockets open."""

    async def handle(r, w):
        try:
            while True:
                b0 = (await r.readexactly(1))[0]
                n, mult = 0, 1
                while True:
                    d = (await r.readexactly(1))[0]
                    n += (d & 0x7F) * mult
                    mult *= 128
                    if not d & 0x80:
                        break
                body = await r.readexactly(n) if n else b""
                t = b0 >> 4
                if t == 1:
                    w.write(b"\x20\x02\x00\x00")
                elif t == 8:
                    w.write(b"\x90\x03" + body[:2] + b"\x01")
                elif t == 3 and (b0 >> 1) & 3 == 1:
                    tl = int.from_bytes(body[:2], "big")
                    w.write(b"\x40\x02" + body[2 + tl : 4 + tl])
                elif t == 12:
                    w.write(b"\xd0\x00")
                elif t == 14:
                    break
                await w.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            w.close()

    srv = await asyncio.start_server(handle, host, 0)
    return srv, srv.sockets[0].getsockname()[1]


def test_sigterm_with_mqtt_and_websocket_client(tmp_path):
    import aiohttp

    async def run() -> float:
        srv, mqtt_port = await _fake_broker()
        web_port = _free_port()
        env = {**os.environ, "PYTHONPATH": str(APP)}
        p = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "coway_bridge",
            "--config", str(tmp_path / "none.json"),
            "--data-dir", str(tmp_path),
            "--ingress-port", str(web_port),
            "--listen-port", str(_free_port()),
            "--mqtt-host", "127.0.0.1",
            "--mqtt-port", str(mqtt_port),
            env=env,
        )  # fmt: skip
        try:
            async with aiohttp.ClientSession() as s:
                ws = None
                for _ in range(100):
                    try:
                        ws = await s.ws_connect(f"http://127.0.0.1:{web_port}/api/ws")
                        break
                    except aiohttp.ClientError:
                        await asyncio.sleep(0.1)
                assert ws is not None
                await ws.receive(timeout=5)  # initial state
                await asyncio.sleep(1.0)  # MQTT session established
                t0 = time.time()
                p.send_signal(signal.SIGTERM)
                await asyncio.wait_for(p.wait(), 6)
                return time.time() - t0
        finally:
            if p.returncode is None:
                p.kill()
                await p.wait()
            srv.close()

    assert asyncio.run(run()) < 6
