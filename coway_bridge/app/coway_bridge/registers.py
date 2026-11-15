"""Register map for the AP-2015E. Control regs (A1010/A1011/A1012) and sensor regs (A1020)
live in separate namespaces (e.g. 0x0002 is mode in control, PM10 in sensors)."""

from __future__ import annotations

# control regs
R_POWER, R_MODE, R_SPEED, R_LIGHT, R_TIMER, R_SENS = 0x0001, 0x0002, 0x0003, 0x0007, 0x0008, 0x000A
CONTROL_KNOWN = {R_POWER, R_MODE, R_SPEED, R_LIGHT, R_TIMER, R_SENS}
CONTROL_UNKNOWN = (0x000E, 0x0012, 0x0018, 0x0019, 0x0021, 0x0022)

# sensor regs
S_PM10, S_LUX, S_PREFILTER, S_HEPA = 0x0002, 0x0007, 0x0011, 0x0012
SENSOR_KNOWN = {S_PM10, S_LUX, S_PREFILTER, S_HEPA}

LIGHT_ON, LIGHT_OFF = 2, 0
MODE_NAMES = {1: "smart", 2: "bedtime"}  # anything else -> manual
PRESETS = ("smart", "bedtime", "manual")
SENSITIVITY = {1: "insensitive", 2: "moderate", 3: "sensitive"}
TIMER = {0: "off", 60: "1h", 120: "2h", 240: "4h", 480: "8h"}

CHANNEL_CONTROL, CHANNEL_SENSOR, CHANNEL_SNAPSHOT = "control", "sensor", "snapshot"
CODE_CHANNEL = {"A1010": CHANNEL_CONTROL, "A1020": CHANNEL_SENSOR, "A1012": CHANNEL_SNAPSHOT}


def decode_control(regs: dict[int, int]) -> dict[str, object]:
    """Friendly decode of control registers (missing regs omitted)."""
    out: dict[str, object] = {}
    if R_POWER in regs:
        out["power"] = bool(regs[R_POWER])
    if R_MODE in regs:
        out["mode"] = MODE_NAMES.get(regs[R_MODE], "manual")
    if R_SPEED in regs:
        out["speed"] = regs[R_SPEED]
    if R_LIGHT in regs:
        out["light"] = regs[R_LIGHT] == LIGHT_ON
    if R_TIMER in regs:
        out["timer_min"] = regs[R_TIMER]
    if R_SENS in regs:
        out["sensitivity"] = SENSITIVITY.get(regs[R_SENS])
    return out


def decode_sensors(regs: dict[int, int]) -> dict[str, int]:
    names = {S_PM10: "pm10", S_LUX: "lux", S_PREFILTER: "prefilter_used", S_HEPA: "hepa_used"}
    return {n: regs[r] for r, n in names.items() if r in regs}


def writes_for(entity: str, value: str, control: dict[int, int]) -> dict[int, int] | None:
    """Map a mode/timer/sensitivity command to {reg: val}; None if the value is invalid.

    Shared by the web UI and the MQTT bridge. `entity` "preset" is an alias of "mode".
    Mode writes power the unit on when it is off (setting a mode while off has no effect).
    """
    v = value.strip() if isinstance(value, str) else value
    if entity in ("mode", "preset"):
        if v == "smart":
            w = {R_MODE: 1}
        elif v == "bedtime":
            w = {R_MODE: 2}
        elif v == "manual":  # the unit flips to manual when a speed is written
            w = {R_SPEED: control.get(R_SPEED) or 1}
        else:
            return None
        return w if control.get(R_POWER) else {R_POWER: 1, **w}
    if entity == "timer":
        inv = {name: reg for reg, name in TIMER.items()}
        return {R_TIMER: inv[v]} if v in inv else None
    if entity == "sensitivity":
        inv = {name: reg for reg, name in SENSITIVITY.items()}
        return {R_SENS: inv[v]} if v in inv else None
    return None
