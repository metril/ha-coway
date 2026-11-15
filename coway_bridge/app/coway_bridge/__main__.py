"""Entry point: python -m coway_bridge [--config PATH] [overrides]."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import logging.handlers
import os
import signal
import threading
from pathlib import Path

from .config import load_config
from .mqtt_bridge import MqttBridge
from .mqtt_discovery import resolve_mqtt
from .relay import Relay
from .server import DeviceHub, build_ssl_context, ensure_cert
from .web import WebApp, bound_port, start_web

log = logging.getLogger(__name__)

SHUTDOWN_TIMEOUT = 5.0  # hard backstop for the whole shutdown sequence


def _frame_logger(data_dir: str):
    """Rotated frames.log in the data dir (option log_frames_to_file)."""
    h = logging.handlers.RotatingFileHandler(
        Path(data_dir) / "frames.log", maxBytes=2_000_000, backupCount=3
    )
    lg = logging.getLogger("coway_bridge.frames")
    lg.propagate = False
    lg.addHandler(h)
    lg.setLevel(logging.INFO)

    def observer(mac, direction, raw, decoded):
        lg.info("%s %s %s", direction, mac, raw.hex())

    return observer


class App:
    def __init__(
        self,
        cfg,
        mqtt: bool = True,
        web_host: str = "0.0.0.0",
        listen_port: int | None = None,
        supervisor_url: str = "http://supervisor",
        env: dict | None = None,
    ):
        self.cfg, self.use_mqtt, self.web_host = cfg, mqtt, web_host
        self.listen_port = listen_port if listen_port is not None else cfg.listen_port
        self.hub = DeviceHub(cfg.allowed_macs)
        self.relay = Relay(self.hub, cfg.cloud_host) if cfg.cloud_relay else None
        self.web = WebApp(self.hub, cfg, self.relay)  # first: registers the shared AQI sampler
        self.supervisor_url = supervisor_url
        self.env = os.environ if env is None else env
        self.bridge = None
        self._runner = None
        self._mqtt_task: asyncio.Task | None = None
        self.web_port: int | None = None

    async def start(self) -> None:
        cfg = self.cfg
        Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
        if cfg.log_frames_to_file:
            self.hub.add_frame_observer(_frame_logger(cfg.data_dir))
        cert, key = ensure_cert(cfg.data_dir)
        await self.hub.start(cfg.listen_host, self.listen_port, build_ssl_context(cert, key))
        self._runner = await start_web(self.web, self.web_host, cfg.ingress_port)
        self.web_port = bound_port(self._runner)
        if self.use_mqtt:
            await resolve_mqtt(cfg, self.env, self.supervisor_url)
            if cfg.mqtt_source != "none":
                self.bridge = MqttBridge(self.hub, cfg)
                self.web.mqtt = self.bridge
        if self.bridge:
            self._mqtt_task = asyncio.create_task(self.bridge.run())

    async def stop(self) -> None:
        """Shut everything down, each step bounded so SIGTERM exits within a few seconds."""

        async def bounded(coro, t: float) -> None:
            try:
                await asyncio.wait_for(coro, t)
            except Exception as e:  # incl. TimeoutError
                log.warning("shutdown step timed out or failed: %r", e)

        if self._mqtt_task:
            # clean shutdown: bridge publishes retained offline topics, then disconnects
            self.bridge.request_stop()
            await asyncio.wait({self._mqtt_task}, timeout=2)
            if not self._mqtt_task.done():
                self._mqtt_task.cancel()
                await asyncio.wait({self._mqtt_task}, timeout=1)
        if self.relay:
            await bounded(self.relay.stop(), 2)
        self.web.close_clients()
        if self._runner:
            await bounded(self._runner.cleanup(), 3)
        await bounded(self.hub.stop(), 4)


async def amain(cfg) -> None:
    app = App(cfg)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    await app.start()
    log.info("coway-bridge up: purifiers :%s, web :%s", app.hub.port, app.web_port)
    try:
        waits = [asyncio.create_task(stop.wait())]
        if app._mqtt_task:
            waits.append(app._mqtt_task)
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        if app._mqtt_task and app._mqtt_task.done() and not stop.is_set():
            app._mqtt_task.result()  # surface crash
    finally:
        # last-resort watchdog: even interpreter/executor teardown cannot keep us alive
        wd = threading.Timer(SHUTDOWN_TIMEOUT + 3, os._exit, args=(1,))
        wd.daemon = True
        wd.start()
        try:
            await asyncio.wait_for(app.stop(), SHUTDOWN_TIMEOUT)
        except TimeoutError:
            log.error("shutdown exceeded %gs; exiting anyway", SHUTDOWN_TIMEOUT)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="coway-bridge")
    p.add_argument("--config", default="/data/options.json")
    p.add_argument("--data-dir")
    p.add_argument("--ingress-port", type=int)
    p.add_argument("--listen-port", type=int)
    p.add_argument("--mqtt-host")
    p.add_argument("--mqtt-port", type=int)
    p.add_argument("--log-level")
    a = p.parse_args(argv)
    overrides = {k: v for k, v in vars(a).items() if k != "config"}
    cfg = load_config(a.config, overrides=overrides)
    logging.basicConfig(level=cfg.log_level.upper(), format="%(asctime)s %(name)s %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(amain(cfg))
    logging.shutdown()
    os._exit(0)  # skip interpreter teardown: a stuck resolver thread must not block exit


if __name__ == "__main__":
    main()
