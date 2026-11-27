import json

import pytest

from heater_agent.agent import Agent, HeaterWorker
from heater_agent.safety import Safety, SafetyConfig
from heater_agent.sunster import SunsterHeater

from .fakes import ADDRESS, SimHeater, client_factory


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def setup():
    sim = SimHeater()
    clock = Clock()
    published: list[tuple[str, object, bool]] = []

    async def publish(topic, payload, retain):
        published.append((topic, payload, retain))

    safety = Safety(SafetyConfig(default_runtime_min=60, max_runtime_min=120))
    heater = SunsterHeater(ADDRESS, 1234, client_factory(sim), timeout=1)
    worker = HeaterWorker("1", "Vorne", heater, safety, publish, 15, clock)
    agent = Agent([worker], safety, publish, clock)
    return sim, clock, published, worker, agent


def acks(published):
    return [p for t, p, _ in published if t.endswith("/ack")]


async def test_poll_publishes_retained_state(setup):
    sim, clock, published, worker, agent = setup
    await worker.poll_once()
    topic, state, retain = published[-1]
    assert topic == "heater/1/state" and retain
    assert state["connected"] and state["status"]["mode"] == "temperatur"


async def test_start_vollgas_command_and_auto_off(setup):
    sim, clock, published, worker, agent = setup
    await worker.poll_once()
    await agent.handle_command("1", json.dumps({"action": "start", "mode": "vollgas", "minutes": 30, "ts": clock.t}))
    ack = acks(published)[-1]
    assert ack["ok"], ack
    assert sim.run_state == 2 and sim.mode == 4
    assert worker.state()["remaining_min"] == 30

    clock.t += 30 * 60
    await worker.poll_once()
    assert sim.run_state == 3  # Abkühlphase = aus
    alarms = [p for t, p, _ in published if t == "alarm"]
    assert any("Laufzeit" in a["message"] for a in alarms)


async def test_start_denied_during_cooldown(setup):
    sim, clock, published, worker, agent = setup
    sim.run_state = 3
    await worker.poll_once()
    await agent.handle_command("1", json.dumps({"action": "start"}))
    ack = acks(published)[-1]
    assert not ack["ok"] and "kühlt" in ack["message"]


async def test_stale_and_invalid_commands(setup):
    sim, clock, published, worker, agent = setup
    await worker.poll_once()
    await agent.handle_command("1", json.dumps({"action": "start", "ts": clock.t - 3600}))
    assert "zu alt" in acks(published)[-1]["message"]
    await agent.handle_command("1", "kein json")
    assert not acks(published)[-1]["ok"]
    await agent.handle_command("9", json.dumps({"action": "stop"}))
    assert "Unbekannte Heizung" in acks(published)[-1]["message"]
    assert sim.run_state == 1  # nichts wurde geschaltet


async def test_co_alarm_turns_heater_off(setup):
    sim, clock, published, worker, agent = setup
    await worker.poll_once()
    await agent.handle_command("1", json.dumps({"action": "start", "mode": "vollgas"}))
    assert sim.run_state == 2
    await agent.handle_co(b"75")
    assert sim.run_state == 3
    assert any(t == "alarm" and p["level"] == "critical" for t, p, _ in published)
    await agent.handle_command("1", json.dumps({"action": "start"}))
    assert "CO-Alarm" in acks(published)[-1]["message"]


async def test_temperature_start(setup):
    sim, clock, published, worker, agent = setup
    await worker.poll_once()
    await agent.handle_command("1", json.dumps({"action": "start", "mode": "temperatur", "temp": 22}))
    assert acks(published)[-1]["ok"]
    assert sim.mode == 2 and sim.value == 22


async def test_extend_and_reminder(setup):
    sim, clock, published, worker, agent = setup
    await worker.poll_once()
    await agent.handle_command("1", json.dumps({"action": "start", "mode": "vollgas", "minutes": 60}))
    start = clock.t

    clock.t = start + 40 * 60                      # 20 min Rest: noch keine Erinnerung
    await worker.poll_once()
    assert not [p for t, p, _ in published if t == "alarm" and p["level"] == "reminder"]

    clock.t = start + 46 * 60                      # 14 min Rest: Erinnerung
    await worker.poll_once()
    await worker.poll_once()                       # nur einmal
    reminders = [p for t, p, _ in published if t == "alarm" and p["level"] == "reminder"]
    assert len(reminders) == 1 and "verlängern" in reminders[0]["message"]

    await agent.handle_command("1", json.dumps({"action": "extend", "minutes": 60}))
    assert acks(published)[-1]["ok"], acks(published)[-1]
    assert worker.state()["remaining_min"] == 74.0

    clock.t = start + 61 * 60                      # alte Abschaltzeit vorbei: läuft weiter
    await worker.poll_once()
    assert sim.run_state == 2

    clock.t = start + 106 * 60                     # neue Erinnerung für die neue Abschaltzeit
    await worker.poll_once()
    reminders = [p for t, p, _ in published if t == "alarm" and p["level"] == "reminder"]
    assert len(reminders) == 2

    clock.t = start + 120 * 60
    await worker.poll_once()
    assert sim.run_state == 3                      # jetzt aus


async def test_extend_when_off(setup):
    sim, clock, published, worker, agent = setup
    await worker.poll_once()
    await agent.handle_command("1", json.dumps({"action": "extend", "minutes": 60}))
    assert not acks(published)[-1]["ok"]


async def test_ventilation_command(setup):
    sim, clock, published, worker, agent = setup
    await worker.poll_once()
    await agent.handle_command("1", json.dumps({"action": "start", "mode": "lueften", "level": 4}))
    assert "läuft" in acks(published)[-1]["message"]          # aus -> abgelehnt
    await agent.handle_command("1", json.dumps({"action": "start", "mode": "vollgas"}))
    await agent.handle_command("1", json.dumps({"action": "start", "mode": "lueften", "level": 4, "minutes": 30}))
    ack = acks(published)[-1]
    assert ack["ok"] and "Lüften Stufe 4" in ack["message"], ack
    assert sim.mode == 3 and sim.value == 4
