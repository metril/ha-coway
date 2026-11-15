"""Config: /data/options.json (HA app options), overridable by env COWAY_<KEY> and CLI."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any


@dataclass
class Config:
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_tls: bool = False
    ingress_port: int = 8099
    listen_port: int = 9090  # remap the host port in the app's Network UI
    allowed_macs: list[str] = field(default_factory=list)
    cloud_relay: bool = False
    cloud_host: str = "iocgwo.coway.co.kr:9090"
    log_frames_to_file: bool = False
    log_level: str = "info"
    data_dir: str = "/data"
    listen_host: str = "0.0.0.0"

    def __post_init__(self) -> None:
        self.allowed_macs = [normalize_mac(m) for m in self.allowed_macs if m]


def normalize_mac(mac: str) -> str:
    return mac.replace(":", "").replace("-", "").upper()


def _coerce(name: str, value: Any, default: Any) -> Any:
    if isinstance(default, bool):
        return (
            value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
        )
    if isinstance(default, int):
        return int(value)
    if isinstance(default, list):
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return list(value)
    return str(value)


def load_config(
    path: str | os.PathLike | None = "/data/options.json",
    env: dict[str, str] | None = None,
    overrides: dict[str, Any] | None = None,
) -> Config:
    """Precedence: overrides (CLI) > env (COWAY_<KEY>) > options file > defaults."""
    env = os.environ if env is None else env
    defaults = Config()
    values: dict[str, Any] = {}
    if path and Path(path).is_file():
        values.update(json.loads(Path(path).read_text()))
    known = {f.name for f in fields(Config)}
    for k in known:
        ev = env.get(f"COWAY_{k.upper()}")
        if ev is not None:
            values[k] = ev
    values.update({k: v for k, v in (overrides or {}).items() if v is not None})
    kwargs = {k: _coerce(k, v, getattr(defaults, k)) for k, v in values.items() if k in known}
    return Config(**kwargs)
