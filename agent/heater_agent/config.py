"""Konfiguration aus YAML laden (config.yaml + optional config.local.yaml mit Geheimnissen)."""
from __future__ import annotations

import os
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
    address: str
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


def load_config(path: str | Path) -> Config:
    path = Path(path)
    raw: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
    local = path.with_name("config.local.yaml")
    if local.exists():
        raw = _merge(raw, yaml.safe_load(local.read_text()) or {})

    mqtt = _build(MqttConfig, raw.get("mqtt"))
    if os.environ.get("HEATER_MQTT_PASSWORD"):
        mqtt.password = os.environ["HEATER_MQTT_PASSWORD"]

    heaters = [_build(HeaterConfig, {**h, "id": str(h["id"])}) for h in raw.get("heaters", [])]
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
