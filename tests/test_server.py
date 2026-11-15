import asyncio
import ssl

import pytest
from coway_bridge.config import Config
from coway_bridge.mqtt_bridge import MqttBridge
from coway_bridge.server import DeviceHub, build_ssl_context, ensure_cert

from coway_bridge import codec

HDR, MAC = "010100000TST0000000001", "0008DC000001"
CTL = "A1010RM000C0001000100020000000300010007000200080000000A0001000E00010012000000180001001900010021000000220000"
SEN = "A1020RM0004000200000007037E0011006400120064"


def client_ctx():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1
    ctx.maximum_version = ssl.TLSVersion.TLSv1
    ctx.set_ciphers("AES256-SHA:@SECLEVEL=0")
    return ctx


class Purifier:
    def __init__(self, r, w):
        self.r, self.w, self.buf = r, w, b""

    @classmethod
    async def connect(cls, port, mac=MAC):
        r, w = await asyncio.open_connection("127.0.0.1", port, ssl=client_ctx())
        p = cls(r, w)
        p.mac = mac
        return p

    def send(self, body):
        self.w.write(codec.make_frame(HDR, self.mac, body))

    async def recv(self, timeout=3):
        while True:
            fr, self.buf = codec.split_frames(self.buf)
            if fr:
                self.buf = b"".join(fr[1:]) + self.buf
                return codec.decode_frame(fr[0])
            d = await asyncio.wait_for(self.r.read(4096), timeout)
            if not d:
                return None
            self.buf += d

    async def recv_until(self, body):
        for _ in range(20):
            f = await self.recv()
            if f is None or f.body == body:
                return f
        raise AssertionError(body)


@pytest.fixture
async def hub(tmp_path):
    cert, key = ensure_cert(tmp_path)
    h = DeviceHub(poll_interval=0.2, silence_timeout=5)
    await h.start("127.0.0.1", 0, build_ssl_context(cert, key))
    yield h
    await h.stop()


async def wait_for(cond, t=3):
    for _ in range(int(t / 0.02)):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timeout")


def test_cert_subject(tmp_path):
    from cryptography import x509

    cert, _ = ensure_cert(tmp_path)
    c = x509.load_pem_x509_certificate(open(cert, "rb").read())
    assert "CN=Coway_iTrust" in c.subject.rfc4514_string()
    assert c.signature_hash_algorithm.name == "sha1"
    assert ensure_cert(tmp_path)[0] == cert


def test_cert_is_sha1_and_regenerated_if_not(tmp_path, caplog):
    import datetime as dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    cert, key = ensure_cert(tmp_path)
    c = x509.load_pem_x509_certificate(open(cert, "rb").read())
    assert c.signature_algorithm_oid._name == "sha1WithRSAEncryption"
    assert isinstance(c.signature_hash_algorithm, hashes.SHA1)
    # replace with a SHA256 cert -> must be regenerated
    k = rsa.generate_private_key(65537, 2048)
    n = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "x")])
    now = dt.datetime.now(dt.UTC)
    c2 = (
        x509.CertificateBuilder()
        .subject_name(n)
        .issuer_name(n)
        .public_key(k.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(k, hashes.SHA256())
    )
    open(cert, "wb").write(c2.public_bytes(serialization.Encoding.PEM))
    ensure_cert(tmp_path)
    c3 = x509.load_pem_x509_certificate(open(cert, "rb").read())
    assert isinstance(c3.signature_hash_algorithm, hashes.SHA1)
    assert "not SHA1" in caplog.text
    assert c3.signature_algorithm_oid._name == "sha1WithRSAEncryption"
    assert c3.subject.rfc4514_string() == (
        "CN=Coway_iTrust,OU=SW Dev Unit,O=COWAY,L=Seoul,ST=Seoul,C=KR"
    )


async def test_session_lifecycle_logged_at_info(hub, caplog):
    import logging

    caplog.set_level(logging.INFO)
    p = await Purifier.connect(hub.port)
    p.send("A1000QMV0.0.0.13")
    await wait_for(lambda: hub.device_state(MAC)["firmware"] == "0.0.0.13")
    p.w.close()
    await wait_for(lambda: not hub.device_state(MAC)["online"])
    await asyncio.sleep(0.05)
    text = caplog.text
    assert "purifier connected" in text and MAC in text and "127.0.0.1" in text
    assert f"purifier {MAC} is now online" in text and f"purifier {MAC} is now offline" in text
    assert "purifier disconnected" in text and "0.0.0.13" in text


async def test_session_flow(hub):
    events = []
    hub.add_online_callback(lambda mac, on, fw: events.append(("online", mac, on, fw)))
    hub.add_state_callback(lambda mac, st: events.append(("state", mac)))
    frames = []
    hub.add_frame_observer(lambda mac, d, raw, dec: frames.append((d, dec.code if dec else None)))
    p = await Purifier.connect(hub.port)
    assert p.w.get_extra_info("ssl_object").version() == "TLSv1"
    p.send("A1000QMV0.0.0.13")
    p.send(CTL)
    p.send(SEN)
    seen = set()
    for _ in range(12):  # polls repeat every 0.2s
        f = await p.recv()
        seen.add(f.body)
        if {"A1000SG", "A1010SG", "A1020SG", "A1012QG"} <= seen:
            break
    assert {"A1000SG", "A1010SG", "A1020SG", "A1012QG"} <= seen
    assert f.hdr == HDR and f.mac == MAC  # echoed
    await wait_for(lambda: hub.device_state(MAC)["sensor"].get(7) == 894)
    st = hub.device_state(MAC)
    assert st["online"] and st["firmware"] == "0.0.0.13"
    assert st["control"][1] == 1 and st["control"][0xE] == 1
    assert ("online", MAC, True, None) in events and ("state", MAC) in events
    assert ("P>A", "A1010") in frames and ("A>P", "A1000") in frames

    # write
    assert await hub.write(MAC, {3: 2})
    f = await p.recv_until("A1011QG000100030002")
    assert f.body == "A1011QG000100030002"
    assert await hub.request_snapshot(MAC)
    await p.recv_until("A1012QG")
    # snapshot report
    p.send("A1012SM0001000A0001")
    await wait_for(lambda: hub.device_state(MAC)["snapshot"].get(0xA) == 1)

    # suppress polls
    hub.session(MAC).suppress_polls = True
    await asyncio.sleep(0.3)
    with pytest.raises(TimeoutError):  # drain in-flight polls, then silence
        while True:
            await asyncio.wait_for(p.r.read(4096), 0.5)

    p.w.close()
    await wait_for(lambda: not hub.device_state(MAC)["online"])
    assert events[-1][:3] == ("online", MAC, False)
    assert not await hub.write(MAC, {1: 0})  # dropped while offline


async def test_replace_session(hub):
    online = []
    hub.add_online_callback(lambda mac, on, fw: online.append(on))
    a = await Purifier.connect(hub.port)
    a.send("A1000QMV0.0.0.13")
    await wait_for(lambda: hub.session(MAC) is not None)
    first = hub.session(MAC)
    b = await Purifier.connect(hub.port)
    b.send("A1000QMV0.0.0.13")
    await wait_for(lambda: hub.session(MAC) not in (None, first))
    while await a.recv() is not None:  # old connection gets closed
        pass
    assert hub.device_state(MAC)["online"]  # not marked offline by the replaced session
    assert False not in online


async def test_allowed_macs(tmp_path):
    cert, key = ensure_cert(tmp_path)
    h = DeviceHub(allowed_macs=["0008DC0000FF"], poll_interval=0.2)
    await h.start("127.0.0.1", 0, build_ssl_context(cert, key))
    try:
        p = await Purifier.connect(h.port)
        p.send("A1000QMV0.0.0.13")
        assert await p.recv() is None
        assert h.session(MAC) is None
    finally:
        await h.stop()


async def test_silence_timeout(tmp_path):
    cert, key = ensure_cert(tmp_path)
    h = DeviceHub(poll_interval=10, silence_timeout=0.3)
    await h.start("127.0.0.1", 0, build_ssl_context(cert, key))
    try:
        p = await Purifier.connect(h.port)
        p.send("A1000QMV0.0.0.13")
        await wait_for(lambda: h.device_state(MAC)["online"])
        await wait_for(lambda: not h.device_state(MAC)["online"])
    finally:
        await h.stop()


async def test_mqtt_bridge_publishes_and_commands(hub):
    b = MqttBridge(hub, Config())
    p = await Purifier.connect(hub.port)
    p.send("A1000QMV0.0.0.13")
    p.send(CTL)
    await wait_for(lambda: hub.device_state(MAC)["control"].get(1) == 1)
    topics = []
    while not b._q.empty():
        topics.append(b._q.get_nowait()[0])
    assert f"coway_bridge/{MAC.lower()}/state" in topics
    assert f"coway_bridge/{MAC.lower()}/online" in topics
    await b.handle_command(f"coway_bridge/{MAC.lower()}/percentage/set", "100")
    assert (await p.recv_until("A1011QG000100030003")).body.endswith("0003")


async def _connect_from(port, ip):
    r, w = await asyncio.open_connection("127.0.0.1", port, ssl=client_ctx(), local_addr=(ip, 0))
    p = Purifier(r, w)
    p.mac = MAC
    return p


async def test_live_session_from_other_ip_refused(hub, caplog):
    p1 = await Purifier.connect(hub.port)
    p1.send("A1000QMV0.0.0.13")
    await wait_for(lambda: hub.session(MAC) is not None)
    first = hub.session(MAC)
    p2 = await _connect_from(hub.port, "127.0.0.2")
    p2.send("A1000QMV0.0.0.13")
    assert await p2.recv(timeout=3) is None  # newcomer closed
    assert hub.session(MAC) is first and first.online
    assert "refusing" in caplog.text and "127.0.0.2" in caplog.text and "127.0.0.1" in caplog.text


async def test_stale_or_same_ip_session_replaced(hub):
    p1 = await Purifier.connect(hub.port)
    p1.send("A1000QMV0.0.0.13")
    await wait_for(lambda: hub.session(MAC) is not None)
    first = hub.session(MAC)
    p2 = await Purifier.connect(hub.port)  # same IP: replace
    p2.send("A1000QMV0.0.0.13")
    await wait_for(lambda: hub.session(MAC) is not first and not first.online)
    second = hub.session(MAC)
    second.last_frame -= 60  # stale
    p3 = await _connect_from(hub.port, "127.0.0.2")  # other IP but old is stale: replace
    p3.send("A1000QMV0.0.0.13")
    await wait_for(lambda: hub.session(MAC) is not second and not second.online)
    assert hub.session(MAC).serial == "00000TST0000000001"


async def test_tls_handshake_failure_logged_with_peer_ip(hub, caplog):
    r, w = await asyncio.open_connection("127.0.0.1", hub.port)
    w.write(b"not tls at all\n" * 4)
    await w.drain()
    await wait_for(lambda: "TLS handshake with 127.0.0.1 failed" in caplog.text)
    assert any(r.levelname == "WARNING" for r in caplog.records if "handshake" in r.getMessage())
    w.close()


async def test_session_connect_disconnect_logged(hub, caplog):
    import logging

    caplog.set_level(logging.INFO)
    p = await Purifier.connect(hub.port)
    p.send("A1000QMV0.0.0.13")
    await p.w.drain()
    await wait_for(lambda: "purifier connected: ip=127.0.0.1" in caplog.text)
    assert MAC in caplog.text
    p.w.close()
    await wait_for(lambda: "purifier disconnected" in caplog.text)
    assert "reason=closed by peer" in caplog.text
