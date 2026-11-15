import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "coway_bridge" / "app"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_sigterm_exits_promptly(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(APP)}
    p = subprocess.Popen(
        [
            sys.executable, "-m", "coway_bridge",
            "--config", str(tmp_path / "none.json"),
            "--data-dir", str(tmp_path),
            "--ingress-port", str(_free_port()),
            "--listen-port", str(_free_port()),
            "--mqtt-host", "127.0.0.1",
            "--mqtt-port", str(_free_port()),  # nothing listens here
        ],
        env=env,
        stderr=subprocess.PIPE,
        text=True,
    )  # fmt: skip
    try:
        time.sleep(2.0)  # let it start and hit the MQTT retry loop
        assert p.poll() is None, p.stderr.read()
        p.send_signal(signal.SIGTERM)
        t0 = time.time()
        p.wait(timeout=8)
        assert time.time() - t0 < 8
    finally:
        if p.poll() is None:
            p.kill()
        p.stderr.close()
