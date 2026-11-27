"""Einstellungen aus Umgebungsvariablen."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass
class Settings:
    mqtt_host: str = field(default_factory=lambda: _env("MQTT_HOST", "localhost"))
    mqtt_port: int = field(default_factory=lambda: int(_env("MQTT_PORT", "1883")))
    mqtt_user: str | None = field(default_factory=lambda: _env("MQTT_USER"))
    mqtt_password: str | None = field(default_factory=lambda: _env("MQTT_PASSWORD"))
    topic_prefix: str = field(default_factory=lambda: _env("TOPIC_PREFIX", "werkstatt"))
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY", ""))
    # Format: "name:hash,name2:hash2" – Hash erzeugen mit: python -m app.hashpw
    users: str = field(default_factory=lambda: _env("USERS", ""))
    db_path: str = field(default_factory=lambda: _env("DB_PATH", "heizung.db"))
    cookie_secure: bool = field(default_factory=lambda: _env("COOKIE_SECURE", "true").lower() == "true")
    ntfy_url: str | None = field(default_factory=lambda: _env("NTFY_URL"))
    ntfy_token: str | None = field(default_factory=lambda: _env("NTFY_TOKEN"))
    # Adresse der WebApp – Push-Nachrichten öffnen sie beim Antippen
    public_url: str | None = field(default_factory=lambda: _env("PUBLIC_URL"))
    command_timeout_s: float = field(default_factory=lambda: float(_env("COMMAND_TIMEOUT_S", "30")))

    def user_hashes(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for entry in self.users.split(","):
            entry = entry.strip()
            if entry:
                name, _, hashed = entry.partition(":")
                out[name.strip()] = hashed.strip()
        return out

    def validate(self) -> None:
        if len(self.secret_key) < 32:
            raise RuntimeError("SECRET_KEY fehlt oder ist zu kurz (mind. 32 Zeichen)")
        if not self.user_hashes():
            raise RuntimeError("USERS ist leer – mindestens einen Benutzer anlegen (python -m app.hashpw)")
