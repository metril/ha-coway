"""Frame codec for the Coway AP-2015E protocol (port 9090) -- pure functions.

Frame: STX HDR(22) MAC(12) FILL(16) LEN(4 hex) BODY CKSUM(2 hex) ETX
  LEN   = len(BODY) + 3
  CKSUM = sum(bytes STX..last BODY byte) & 0xFF, uppercase hex.
BODY = CODE(5) VERB(2) DATA. RM/SM/QG data = COUNT(4 hex) + COUNT * (REG(4) VAL(4)).
"""

from __future__ import annotations

from dataclasses import dataclass

STX = 0x02
ETX = 0x03
FILL = "F" * 16
HDR_LEN, MAC_LEN, FILL_LEN, LEN_LEN = 22, 12, 16, 4
_PREFIX = HDR_LEN + MAC_LEN + FILL_LEN + LEN_LEN


class FrameError(ValueError):
    """Malformed frame or bad checksum."""


def checksum(data: bytes) -> str:
    """Checksum over `data` (everything from STX through the last BODY byte)."""
    return f"{sum(data) & 0xFF:02X}"


@dataclass(frozen=True)
class Frame:
    hdr: str
    mac: str
    body: str
    fill: str = FILL

    @property
    def code(self) -> str:
        return self.body[:5]

    @property
    def verb(self) -> str:
        return self.body[5:7]

    @property
    def data(self) -> str:
        return self.body[7:]

    def encode(self) -> bytes:
        return encode_frame(self)

    def regs(self) -> dict[int, int]:
        return parse_regs(self.data)


def encode_frame(frame: Frame) -> bytes:
    head = bytes([STX]) + (
        frame.hdr + frame.mac + frame.fill + f"{len(frame.body) + 3:04X}" + frame.body
    ).encode("ascii")
    return head + checksum(head).encode("ascii") + bytes([ETX])


def decode_frame(raw: bytes) -> Frame:
    if len(raw) < _PREFIX + 7 + 3 or raw[0] != STX or raw[-1] != ETX:
        raise FrameError("bad framing")
    try:
        text = raw[1:-1].decode("ascii")
    except UnicodeDecodeError as e:
        raise FrameError("non-ascii") from e
    ck = text[-2:]
    inner = raw[:-3]
    if checksum(inner) != ck.upper():
        raise FrameError(f"checksum mismatch: got {ck}, want {checksum(inner)}")
    try:
        n = int(text[_PREFIX - LEN_LEN : _PREFIX], 16)
    except ValueError as e:
        raise FrameError("bad length") from e
    body = text[_PREFIX:-2]
    if n != len(body) + 3:
        raise FrameError(f"length mismatch: {n} vs {len(body) + 3}")
    o = HDR_LEN
    return Frame(
        hdr=text[:o],
        mac=text[o : o + MAC_LEN],
        fill=text[o + MAC_LEN : o + MAC_LEN + FILL_LEN],
        body=body,
    )


def split_frames(buf: bytes) -> tuple[list[bytes], bytes]:
    """Split a byte buffer on ETX. Returns (complete frames, remainder)."""
    frames: list[bytes] = []
    while True:
        i = buf.find(bytes([ETX]))
        if i < 0:
            return frames, buf
        chunk, buf = buf[: i + 1], buf[i + 1 :]
        s = chunk.find(bytes([STX]))
        if s >= 0:
            frames.append(chunk[s:])


def parse_regs(data: str) -> dict[int, int]:
    """Parse COUNT + COUNT*(REG,VAL) hex data."""
    if len(data) < 4:
        raise FrameError("short reg data")
    count = int(data[:4], 16)
    if len(data) != 4 + count * 8:
        raise FrameError("reg count mismatch")
    return {
        int(data[4 + i * 8 : 8 + i * 8], 16): int(data[8 + i * 8 : 12 + i * 8], 16)
        for i in range(count)
    }


def build_regs(pairs: dict[int, int] | list[tuple[int, int]]) -> str:
    items = list(pairs.items()) if isinstance(pairs, dict) else list(pairs)
    return f"{len(items):04X}" + "".join(f"{r:04X}{v:04X}" for r, v in items)


def make_frame(hdr: str, mac: str, body: str) -> bytes:
    return encode_frame(Frame(hdr=hdr, mac=mac, body=body))


def poll_body(code: str) -> str:
    return f"{code}SG"


def write_body(pairs: dict[int, int] | list[tuple[int, int]]) -> str:
    return "A1011QG" + build_regs(pairs)


# --- setup (port 5000) frames: STX LEN(2) CMD(2) DATA CKSUM(2) ETX -------------------------


def encode_setup(cmd: str, data: bytes = b"") -> bytes:
    inner = cmd.encode("ascii") + data
    head = bytes([STX]) + f"{len(inner) + 3:02X}".encode("ascii") + inner
    return head + checksum(head).encode("ascii") + bytes([ETX])


def decode_setup(raw: bytes) -> tuple[str, bytes]:
    if len(raw) < 7 or raw[0] != STX or raw[-1] != ETX:
        raise FrameError("bad setup framing")
    if checksum(raw[:-3]) != raw[-3:-1].decode("ascii", "replace").upper():
        raise FrameError("setup checksum mismatch")
    n = int(raw[1:3], 16)
    if n != len(raw[3:-3]) + 3:
        raise FrameError("setup length mismatch")
    return raw[3:5].decode("ascii"), raw[5:-3]
