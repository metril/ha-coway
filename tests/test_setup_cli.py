import socket
import threading

from coway_bridge import codec, setup_cli

SERIAL = "00000TST0000000001"


def test_frames_byte_exact():
    f = setup_cli.build_sequence("TestNet", "dummy-pass", SERIAL)
    assert f[2] == b"\x02170300000TST000000000199\x03"
    assert f[3] == b"\x020704\x00\x00CD\x03"
    for fr, (cmd, data) in zip(
        f,
        [("01", b"TestNet"), ("02", b"dummy-pass"), ("03", None), ("04", b"\x00\x00")],
        strict=True,
    ):
        c, d = codec.decode_setup(fr)  # independent codec agrees
        assert c == cmd and (data is None or d == data)
        assert f"{sum(fr[:-3]) & 0xFF:02X}".encode() == fr[-3:-1]


def fake_ap(drop_first_ack=False):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got = []

    def run():
        conn, _ = srv.accept()
        with conn:
            n = 0
            while len(got) < 4:
                data = conn.recv(256)
                if not data:
                    break
                n += 1
                if drop_first_ack and n == 1:
                    continue
                got.append(data)
                conn.sendall(b"\x0205FEF2\x03")
        srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return srv.getsockname()[1], got, t


def test_provision_flow():
    port, got, t = fake_ap()
    assert (
        setup_cli.main(
            [
                "--serial",
                SERIAL,
                "--ssid",
                "TestNet",
                "--password",
                "pw",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--timeout",
                "2",
            ]
        )
        == 0
    )
    t.join(2)
    assert got == setup_cli.build_sequence("TestNet", "pw", SERIAL)


def test_retry_on_missing_ack():
    port, got, t = fake_ap(drop_first_ack=True)
    assert setup_cli.provision(
        "TestNet", "pw", SERIAL, "127.0.0.1", port, timeout=0.5, log=lambda *_: 0
    )
    t.join(2)


def test_connection_refused():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert (
        setup_cli.main(
            [
                "--serial",
                SERIAL,
                "--ssid",
                "a",
                "--password",
                "b",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--timeout",
                "1",
            ]
        )
        == 2
    )


def test_password_prompted_when_absent(monkeypatch):
    port, got, t = fake_ap()
    monkeypatch.setattr(setup_cli.getpass, "getpass", lambda *_: "prompted")
    argv = ["--serial", SERIAL, "--ssid", "TestNet", "--host", "127.0.0.1", "--port", str(port)]
    assert setup_cli.main(argv) == 0
    t.join(2)
    assert got == setup_cli.build_sequence("TestNet", "prompted", SERIAL)


def test_serial_required():
    import pytest

    with pytest.raises(SystemExit):
        setup_cli.main(["--ssid", "a"])
