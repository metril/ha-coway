# Changelog

## 0.1.1

- Fix: server certificate is now SHA1-signed (generated with the `openssl` CLI); the purifier's old TLS stack rejects SHA256-signed certs. Existing non-SHA1 certs are regenerated.
- TLS handshake failures and connection errors on the purifier listener are logged at WARNING with the peer IP; purifier connect/disconnect are logged at INFO.
- MQTT: refused subscriptions (e.g. not authorized) are logged as ERROR and trigger a reconnect with exponential backoff (max 60s); publishes use QoS 1 and refused publishes are logged once per topic.
- Fix: SIGTERM now shuts the app down within a few seconds.
