from pathlib import Path

import pytest

from coway_bridge import codec
from coway_bridge import registers as R

HDR, MAC = "010100000TST0000000001", "0008DC000001"
CORPUS = Path(__file__).parent / "fixtures" / "allframes.txt"


def corpus_frames():
    out = []
    for line in CORPUS.read_text().split():
        frames, rest = codec.split_frames(bytes.fromhex(line))
        assert rest == b""
        out += frames
    return out


def test_corpus_roundtrip_and_checksums():
    frames = corpus_frames()
    assert len(frames) >= 170
    for raw in frames:
        assert f"{sum(raw[:-3]) & 0xFF:02X}".encode() == raw[-3:-1]  # independent check
        f = codec.decode_frame(raw)
        assert f.encode() == raw
        assert (f.hdr, f.mac) == (HDR, MAC)


@pytest.mark.parametrize(
    "body",
    [
        "A1000QMV0.0.0.13",
        "A1011QG000100030002",
        "A1011QG000100010000",
        "A1011QG000100070002",
        "A1011QG000100080078",
        "A1011QG000200020001000A0002",
        "A1011SM",
        "A1000SG",
        "A1010SG",
        "A1020SG",
        "A1012QG",
        "A1010RM000C0001000100020000000300010007000200080000000A0001000E00010012000000180001"
        "001900010021000000220000",
        "A1020RM0004000200000007037E0011006400120064",
    ],
)
def test_golden_bodies(body):
    raw = codec.make_frame(HDR, MAC, body)
    assert raw[0] == 2 and raw[-1] == 3
    assert int(raw[-3 - len(body) - 4 : -3 - len(body)], 16) == len(body) + 3
    assert f"{sum(raw[:-3]) & 0xFF:02X}".encode() == raw[-3:-1]
    f = codec.decode_frame(raw)
    assert f.body == body and (f.code, f.verb) == (body[:5], body[5:7])


def test_len_example():
    assert codec.make_frame(HDR, MAC, "A1000SG")[-3 - 7 - 4 : -3 - 7] == b"000A"


def test_bad_checksum_and_framing():
    raw = bytearray(codec.make_frame(HDR, MAC, "A1010SG"))
    raw[-3] ^= 1
    with pytest.raises(codec.FrameError):
        codec.decode_frame(bytes(raw))
    with pytest.raises(codec.FrameError):
        codec.decode_frame(b"garbage")


def test_split_concatenated_and_partial():
    a, b = codec.make_frame(HDR, MAC, "A1010SG"), codec.make_frame(HDR, MAC, "A1020SG")
    frames, rest = codec.split_frames(a + b + b[:10])
    assert frames == [a, b] and rest == b[:10]


def test_regs_parse_build():
    body = "A1020RM0004000200000007037E0011006400120064"
    regs = codec.decode_frame(codec.make_frame(HDR, MAC, body)).regs()
    assert regs == {2: 0, 7: 0x37E, 0x11: 100, 0x12: 100}
    assert codec.build_regs(regs) == body[7:]
    assert codec.write_body({3: 2}) == "A1011QG000100030002"
    assert codec.write_body([(2, 1), (0xA, 2)]) == "A1011QG000200020001000A0002"
    with pytest.raises(codec.FrameError):
        codec.parse_regs("0002" + "00010001")


def test_register_decode():
    ctl = {1: 1, 2: 0, 3: 1, 7: 2, 8: 0x1E0, 0xA: 2}
    d = R.decode_control(ctl)
    assert d == {
        "power": True,
        "mode": "manual",
        "speed": 1,
        "light": True,
        "timer_min": 480,
        "sensitivity": "moderate",
    }
    assert R.decode_control({2: 1})["mode"] == "smart"
    assert R.decode_sensors({2: 0, 7: 894, 0x11: 100, 0x12: 100}) == {
        "pm10": 0,
        "lux": 894,
        "prefilter_used": 100,
        "hepa_used": 100,
    }


def test_setup_frames():
    assert codec.encode_setup("03", b"00000TST0000000001") == b"\x02170300000TST000000000199\x03"
    assert codec.encode_setup("04", b"\x00\x00") == b"\x020704\x00\x00CD\x03"
    assert codec.decode_setup(b"\x0205FEF2\x03") == ("FE", b"")
    dummy = codec.encode_setup("01", b"TestNet")
    assert codec.decode_setup(dummy) == ("01", b"TestNet")
    assert dummy[1:3] == b"0C"


def test_fixture_checksums_valid_and_anonymised():
    raw = CORPUS.read_text()
    assert ("2143" + "000104") not in bytes.fromhex("".join(raw.split())).decode("latin-1")
    n = 0
    for line in raw.split():
        data = bytes.fromhex(line)
        for fr in data.split(b"\x03")[:-1]:
            fr = fr + b"\x03"
            assert f"{sum(fr[:-3]) & 0xFF:02X}".encode() == fr[-3:-1]
            n += 1
    assert n > 50
