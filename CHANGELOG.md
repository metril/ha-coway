# Changelog

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
