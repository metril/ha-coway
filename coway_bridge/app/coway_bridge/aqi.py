"""US EPA AQI computed from PM10 (the purifier only reports PM10) with short-term smoothing."""

from __future__ import annotations

import time
from collections import deque

from .registers import S_PM10

# (pm_lo, pm_hi, aqi_lo, aqi_hi)
PM10_BREAKPOINTS = (
    (0, 54, 0, 50),
    (55, 154, 51, 100),
    (155, 254, 101, 150),
    (255, 354, 151, 200),
    (355, 424, 201, 300),
    (425, 504, 301, 400),
    (505, 604, 401, 500),
)
BANDS = (
    (50, "Good"),
    (100, "Moderate"),
    (150, "Unhealthy for sensitive groups"),
    (200, "Unhealthy"),
    (300, "Very unhealthy"),
    (500, "Hazardous"),
)
WINDOW = 300.0  # seconds of PM10 samples averaged


def pm10_to_aqi(pm10: float) -> int:
    """Piecewise-linear EPA index for a PM10 concentration (ug/m3); clamped to 0..500."""
    if pm10 <= 0:
        return 0
    if pm10 >= 604:
        return 500
    c = int(pm10)  # EPA truncates PM10 to whole numbers
    for lo, hi, a_lo, a_hi in PM10_BREAKPOINTS:
        if c <= hi:
            return round((a_hi - a_lo) / (hi - lo) * (c - lo) + a_lo)
    return 500  # unreachable


def aqi_band(aqi: int | None) -> str | None:
    if aqi is None:
        return None
    return next((name for top, name in BANDS if aqi <= top), BANDS[-1][1])


class AqiTracker:
    """Rolling mean of PM10 samples per device over the last WINDOW seconds."""

    def __init__(self, window: float = WINDOW, clock=time.monotonic):
        self.window, self.clock = window, clock
        self._s: dict[str, deque[tuple[float, float]]] = {}

    @classmethod
    def for_hub(cls, hub) -> AqiTracker:
        """One shared tracker per hub, sampling on every state report (registered once)."""
        t = getattr(hub, "aqi_tracker", None)
        if t is None:
            t = hub.aqi_tracker = cls()
            hub.add_state_callback(t._on_state)
        return t

    def _on_state(self, mac: str, state: dict) -> None:
        v = state.get("sensor", {}).get(S_PM10)
        if v is not None:
            self.add(mac, v)

    def add(self, mac: str, pm10: float, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        q = self._s.setdefault(mac, deque())
        q.append((now, float(pm10)))
        while q and q[0][0] < now - self.window:
            q.popleft()

    def mean_pm10(self, mac: str, now: float | None = None) -> float | None:
        now = self.clock() if now is None else now
        vals = [v for t, v in self._s.get(mac, ()) if t >= now - self.window]
        return sum(vals) / len(vals) if vals else None

    def aqi(self, mac: str, now: float | None = None) -> int | None:
        m = self.mean_pm10(mac, now)
        return None if m is None else pm10_to_aqi(m)
