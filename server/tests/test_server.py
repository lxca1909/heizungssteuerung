import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.auth import LoginLimiter, check_login, hash_password, verify_password
from app.db import Database
from app.hub import Hub
from app.main import create_app
from app.settings import Settings

PW = "richtig-langes-passwort"


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def send(self, title, message, level="info"):
        self.sent.append((title, message, level))


class FakeMqtt:
    """Ersetzt den MQTT-Client: beantwortet Befehle wie der Agent."""

    def __init__(self, hub, reply_ok=True):
        self.hub, self.reply_ok, self.published = hub, reply_ok, []

    async def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, json.loads(payload)))
        cmd = json.loads(payload)
        ack = {"id": cmd["id"], "ok": self.reply_ok, "message": "Läuft (vollgas)" if self.reply_ok else "kühlt ab"}
        asyncio.get_running_loop().call_soon(
            lambda: asyncio.ensure_future(self.hub.handle_message(topic.rsplit("/", 1)[0] + "/ack", json.dumps(ack))))


STATE = {"name": "Vorne", "connected": True, "status": {"on": False, "mode": "temperatur", "room_temp": 9.0,
                                                          "case_temp": 20.0, "voltage": 12.8}, "ts": 0}


@pytest.fixture
def env(tmp_path):
    settings = Settings(secret_key="x" * 40, users=f"luca:{hash_password(PW)}", db_path=str(tmp_path / "t.db"),
                        cookie_secure=False)
    notifier = FakeNotifier()
    hub = Hub(Database(settings.db_path), notifier, "werkstatt", command_timeout_s=2)
    app = create_app(settings, hub, start_mqtt=False)
    with TestClient(app) as client:
        yield client, hub, notifier


def login(client):
    r = client.post("/api/login", json={"username": "luca", "password": PW})
    assert r.status_code == 200


def feed(client, hub, topic, payload):
    client.portal.call(hub.handle_message, topic, payload if isinstance(payload, str) else json.dumps(payload))


# ------------------------------------------------------------------ Auth
def test_password_hash_roundtrip():
    h = hash_password("geheim123456")
    assert verify_password("geheim123456", h)
    assert not verify_password("falsch", h)
    assert not verify_password("x", "kaputt")
    assert not check_login({"a": h}, "b", "geheim123456")


def test_login_limiter():
    lim = LoginLimiter(limit=3, window_s=60)
    for i in range(3):
        lim.failed("1.2.3.4", now=100 + i)
    assert lim.blocked("1.2.3.4", now=105)
    assert not lim.blocked("1.2.3.4", now=200)


def test_api_requires_login(env):
    client, hub, _ = env
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/heater/1/start", json={}).status_code == 401
    r = client.post("/api/login", json={"username": "luca", "password": "falsch"})
    assert r.status_code == 401
    login(client)
    assert client.get("/api/me").json() == {"user": "luca"}
    client.post("/api/logout")
    assert client.get("/api/state").status_code == 401


def test_login_bruteforce_blocked(env):
    client, *_ = env
    for _ in range(5):
        client.post("/api/login", json={"username": "luca", "password": "falsch"})
    r = client.post("/api/login", json={"username": "luca", "password": PW})
    assert r.status_code == 429


def test_static_index_public(env):
    client, *_ = env
    r = client.get("/")
    assert r.status_code == 200 and "Werkstatt-Heizung" in r.text


# ----------------------------------------------------------------- State
def test_state_and_history(env):
    client, hub, _ = env
    login(client)
    feed(client, hub, "werkstatt/agent/status", "online")
    feed(client, hub, "werkstatt/heater/1/state", STATE)
    s = client.get("/api/state").json()
    assert s["agent_online"] is True
    assert s["heaters"][0]["id"] == "1" and s["heaters"][0]["status"]["room_temp"] == 9.0
    hist = client.get("/api/history?hours=1").json()
    assert hist["series"]["1"][0][1] == 9.0


def test_agent_offline_and_alarm_notify(env):
    client, hub, notifier = env
    feed(client, hub, "werkstatt/agent/status", "online")
    feed(client, hub, "werkstatt/agent/status", "offline")
    feed(client, hub, "werkstatt/alarm", {"level": "critical", "message": "CO-Alarm: 80 ppm", "ts": 1})
    msgs = [m for _, m, _ in notifier.sent]
    assert "Werkstatt-PC ist offline" in msgs and "CO-Alarm: 80 ppm" in msgs
    login(client)
    log = client.get("/api/log").json()
    assert any("CO-Alarm" in a["message"] for a in log["alarms"])


def test_ignores_foreign_and_broken_messages(env):
    client, hub, _ = env
    feed(client, hub, "anderes/heater/1/state", STATE)
    feed(client, hub, "werkstatt/heater/1/state", "kein json")
    assert hub.heaters == {}


# --------------------------------------------------------------- Befehle
def test_start_command_roundtrip(env):
    client, hub, _ = env
    login(client)
    feed(client, hub, "werkstatt/agent/status", "online")
    feed(client, hub, "werkstatt/heater/1/state", STATE)
    hub._client = FakeMqtt(hub)
    r = client.post("/api/heater/1/start", json={"mode": "vollgas", "minutes": 60})
    assert r.status_code == 200 and r.json()["ok"]
    topic, cmd = hub._client.published[-1]
    assert topic == "werkstatt/heater/1/cmd"
    assert cmd["action"] == "start" and cmd["mode"] == "vollgas" and cmd["minutes"] == 60 and cmd["ts"]
    entries = client.get("/api/log").json()["commands"]
    assert entries[0]["user"] == "luca" and entries[0]["ok"] == 1


def test_start_validation(env):
    client, hub, _ = env
    login(client)
    feed(client, hub, "werkstatt/agent/status", "online")
    feed(client, hub, "werkstatt/heater/1/state", STATE)
    hub._client = FakeMqtt(hub)
    assert client.post("/api/heater/1/start", json={"mode": "temperatur"}).status_code == 422
    assert client.post("/api/heater/1/start", json={"mode": "temperatur", "temp": 50}).status_code == 422
    assert client.post("/api/heater/1/start", json={"minutes": 999}).status_code == 422
    assert client.post("/api/heater/1/start", json={"mode": "turbo"}).status_code == 422
    assert client.post("/api/heater/9/start", json={}).status_code == 404
    assert hub._client.published == []


def test_command_when_agent_offline(env):
    client, hub, _ = env
    login(client)
    feed(client, hub, "werkstatt/heater/1/state", STATE)
    feed(client, hub, "werkstatt/agent/status", "offline")
    hub._client = FakeMqtt(hub)
    r = client.post("/api/heater/1/stop")
    assert r.status_code == 503 and "offline" in r.json()["detail"]


def test_command_timeout(env):
    client, hub, _ = env
    login(client)
    feed(client, hub, "werkstatt/agent/status", "online")
    feed(client, hub, "werkstatt/heater/1/state", STATE)

    class Silent:
        async def publish(self, *a, **k):
            pass

    hub._client = Silent()
    r = client.post("/api/heater/1/stop")
    assert r.status_code == 504


def test_rejected_command_passed_through(env):
    client, hub, _ = env
    login(client)
    feed(client, hub, "werkstatt/agent/status", "online")
    feed(client, hub, "werkstatt/heater/1/state", STATE)
    hub._client = FakeMqtt(hub, reply_ok=False)
    r = client.post("/api/heater/1/start", json={})
    assert r.status_code == 200 and r.json() == {"ok": False, "message": "kühlt ab"}


def test_extend_command(env):
    client, hub, _ = env
    login(client)
    feed(client, hub, "werkstatt/agent/status", "online")
    feed(client, hub, "werkstatt/heater/1/state", STATE)
    hub._client = FakeMqtt(hub)
    r = client.post("/api/heater/1/extend", json={"minutes": 120})
    assert r.status_code == 200
    assert hub._client.published[-1][1]["action"] == "extend"
    assert hub._client.published[-1][1]["minutes"] == 120
    assert client.post("/api/heater/1/extend", json={"minutes": 5}).status_code == 422
    assert client.post("/api/heater/1/extend", json={"minutes": 999}).status_code == 422


def test_reminder_push_links_to_webapp(env):
    client, hub, notifier = env
    feed(client, hub, "werkstatt/alarm",
         {"level": "reminder", "heater": "1", "name": "Vorne", "message": "Geht um 14:30 aus", "ts": 1})
    assert notifier.sent[-1] == ("Vorne", "Geht um 14:30 aus", "reminder")


async def test_ntfy_headers(monkeypatch):
    import httpx
    from app.notify import Notifier
    captured = {}

    async def fake_post(self, url, content, headers):
        captured.update(url=url, body=content.decode(), headers=headers)
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    await Notifier("https://ntfy.example/topic", public_url="https://heizung.example").send(
        "Heizung 1", "Geht um 14:30 aus – verlängern?", "reminder")
    h = captured["headers"]
    assert captured["body"] == "Geht um 14:30 aus – verlängern?"
    assert h["Priority"] == "high" and h["Click"] == "https://heizung.example"
    assert h["Actions"].startswith("view, Verl") and h["Actions"].endswith("https://heizung.example")
