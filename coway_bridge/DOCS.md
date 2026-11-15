# Coway Bridge

Local control for the Coway Airmega 400S (AP-2015E). The purifier normally talks TLS 1.0 to the Coway cloud on TCP 9090; you redirect that traffic to this app instead. Entities appear in HA via MQTT discovery.

## Install
1. HA Settings, Apps, Repositories: add `https://github.com/metril/ha-coway`.
2. Install **Coway Bridge**, set the options (`mqtt_host` is required), start it. Needs an MQTT broker you configure explicitly (EMQX, Mosquitto, ...).
3. Open the **Coway Bridge** sidebar panel (debug UI).

## Options
| Option | Default | Notes |
|---|---|---|
| `mqtt_host` / `mqtt_port` | required / 1883 | Broker (e.g. your EMQX); credentials below |
| `mqtt_username` / `mqtt_password` | empty | Broker credentials |
| `mqtt_tls` | false | TLS to broker |
| `allowed_macs` | `[]` | Only these purifier MACs are accepted; empty accepts any. Use `0008DC000001` or `00:08:DC:00:00:01` form |
| `cloud_relay` | false | Also connect upstream to the Coway cloud (see below) |
| `cloud_host` | `iocgwo.coway.co.kr:9090` | Upstream `host:port`, resolved at each connect |
| `log_frames_to_file` | false | Rotated `frames.log` in the app data dir (hex frames) |
| `log_level` | info | |

The debug UI is served on ingress port 8099 (not exposed on the host; under HAOS only the Supervisor ingress proxy may reach it). The purifier port is always 9090 inside the container; to use another host port, change it in the app's Network UI.

## Network (pfSense example)
The purifier must reach this app on TCP 9090 while believing it talks to the cloud.
1. **NAT port forward** (Firewall, NAT, Port Forward): interface = IoT VLAN, protocol TCP, source = purifier IP, destination = `iocgwo.coway.co.kr` (alias of its IPs) port 9090, redirect target = HAOS IP port 9090. Match only traffic from the purifier so the app's own relay connection is not looped back.
2. **Filter rule** on the IoT VLAN: pass TCP from purifier to HAOS IP:9090. Restrict the source to the IoT VLAN/purifier; never expose 9090 elsewhere. Set `allowed_macs` as well.
3. Keep the purifier's other traffic as you prefer (it can be blocked from the internet if relay is off).

## Wi-Fi setup
To (re)provision the purifier, put it in pairing mode (hold the Wi-Fi button until the LED blinks), join the open network `Airmega_400s` on a laptop, and run:

    curl -O https://raw.githubusercontent.com/metril/ha-coway/main/coway_bridge/app/coway_bridge/setup_cli.py
    python3 setup_cli.py --serial YOUR_SERIAL --ssid 'My SSID'

`--serial` is the serial number on the unit's label (also shown in the debug UI Status card once a unit has connected). The CLI prompts for the Wi-Fi password (or pass `--password`). Alternatively: `pip install "git+https://github.com/metril/ha-coway"` and run `coway-setup` with the same arguments. The debug UI's Setup card generates the command (without the password). Then rejoin your normal Wi-Fi.

## Cloud relay
With `cloud_relay: true` the app opens a TLS connection per purifier to `cloud_host`, forwards frames both ways (so IoCare keeps working), still decodes everything for HA, and stops its own polling. HA commands are injected as `A1011QG`/`A1012QG` frames and only the acks matching those local commands are swallowed so the cloud never sees them. Purifier frames received before upstream connects are buffered (up to 50) and flushed on connect. If upstream drops, local polling resumes and the app retries with exponential backoff (max 5 min). No IP is hardcoded; name resolution uses the HAOS resolver.

## Discovering new registers
Unknown registers show up as disabled diagnostic sensors `reg_XXXX` and are highlighted (`!`) in the debug UI. To find a command register (e.g. filter reset): enable relay mode, trigger the action in the IoCare app, then find the cloud-to-purifier (`C→P`) `A1011QG` frame in the UI Frame log. Use **Download** to save a capture (`allframes.txt` format, one hex frame per line) for analysis.
