# ha-coway

Local control of the Coway Airmega 400S (AP-2015E) for Home Assistant. The purifier's cloud connection is redirected to this bridge, which speaks the device protocol (TLS 1.0, STX/ETX frames on port 9090), publishes state and accepts commands over MQTT discovery, and ships a debug web UI.

- Home Assistant app (add-on): `coway_bridge/` — see [DOCS](coway_bridge/DOCS.md)
- Optional cloud relay so the IoCare app keeps working
- Debug UI (ingress panel) with live frame log and register table
- `tools/`: reverse-engineering scripts

Wi-Fi provisioning CLI (standalone, stdlib only): `curl -O https://raw.githubusercontent.com/metril/ha-coway/main/coway_bridge/app/coway_bridge/setup_cli.py && python3 setup_cli.py --serial <label serial> --ssid <name>` (or `pip install "git+https://github.com/metril/ha-coway"`).

Add `https://github.com/metril/ha-coway` as an app repository in HA, install **Coway Bridge**, and follow DOCS.md.

Development: `uv venv && uv pip install -e ".[dev]" && pytest -n auto -q && ruff check .`

License: MIT
