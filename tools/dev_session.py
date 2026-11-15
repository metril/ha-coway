"""Run the bridge locally (MQTT off) with a simulated purifier, to look at the web UI.

    .venv/bin/python tools/dev_session.py [--listen-port 19090] [--ingress-port 18099]

Uses obviously fake identifiers. Ctrl-C to stop.
"""

from __future__ import annotations

import argparse
import asyncio
import ssl
import tempfile

from coway_bridge.__main__ import App
from coway_bridge.config import Config

from coway_bridge import codec

HDR, MAC = "010100000TST0000000001", "0008DC000001"


async def purifier(port: int) -> None:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1
    ctx.set_ciphers("ALL:@SECLEVEL=0")
    r, w = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
    ctl = {
        1: 1,
        2: 0,
        3: 2,
        7: 0,
        8: 0,
        0xA: 1,
        0xE: 1,
        0x12: 0,
        0x18: 1,
        0x19: 1,
        0x21: 0,
        0x22: 0,
    }
    sen = {2: 12, 7: 893, 0x11: 0, 0x12: 100}

    def send(body: str) -> None:
        w.write(codec.make_frame(HDR, MAC, body))

    send("A1000QMV0.0.0.10")
    send("A1010RM" + codec.build_regs(ctl))
    send("A1020RM" + codec.build_regs(sen))
    buf = b""
    while True:
        data = await r.read(4096)
        if not data:
            return
        frames, buf = codec.split_frames(buf + data)
        for raw in frames:
            f = codec.decode_frame(raw)
            if f.code == "A1011" and f.verb == "QG":
                for reg, val in f.regs().items():
                    ctl[reg] = val
                    if reg == 3:
                        ctl[2] = 0
                send("A1011SM0000")
                send("A1010RM" + codec.build_regs(ctl))
            elif f.code == "A1010" and f.verb == "SG":
                send("A1010RM" + codec.build_regs(ctl))
            elif f.code == "A1020" and f.verb == "SG":
                send("A1020RM" + codec.build_regs(sen))
            elif f.code == "A1012" and f.verb == "QG":
                send("A1012SM" + codec.build_regs(ctl))
            elif f.code == "A1000":
                send("A1000QMV0.0.0.10")


async def main(a) -> None:
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(listen_host="127.0.0.1", ingress_port=a.ingress_port, data_dir=d)
        app = App(cfg, mqtt=False, web_host="127.0.0.1", listen_port=a.listen_port)
        await app.start()
        print(f"web: http://127.0.0.1:{app.web_port}/  purifiers: :{app.hub.port}", flush=True)
        if not a.no_purifier:
            sim = asyncio.create_task(purifier(app.hub.port))  # keep a reference
            sim.add_done_callback(lambda t: t.exception())
        try:
            await asyncio.Event().wait()
        finally:
            await app.stop()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--listen-port", type=int, default=19090)
    p.add_argument("--ingress-port", type=int, default=18099)
    p.add_argument("--no-purifier", action="store_true")
    try:
        asyncio.run(main(p.parse_args()))
    except KeyboardInterrupt:
        pass
