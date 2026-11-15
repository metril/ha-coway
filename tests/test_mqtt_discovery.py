import logging

from aiohttp import ClientSession, web
from coway_bridge.__main__ import App
from coway_bridge.config import Config

PW = "s3cretpw"
GOOD = {
    "result": "ok",
    "data": {"host": "core-mosquitto", "port": 1884, "username": "u", "password": PW, "ssl": False},
}


async def supervisor(payload, status=200):
    seen = {}

    async def h(request):
        seen["auth"] = request.headers.get("Authorization")
        return web.json_response(payload, status=status)

    app = web.Application()
    app.router.add_get("/services/mqtt", h)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}", seen


async def run_app(tmp_path, sup_url, env, **kw):
    cfg = Config(listen_host="127.0.0.1", ingress_port=0, data_dir=str(tmp_path), **kw)
    app = App(cfg, web_host="127.0.0.1", listen_port=0, supervisor_url=sup_url, env=env)
    await app.start()
    async with ClientSession() as s:
        j = await (await s.get(f"http://127.0.0.1:{app.web_port}/api/state")).json()
    return app, j["connections"]["mqtt"]


async def test_discovery_success(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    runner, url, seen = await supervisor(GOOD)
    app, m = await run_app(tmp_path, url, {"SUPERVISOR_TOKEN": "tok"})
    try:
        assert seen["auth"] == "Bearer tok"
        assert (m["source"], m["host"], m["port"], m["username"]) == (
            "supervisor",
            "core-mosquitto",
            1884,
            "u",
        )
        assert PW not in str(m)
        assert "using broker from Home Assistant: core-mosquitto:1884 (tls off, user u)" in (
            caplog.text
        )
        assert PW not in caplog.text
    finally:
        await app.stop()
        await runner.cleanup()


async def test_explicit_options_win(tmp_path):
    runner, url, _ = await supervisor(GOOD)
    app, m = await run_app(
        tmp_path, url, {"SUPERVISOR_TOKEN": "tok"}, mqtt_tls=True, mqtt_username="me"
    )
    try:
        assert m["source"] == "supervisor" and m["tls"] is True and m["username"] == "me"
    finally:
        await app.stop()
        await runner.cleanup()


async def test_explicit_host_skips_supervisor(tmp_path):
    runner, url, seen = await supervisor(GOOD)
    app, m = await run_app(tmp_path, url, {"SUPERVISOR_TOKEN": "tok"}, mqtt_host="127.0.0.1")
    try:
        assert m["source"] == "options" and "auth" not in seen
    finally:
        await app.stop()
        await runner.cleanup()


async def test_no_service_runs_without_mqtt(tmp_path, caplog):
    runner, url, _ = await supervisor({"result": "error", "message": "x"}, status=400)
    app, m = await run_app(tmp_path, url, {"SUPERVISOR_TOKEN": "tok"})
    try:
        assert m["source"] == "none" and m["enabled"] is False
        assert m["error"].startswith("MQTT: no broker configured.")
        assert app.bridge is None and app._mqtt_task is None
        assert "no broker configured" in caplog.text
    finally:
        await app.stop()
        await runner.cleanup()


async def test_no_token_is_none(tmp_path):
    app, m = await run_app(tmp_path, "http://127.0.0.1:1", {})
    try:
        assert m["source"] == "none" and m["enabled"] is False and m["error"]
    finally:
        await app.stop()


async def test_options_without_mqtt_keys(tmp_path):
    import json

    from coway_bridge.config import load_config

    p = tmp_path / "options.json"
    p.write_text(json.dumps({"allowed_macs": [], "cloud_relay": False, "log_level": "info"}))
    cfg = load_config(p, env={})
    assert (cfg.mqtt_host, cfg.mqtt_port, cfg.mqtt_tls) == ("", None, None)
    runner, url, _ = await supervisor(GOOD)
    cfg.listen_host, cfg.ingress_port, cfg.data_dir = "127.0.0.1", 0, str(tmp_path)
    app = App(
        cfg, web_host="127.0.0.1", listen_port=0, supervisor_url=url, env={"SUPERVISOR_TOKEN": "t"}
    )
    await app.start()
    try:
        assert (cfg.mqtt_port, cfg.mqtt_tls) == (1884, False)
    finally:
        await app.stop()
        await runner.cleanup()
    # no discovery, host only: defaults
    cfg2 = load_config(p, env={"COWAY_MQTT_HOST": "h"})
    from coway_bridge.mqtt_discovery import resolve_mqtt

    await resolve_mqtt(cfg2, {})
    assert (cfg2.mqtt_port, cfg2.mqtt_tls) == (1883, False)
    cfg3 = load_config(p, env={"COWAY_MQTT_HOST": "h", "COWAY_MQTT_TLS": "true"})
    await resolve_mqtt(cfg3, {})
    assert (cfg3.mqtt_port, cfg3.mqtt_tls) == (8883, True)
