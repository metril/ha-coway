import asyncio

from aiohttp import ClientSession
from coway_bridge.__main__ import App
from coway_bridge.config import Config
from test_server import CTL, MAC, Purifier, wait_for


async def test_full_app_smoke(tmp_path):
    cfg = Config(
        listen_host="127.0.0.1",
        ingress_port=0,
        data_dir=str(tmp_path),
        log_frames_to_file=True,
    )
    app = App(cfg, mqtt=False, web_host="127.0.0.1", listen_port=0)
    await app.start()
    try:
        assert app.relay is None
        p = await Purifier.connect(app.hub.port)
        p.send(CTL)
        await wait_for(lambda: app.hub.device_state(MAC)["control"])
        async with ClientSession() as s:
            base = f"http://127.0.0.1:{app.web_port}"
            j = await (await s.get(f"{base}/api/state")).json()
            assert j["devices"][0]["mac"] == MAC
            assert (await s.get(f"{base}/")).status == 200
        await asyncio.sleep(0.05)
        assert (tmp_path / "frames.log").stat().st_size > 0
        p.w.close()
    finally:
        await app.stop()


async def test_app_with_relay_enabled(tmp_path):
    cfg = Config(
        listen_host="127.0.0.1",
        ingress_port=0,
        data_dir=str(tmp_path),
        cloud_relay=True,
        cloud_host="127.0.0.1:1",
    )
    app = App(cfg, mqtt=False, web_host="127.0.0.1", listen_port=0)
    await app.start()
    try:
        async with ClientSession() as s:
            j = await (await s.get(f"http://127.0.0.1:{app.web_port}/api/state")).json()
            assert j["bridge"]["relay_enabled"] is True
    finally:
        await app.stop()
