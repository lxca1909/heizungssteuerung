"""Konfiguration: Umgebungsvariablen (.env) und optional YAML (config.yaml + config.local.yaml).

Für Docker reicht eine .env – Adressen, PINs und Passwörter gehören nie ins Repo.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from .safety import SafetyConfig


@dataclass
class MqttConfig:
    host: str = "localhost"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    topic_prefix: str = "werkstatt"
    client_id: str = "heater-agent"


@dataclass
class HeaterConfig:
    id: str
    address: str = ""
    pin: int = 1234
    name: str = ""


@dataclass
class Config:
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    heaters: list[HeaterConfig] = field(default_factory=list)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    poll_interval_s: float = 15.0
    co_topic: str | None = None


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _build(cls: type, data: dict[str, Any] | None) -> Any:
    data = data or {}
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"Unbekannte Einstellungen für {cls.__name__}: {sorted(unknown)}")
    return cls(**data)


_MQTT_ENV = {
    "MQTT_HOST": "host", "MQTT_PORT": "port", "MQTT_USER": "username",
    "MQTT_PASSWORD": "password", "TOPIC_PREFIX": "topic_prefix",
}
_HEATER_ENV = re.compile(r"^HEATER_([A-Za-z0-9]+)_(ADDRESS|PIN|NAME)$")


def _coerce(value: str, current: Any) -> Any:
    """Umgebungswert in den Typ des Standardwerts umwandeln."""
    if isinstance(current, bool):
        return value.strip().lower() in ("1", "true", "yes", "ja", "on")
    if isinstance(current, int):
        return int(value)
    if isinstance(current, float) or current is None:
        if value.strip().lower() in ("", "none", "aus", "off"):
            return None
        return float(value)
    return value


def _apply_env(raw: dict[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    """Umgebungsvariablen (z. B. aus .env) überschreiben die YAML-Werte.

    MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASSWORD, TOPIC_PREFIX
    HEATER_<id>_ADDRESS, HEATER_<id>_PIN, HEATER_<id>_NAME
    POLL_INTERVAL_S, CO_TOPIC, SAFETY_<FELD> (z. B. SAFETY_MAX_RUNTIME_MIN)
    """
    raw = _merge(raw, {})
    mqtt = dict(raw.get("mqtt") or {})
    for var, key in _MQTT_ENV.items():
        if env.get(var):
            mqtt[key] = int(env[var]) if key == "port" else env[var]
    raw["mqtt"] = mqtt

    heaters = {str(h["id"]): dict(h) for h in raw.get("heaters") or []}
    for var, value in env.items():
        m = _HEATER_ENV.match(var)
        if m and value != "":
            hid, field_name = m.group(1), m.group(2).lower()
            heaters.setdefault(hid, {"id": hid})[field_name] = int(value) if field_name == "pin" else value
    raw["heaters"] = sorted(heaters.values(), key=lambda h: str(h["id"]))

    if env.get("POLL_INTERVAL_S"):
        raw["poll_interval_s"] = float(env["POLL_INTERVAL_S"])
    if env.get("CO_TOPIC"):
        raw["co_topic"] = env["CO_TOPIC"]

    safety = dict(raw.get("safety") or {})
    defaults = SafetyConfig()
    for f in fields(SafetyConfig):
        var = f"SAFETY_{f.name.upper()}"
        if var in env and env[var] != "":
            safety[f.name] = _coerce(env[var], getattr(defaults, f.name))
    raw["safety"] = safety
    return raw


def load_config(path: str | Path | None = None, env: Mapping[str, str] | None = None) -> Config:
    """YAML (optional) laden, dann Umgebungsvariablen anwenden."""
    env = os.environ if env is None else env
    raw: dict[str, Any] = {}
    if path is not None and Path(path).exists():
        path = Path(path)
        raw = yaml.safe_load(path.read_text()) or {}
        local = path.with_name("config.local.yaml")
        if local.exists():
            raw = _merge(raw, yaml.safe_load(local.read_text()) or {})
    raw = _apply_env(raw, env)

    mqtt = _build(MqttConfig, raw.get("mqtt"))
    # Alter Name, weiterhin unterstützt
    if env.get("HEATER_MQTT_PASSWORD"):
        mqtt.password = env["HEATER_MQTT_PASSWORD"]

    heaters = [_build(HeaterConfig, {**h, "id": str(h["id"])}) for h in raw.get("heaters", [])]
    for h in heaters:
        if not h.address:
            raise ValueError(f"Heizung {h.id}: Adresse fehlt (HEATER_{h.id}_ADDRESS)")
    ids = [h.id for h in heaters]
    if len(ids) != len(set(ids)):
        raise ValueError("Heizungs-IDs müssen eindeutig sein")

    return Config(
        mqtt=mqtt,
        heaters=heaters,
        safety=_build(SafetyConfig, raw.get("safety")),
        poll_interval_s=float(raw.get("poll_interval_s", 15.0)),
        co_topic=raw.get("co_topic"),
    )
