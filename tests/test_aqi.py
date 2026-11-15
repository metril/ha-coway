import pytest
from coway_bridge.aqi import AqiTracker, aqi_band, pm10_to_aqi


@pytest.mark.parametrize(
    "pm,aqi",
    [
        (-5, 0),
        (0, 0),
        (12, 11),
        (54, 50),
        (55, 51),
        (154, 100),
        (155, 101),
        (254, 150),
        (255, 151),
        (354, 200),
        (355, 201),
        (424, 300),
        (425, 301),
        (504, 400),
        (505, 401),
        (604, 500),
        (9999, 500),
    ],
)
def test_pm10_breakpoints(pm, aqi):
    assert pm10_to_aqi(pm) == aqi


def test_bands():
    assert aqi_band(None) is None
    assert [aqi_band(a) for a in (0, 50, 51, 100, 101, 150, 151, 200, 201, 300, 301, 500)] == [
        "Good",
        "Good",
        "Moderate",
        "Moderate",
        "Unhealthy for sensitive groups",
        "Unhealthy for sensitive groups",
        "Unhealthy",
        "Unhealthy",
        "Very unhealthy",
        "Very unhealthy",
        "Hazardous",
        "Hazardous",
    ]


def test_rolling_mean_window():
    t = AqiTracker(window=300)
    assert t.aqi("A", now=0) is None
    t.add("A", 100, now=0)
    t.add("A", 20, now=100)
    t.add("A", 20, now=200)
    assert t.mean_pm10("A", now=200) == pytest.approx(46.667, abs=1e-3)
    assert t.aqi("A", now=200) == pm10_to_aqi(46.667)
    # the first sample ages out after 300 s
    assert t.mean_pm10("A", now=350) == 20
    assert t.aqi("A", now=1000) is None  # everything aged out
    assert t.aqi("B", now=0) is None  # per device


def test_spike_is_smoothed():
    t = AqiTracker(window=300)
    for i in range(29):
        t.add("A", 10, now=i * 10)
    t.add("A", 300, now=290)
    assert t.aqi("A", now=290) < pm10_to_aqi(300)
