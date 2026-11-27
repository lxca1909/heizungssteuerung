import pytest

from heater_agent.config import load_config


def test_env_only(tmp_path):
    env = {
        "MQTT_HOST": "10.8.0.1", "MQTT_PORT": "1884", "MQTT_USER": "werkstatt", "MQTT_PASSWORD": "geheim",
        "HEATER_1_ADDRESS": "AA:BB:CC:DD:EE:01", "HEATER_1_PIN": "4711", "HEATER_1_NAME": "Vorne",
        "HEATER_2_ADDRESS": "AA:BB:CC:DD:EE:02",
        "SAFETY_MAX_RUNTIME_MIN": "300", "SAFETY_CO_SENSOR_REQUIRED": "true", "SAFETY_VOLLGAS_MAX_ROOM_TEMP": "aus",
        "SAFETY_REMINDER_MIN": "10",
    }
    cfg = load_config(tmp_path / "gibtsnicht.yaml", env)
    assert (cfg.mqtt.host, cfg.mqtt.port, cfg.mqtt.username, cfg.mqtt.password) == ("10.8.0.1", 1884, "werkstatt", "geheim")
    assert [(h.id, h.address, h.pin, h.name) for h in cfg.heaters] == [
        ("1", "AA:BB:CC:DD:EE:01", 4711, "Vorne"), ("2", "AA:BB:CC:DD:EE:02", 1234, "")]
    assert cfg.safety.max_runtime_min == 300
    assert cfg.safety.co_sensor_required is True
    assert cfg.safety.vollgas_max_room_temp is None
    assert cfg.safety.reminder_min == 10.0


def test_env_overrides_yaml(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("mqtt: {host: alt}\nheaters:\n  - {id: '1', address: 'AA:00', pin: 1111}\nsafety: {max_runtime_min: 100}\n")
    cfg = load_config(p, {"MQTT_HOST": "neu", "HEATER_1_PIN": "2222"})
    assert cfg.mqtt.host == "neu"
    assert cfg.heaters[0].address == "AA:00" and cfg.heaters[0].pin == 2222
    assert cfg.safety.max_runtime_min == 100


def test_heater_without_address_fails(tmp_path):
    with pytest.raises(ValueError, match="HEATER_3_ADDRESS"):
        load_config(None, {"HEATER_3_PIN": "1234"})


def test_example_yaml_still_loads():
    from pathlib import Path
    cfg = load_config(Path(__file__).parent.parent / "config.example.yaml", {})
    assert cfg.heaters and cfg.safety.reminder_min == 15
