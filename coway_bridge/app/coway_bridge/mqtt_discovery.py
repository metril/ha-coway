"""MQTT broker resolution: explicit options, else HA Supervisor service discovery."""

from __future__ import annotations

import logging

from aiohttp import ClientSession, ClientTimeout

log = logging.getLogger(__name__)

NO_BROKER = (
    "MQTT: no broker configured. Set mqtt_host (and port/username/password/mqtt_tls) in the "
    "app Configuration, or install an MQTT app that provides the service."
)


async def fetch_supervisor_mqtt(base_url: str, token: str) -> dict | None:
    try:
        async with ClientSession(timeout=ClientTimeout(total=10)) as s:
            async with s.get(
                f"{base_url.rstrip('/')}/services/mqtt",
                headers={"Authorization": f"Bearer {token}"},
            ) as r:
                if r.status != 200:
                    log.debug("supervisor mqtt service: HTTP %s", r.status)
                    return None
                j = await r.json(content_type=None)
    except Exception as e:
        log.debug("supervisor mqtt lookup failed: %r", e)
        return None
    if not isinstance(j, dict) or j.get("result") != "ok" or not j.get("data", {}).get("host"):
        return None
    return j["data"]


def _finalize(cfg) -> None:
    if cfg.mqtt_tls is None:
        cfg.mqtt_tls = False
    if cfg.mqtt_port is None:
        cfg.mqtt_port = 8883 if cfg.mqtt_tls else 1883


async def resolve_mqtt(cfg, env, supervisor_url: str = "http://supervisor") -> None:
    """Fill cfg.mqtt_* and set cfg.mqtt_source ('options'|'supervisor'|'none')."""
    if cfg.mqtt_host:
        cfg.mqtt_source = "options"
        _finalize(cfg)
        return
    token = env.get("SUPERVISOR_TOKEN")
    data = await fetch_supervisor_mqtt(supervisor_url, token) if token else None
    if not data:
        cfg.mqtt_source, cfg.mqtt_error = "none", NO_BROKER
        log.error(NO_BROKER)
        return
    cfg.mqtt_source = "supervisor"
    cfg.mqtt_host = str(data["host"])
    # only keys the user actually set override discovered values
    if cfg.mqtt_port is None and data.get("port"):
        cfg.mqtt_port = int(data["port"])
    if not cfg.mqtt_username:
        cfg.mqtt_username = data.get("username") or ""
    if not cfg.mqtt_password:
        cfg.mqtt_password = data.get("password") or ""
    if cfg.mqtt_tls is None:
        cfg.mqtt_tls = bool(data.get("ssl"))
    _finalize(cfg)
    log.info(
        "MQTT: using broker from Home Assistant: %s:%s (tls %s, user %s)",
        cfg.mqtt_host,
        cfg.mqtt_port,
        "on" if cfg.mqtt_tls else "off",
        cfg.mqtt_username or "none",
    )
