import json

from coway_bridge.config import load_config


def test_precedence(tmp_path):
    p = tmp_path / "options.json"
    p.write_text(
        json.dumps({"mqtt_host": "file", "mqtt_port": 1884, "allowed_macs": ["00:08:dc:00:00:01"]})
    )
    c = load_config(
        p,
        env={"COWAY_MQTT_HOST": "env", "COWAY_MQTT_TLS": "true"},
        overrides={"mqtt_port": 9, "log_level": None},
    )
    assert (c.mqtt_host, c.mqtt_port, c.mqtt_tls) == ("env", 9, True)
    assert c.allowed_macs == ["0008DC000001"]
    assert c.cloud_host == "iocgwo.coway.co.kr:9090"


def test_defaults_without_file(tmp_path):
    c = load_config(
        tmp_path / "none.json", env={"COWAY_ALLOWED_MACS": "aa:bb:cc:dd:ee:ff, 0008dc000001"}
    )
    assert c.allowed_macs == ["AABBCCDDEEFF", "0008DC000001"] and not c.cloud_relay
