# Changelog

## 0.2.9
- The HA fan now uses the purifier's real 3 speeds (`speed_range_min` 1, `speed_range_max` 3): HA shows 33 / 67 / 100 % in 3 steps instead of a 1-100 % slider. The bridge publishes and accepts the raw speed 1-3 on the percentage topics (0 = off); `fan.set_percentage` with 33 / 66 / 100 keeps working.

## 0.2.8
- Home Assistant values are properly capitalized: fan presets `Smart` / `Bedtime` / `Manual`, Bedtime timer `Off` / `1 hour` / `2 hours` / `4 hours` / `8 hours`, Smart sensitivity `Low` / `Medium` / `High` (matching the web UI). Commands still accept the old lowercase values, but HA itself only offers the new options, so update automations that set a preset, timer or sensitivity by name.

## 0.2.7
- The purifier light is now a Home Assistant `light` entity (was a `switch`). Update any automations that referenced `switch.airmega_400s_light` to the new `light.` entity.

## 0.2.6
- Protocol correction, based on a real purifier-to-cloud capture: the purifier pushes sensor reports (`A1020RM`, about every 60 s) and settings reports (`A1010RM`, on change) by itself, and the cloud only acknowledges each with the matching `SG`. `SG` is an ack, not a poll, and the cloud is otherwise silent (minutes of silence are normal).
- Fix: with the cloud relay, cloud silence no longer counts as a stalled upstream. Previously 30 s without a cloud frame made the bridge resume local polling alongside the cloud. Now only socket errors, EOF or a stalled writer mean upstream is down.
- Fix: on every upstream (re)connect the cloud first receives the purifier's original hello (`A1000QM`), then any buffered frames.
- With upstream up the bridge sends nothing to the purifier except your commands and requested snapshots (no local acks, no initial snapshot). If upstream goes down it acks the purifier's reports itself (like the cloud) immediately, and stops when upstream returns.
- Without the relay (or while upstream is down) the 10 s poll loop is gone: each purifier hello/report is acknowledged once, plus one initial snapshot on connect. The purifier-offline timeout is now 150 s (it pushes every 60 s).
- Web UI: `SG` frames are titled "Acknowledged" (e.g. "ack of sensor report"); poll wording removed.

## 0.2.5
- Fix: the web UI Cloud and MQTT pills and the Connections card now update immediately when the Coway cloud link or MQTT connection changes (new `connections` live message), instead of staying stale until another full refresh.
- Cloud pill shows a neutral "Cloud: on · waiting for purifier" when forwarding is enabled but no purifier is connected; yellow "Cloud: connecting" is only for an actual connection attempt.

## 0.2.4
- Fix: sensitivity register 000A mapping was reversed. Correct: 1 = sensitive (High), 2 = moderate (Medium), 3 = insensitive (Low). Existing HA automations that set the sensitivity select by name now write the right value.
- The off timer only works in Bedtime mode: the web UI shows the Off timer row only in Bedtime, and the HA select is renamed "Bedtime timer" and is unavailable unless the purifier is on and in Bedtime mode. Entity keys and unique IDs are unchanged. HA "Sensitivity" is renamed "Smart sensitivity" (settable in any mode).
- Timer commands in the wrong mode are rejected: web API returns 409 with a message; MQTT logs a warning, ignores the command and republishes state so HA reverts.
- Fix: a slow, unreachable, hanging or flapping Coway cloud can no longer affect the local bridge. Purifier frames are forwarded upstream through a bounded queue (oldest dropped with a warning) written by a separate task with a write timeout, so a stalled upstream never blocks the purifier session, commands, acks, MQTT or polling. Connect/TLS handshake are time-limited, relay errors never reach the session, and local polling resumes immediately whenever upstream is down, stalled or silent for 30 s.
- Web UI: Diagnostics message log rows use a fixed-column grid (time, direction, title, summary, code) so columns and vertical alignment stay consistent; compact on narrow screens.

## 0.2.3
- Unknown registers (`reg_XXXX` / `sreg_XXXX`) are no longer published to HA; they stay in the web UI register table and message log.
- New optional `mqtt_base_topic` (default `coway_bridge`, unchanged from 0.2.2).
- Entity keys renamed: `lux` to `light_sensor`, `prefilter` to `pre_filter`, `hepa` to `max2_filter` (discovery topics, unique IDs, state JSON).
- HA identifiers renamed from `coway_<mac>` to `airmega_400s_<mac>` (discovery node, unique IDs, device identifiers).
- **Upgrade note:** the base topic is unchanged, but discovery topics, unique IDs and entity keys changed. Delete the old Airmega device in HA (Settings, Devices & services, MQTT, INFO, three-dot menu, Delete) before or after updating. Old retained discovery under `homeassistant/*/coway_<mac>/` is cleared by deleting the device. Automations and dashboards must be re-pointed to the new entities.

## 0.2.2
- Fan entity gets an air-purifier icon.
- Availability robustness on shared brokers: retained `status` and per-purifier `online` are re-published every 5 minutes; shutdown marks offline only purifiers connected to this bridge, so one bridge stopping no longer marks another's devices offline.

## 0.2.1
- MQTT broker auto-discovery via the HA Supervisor (`mqtt:want`): leave `mqtt_host` empty to use the broker from an installed MQTT app. Explicit options still win.
- No broker configured: clear error in the log and UI; the purifier server and web UI keep running.

## 0.2.0
- Redesigned web UI: Device, Diagnostics and Setup tabs; light/dark; mobile layout; tooltips; plain-language message log.
- AQI (US EPA, from 5-minute mean PM10) in the UI and as an HA sensor.
- New commands: mode, timer, sensitivity (shared mapping for UI and MQTT).
- HA device renamed "Airmega 400S"; entity IDs derived by HA (e.g. sensor.airmega_400s_pm10); "Light sensor" and "Max2 filter" names.
- Connections card: MQTT and Coway cloud forwarding status.
- Discovery publish logged at INFO.

## 0.1.1

- Fix: server certificate is now SHA1-signed (generated with the `openssl` CLI); the purifier's old TLS stack rejects SHA256-signed certs. Existing non-SHA1 certs are regenerated.
- TLS handshake failures and connection errors on the purifier listener are logged at WARNING with the peer IP; purifier connect/disconnect are logged at INFO.
- MQTT: refused subscriptions (e.g. not authorized) are logged as ERROR and trigger a reconnect with exponential backoff (max 60s); publishes use QoS 1 and refused publishes are logged once per topic.
- Fix: SIGTERM now shuts the app down within a few seconds.
