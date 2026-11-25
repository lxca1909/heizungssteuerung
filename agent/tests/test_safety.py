from dataclasses import replace

import pytest

from heater_agent.safety import Safety, SafetyConfig
from heater_agent.sunster import HeaterStatus

BASE = HeaterStatus(
    on=False, run_state=1, phase="standby", mode="temperatur", raw_mode=2, raw_value=20,
    set_temp=20, gear=1, room_temp=10.0, case_temp=20.0, voltage=12.8, co_ppm=0.0, error_code=0,
)
RUNNING = replace(BASE, on=True, run_state=2, phase="heizen", mode="vollgas", raw_mode=4, raw_value=10,
                  set_temp=None, gear=10)


def make(**kw) -> Safety:
    return Safety(SafetyConfig(**kw))


def test_start_allowed():
    assert make().check_start(BASE, 0) is None


@pytest.mark.parametrize(
    "status, expected",
    [
        (None, "nicht verbunden"),
        (replace(BASE, error_code=3), "Fehler 3"),
        (replace(BASE, run_state=3), "kühlt"),
    ],
)
def test_start_denied(status, expected):
    assert expected in make().check_start(status, 0)


def test_runtime_capped():
    s = make(max_runtime_min=240, default_runtime_min=120)
    assert s.runtime_for(None) == 120
    assert s.runtime_for(30) == 30
    assert s.runtime_for(999) == 240
    with pytest.raises(ValueError):
        s.runtime_for(0)


def test_deadline_turns_off():
    s = make()
    s.on_started("1", 10, now=0)
    assert s.check_running("1", RUNNING, now=599) is None
    assert "Laufzeit" in s.check_running("1", RUNNING, now=600)


def test_external_start_gets_default_deadline():
    s = make(default_runtime_min=60)
    assert s.check_running("1", RUNNING, now=1000) is None
    assert s.deadline("1") == 1000 + 3600


def test_stopped_heater_clears_run():
    s = make()
    s.on_started("1", 10, now=0)
    s.check_running("1", BASE, now=5)
    assert s.deadline("1") is None


def test_co_alarm_hysteresis_and_shutdown():
    s = make(co_off_ppm=50, co_clear_ppm=20)
    assert s.update_co(10, 0) is False
    assert s.update_co(55, 1) is True          # löst neu aus
    assert s.update_co(60, 2) is False         # schon aktiv
    assert s.check_running("1", RUNNING, 3) == "CO-Alarm"
    assert "CO-Alarm" in s.check_start(BASE, 3)
    s.update_co(30, 4)                          # über clear-Schwelle: bleibt gesperrt
    assert s.co_alarm
    s.update_co(19, 5)
    assert not s.co_alarm and s.check_start(BASE, 5) is None


def test_heater_own_co_sensor_triggers_alarm():
    s = make(co_off_ppm=50)
    assert s.check_running("1", replace(RUNNING, co_ppm=80.0), 0) == "CO-Alarm"


def test_co_sensor_required():
    s = make(co_sensor_required=True, co_sensor_max_age_s=60)
    assert "CO-Sensor" in s.check_start(BASE, 0)
    s.update_co(0, 100)
    assert s.check_start(BASE, 150) is None
    assert "CO-Sensor" in s.check_start(BASE, 161)


def test_vollgas_room_temp_limit():
    s = make(vollgas_max_room_temp=30)
    s.on_started("1", 60, 0)
    assert s.check_running("1", replace(RUNNING, room_temp=29.0), 1) is None
    assert "Raumtemperatur" in s.check_running("1", replace(RUNNING, room_temp=30.0), 2)
    # im Temperaturmodus regelt die Heizung selbst
    temp_mode = replace(RUNNING, mode="temperatur", raw_mode=2, room_temp=31.0)
    assert s.check_running("1", temp_mode, 3) is None
