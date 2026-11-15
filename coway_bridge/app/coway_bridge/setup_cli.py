"""Wi-Fi provisioning for the purifier's open AP (plain TCP :5000). Stdlib only.

Standalone: curl -O https://raw.githubusercontent.com/metril/ha-coway/main/coway_bridge/app/coway_bridge/setup_cli.py
Join the purifier's AP, then: python3 setup_cli.py --serial SERIAL --ssid NAME [--password PW]
SERIAL is the serial number on the unit's label (also shown in the bridge debug UI once a unit has
connected). The password is prompted for when --password is not given.
Frames: STX LEN(2 hex = len(CMD+DATA)+3) CMD(2) DATA CKSUM(2) ETX; device acks with CMD FE.
"""

from __future__ import annotations

import argparse
import getpass
import socket
import sys

STX, ETX = 0x02, 0x03
DEFAULT_HOST, DEFAULT_PORT = "192.168.12.101", 5000


def _cksum(b: bytes) -> bytes:
    return f"{sum(b) & 0xFF:02X}".encode()


def build_frame(cmd: str, data: bytes = b"") -> bytes:
    inner = cmd.encode() + data
    head = bytes([STX]) + f"{len(inner) + 3:02X}".encode() + inner
    return head + _cksum(head) + bytes([ETX])


def build_sequence(ssid: str, password: str, serial: str) -> list[bytes]:
    return [
        build_frame("01", ssid.encode()),
        build_frame("02", password.encode()),
        build_frame("03", serial.encode()),
        build_frame("04", b"\x00\x00"),
    ]


def is_ack(frame: bytes) -> bool:
    return (
        len(frame) >= 7
        and frame[0] == STX
        and frame[-1] == ETX
        and frame[3:5] == b"FE"
        and _cksum(frame[:-3]) == frame[-3:-1]
    )


def _read_frame(sock: socket.socket) -> bytes:
    buf = b""
    while ETX not in buf:
        chunk = sock.recv(256)
        if not chunk:
            raise ConnectionError("closed by device")
        buf += chunk
    return buf[: buf.index(ETX) + 1]


def provision(
    ssid: str,
    password: str,
    serial: str,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    timeout: float = 5.0,
    retries: int = 3,
    log=print,
) -> bool:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        for n, frame in enumerate(build_sequence(ssid, password, serial), 1):
            for attempt in range(1, retries + 1):
                sock.sendall(frame)
                try:
                    if is_ack(_read_frame(sock)):
                        log(f"step {n}/4 acked")
                        break
                    log(f"step {n}/4 bad ack (attempt {attempt})")
                except TimeoutError:
                    log(f"step {n}/4 timeout (attempt {attempt})")
            else:
                log(f"step {n}/4 failed")
                return False
    return True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="coway-setup", description=__doc__)
    p.add_argument("--ssid", required=True)
    p.add_argument("--serial", required=True, help="serial number on the unit's label")
    p.add_argument("--password", help="Wi-Fi password (prompted if omitted)")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--timeout", type=float, default=5.0)
    p.add_argument("--retries", type=int, default=3)
    a = p.parse_args(argv)
    password = a.password if a.password is not None else getpass.getpass("Wi-Fi password: ")
    try:
        ok = provision(a.ssid, password, a.serial.strip(), a.host, a.port, a.timeout, a.retries)
    except OSError as e:
        print(f"connection failed: {e}", file=sys.stderr)
        return 2
    print("done: purifier will join the network" if ok else "setup failed", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
