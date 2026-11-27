"""Kern des Agenten: hält die Heizungen, setzt Sicherheitsregeln durch, verarbeitet Befehle.

Unabhängig von MQTT: Veröffentlichen läuft über eine ``publish``-Funktion,
Befehle kommen über :meth:`Agent.handle_command`. Fällt die Serververbindung
weg, laufen Abfrage und Sicherheitsregeln einfach weiter.

MQTT-Themen (Präfix z. B. ``werkstatt``):
  <p>/agent/status            "online" / "offline" (retained, Last Will)
  <p>/heater/<id>/state       JSON-Zustand (retained)
  <p>/heater/<id>/cmd         Befehle (JSON, siehe handle_command)
  <p>/heater/<id>/ack         Antwort auf Befehle
  <p>/alarm                   Alarme/Ereignisse (JSON)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

from .safety import Safety
from .sunster import HeaterError, HeaterStatus, SunsterHeater

log = logging.getLogger(__name__)

Publish = Callable[[str, Any, bool], Awaitable[None]]
MAX_COMMAND_AGE_S = 60


class HeaterWorker:
    def __init__(
        self,
        heater_id: str,
        name: str,
        heater: SunsterHeater,
        safety: Safety,
        publish: Publish,
        poll_interval: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.id = heater_id
        self.name = name or f"Heizung {heater_id}"
        self.heater = heater
        self.safety = safety
        self._publish = publish
        self.poll_interval = poll_interval
        self.clock = clock
        self.status: HeaterStatus | None = None
        self.last_error: str | None = None
        self.last_seen: float | None = None
        self._wake = asyncio.Event()
        self._backoff = 5.0
        self._reminded_for: float | None = None  # Abschaltzeit, für die schon erinnert wurde

    # ---------------------------------------------------------- Zustand
    def state(self) -> dict[str, Any]:
        deadline = self.safety.deadline(self.id)
        now = self.clock()
        return {
            "id": self.id,
            "name": self.name,
            "connected": self.heater.connected,
            "last_seen": self.last_seen,
            "last_error": self.last_error,
            "status": self.status.to_dict() if self.status else None,
            "off_at": deadline,
            "remaining_min": round(max(0.0, (deadline - now) / 60), 1) if deadline else None,
            "ts": now,
        }

    async def publish_state(self) -> None:
        await self._publish(f"heater/{self.id}/state", self.state(), True)

    async def alarm(self, level: str, message: str) -> None:
        log.warning("[%s] %s", self.name, message)
        await self._publish(
            "alarm", {"level": level, "heater": self.id, "name": self.name, "message": message, "ts": self.clock()}, False
        )

    # ------------------------------------------------------- Schleife
    async def run(self) -> None:
        while True:
            try:
                await self.poll_once()
                self._backoff = 5.0
                delay = self.poll_interval
            except (HeaterError, OSError, asyncio.TimeoutError, Exception) as err:  # noqa: BLE001
                self.last_error = str(err) or type(err).__name__
                log.info("[%s] %s – neuer Versuch in %.0f s", self.name, self.last_error, self._backoff)
                await self.heater.disconnect()
                await self.publish_state()
                delay, self._backoff = self._backoff, min(self._backoff * 2, 60.0)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), delay)
            except asyncio.TimeoutError:
                pass

    async def poll_once(self) -> None:
        if not self.heater.connected:
            await self.heater.connect()
        status = await self.heater.status()
        await self._apply_status(status)
        reason = self.safety.check_running(self.id, status, self.clock())
        if reason:
            await self.force_off(reason)
        else:
            await self._maybe_remind()
        await self.publish_state()

    async def _maybe_remind(self) -> None:
        """Einmal pro Abschaltzeit kurz vorher erinnern (Push: „verlängern?“)."""
        deadline = self.safety.deadline(self.id)
        window = self.safety.cfg.reminder_min * 60
        if deadline is None or window <= 0 or self._reminded_for == deadline:
            return
        left = deadline - self.clock()
        if 0 < left <= window:
            self._reminded_for = deadline
            off = time.strftime("%H:%M", time.localtime(deadline))
            await self._publish("alarm", {
                "level": "reminder", "heater": self.id, "name": self.name, "ts": self.clock(), "off_at": deadline,
                "message": f"Geht um {off} automatisch aus (in {left / 60:.0f} min) – in der WebApp verlängern?",
            }, False)

    async def _apply_status(self, status: HeaterStatus) -> None:
        prev = self.status
        self.status = status
        self.last_seen = self.clock()
        self.last_error = None
        if status.error_code and (prev is None or prev.error_code != status.error_code):
            await self.alarm("error", f"Heizung meldet Fehler {status.error_code}")

    async def force_off(self, reason: str) -> None:
        await self.alarm("warning", f"Sicherheitsabschaltung: {reason}")
        try:
            status = await self.heater.stop()
            await self._apply_status(status)
        finally:
            self.safety.on_stopped(self.id)

    # -------------------------------------------------------- Befehle
    async def handle(self, cmd: dict[str, Any]) -> tuple[bool, str]:
        """Befehle:
        {"action": "start", "mode": "vollgas"|"temperatur"|"lueften", "temp": 22, "level": 5, "minutes": 120}
            (lueften = nur Gebläse, Stufe 1–10, nur bei laufender Heizung)
        {"action": "extend", "minutes": 60}
        {"action": "stop"}
        {"action": "refresh"}
        """
        action = cmd.get("action")
        now = self.clock()
        ts = cmd.get("ts")
        if ts is not None and now - float(ts) > MAX_COMMAND_AGE_S:
            return False, "Befehl zu alt, ignoriert"

        if action == "refresh":
            self._wake.set()
            return True, "Status wird aktualisiert"

        if not self.heater.connected:
            return False, "Heizung nicht verbunden"

        if action == "stop":
            status = await self.heater.stop()
            await self._apply_status(status)
            self.safety.on_stopped(self.id)
            await self.publish_state()
            return True, "Ausgeschaltet" if not status.on else "Ausschaltbefehl gesendet"

        if action == "extend":
            if not (self.status and self.status.on):
                return False, "Heizung läuft nicht"
            if self.safety.co_alarm:
                return False, "CO-Alarm aktiv"
            deadline = self.safety.extend(self.id, float(cmd.get("minutes", 60)), now)
            await self.publish_state()
            return True, f"Verlängert – läuft bis {time.strftime('%H:%M', time.localtime(deadline))}"

        if action == "start":
            status = await self.heater.status()
            await self._apply_status(status)
            denied = self.safety.check_start(status, now)
            if denied:
                return False, denied
            minutes = self.safety.runtime_for(cmd.get("minutes"))
            mode = cmd.get("mode", "vollgas")
            if mode == "vollgas":
                status = await self.heater.start_vollgas()
            elif mode == "temperatur":
                status = await self.heater.start_temperature(int(cmd.get("temp", 20)))
            elif mode == "lueften":
                if not status.on:
                    return False, "Lüften geht nur, wenn die Heizung läuft"
                status = await self.heater.set_ventilation(int(cmd.get("level", 5)))
            else:
                return False, f"Unbekannter Modus: {mode}"
            await self._apply_status(status)
            self.safety.on_started(self.id, minutes, now)
            await self.publish_state()
            if not status.on:
                return False, "Befehl gesendet, Heizung meldet aber noch AUS"
            label = {"vollgas": "Vollgas", "temperatur": f"Temperatur {status.set_temp} °C",
                     "lueften": f"Lüften Stufe {status.fan_level}"}.get(status.mode, status.mode)
            if status.raw_mode != {"vollgas": 4, "temperatur": 2, "lueften": 3}[mode]:
                return False, f"Heizung hat den Modus nicht übernommen (meldet {label})"
            return True, f"Läuft ({label}), automatisch aus in {minutes:.0f} min"

        return False, f"Unbekannte Aktion: {action}"


class Agent:
    def __init__(self, workers: list[HeaterWorker], safety: Safety, publish: Publish,
                 clock: Callable[[], float] = time.time) -> None:
        self.workers = {w.id: w for w in workers}
        self.safety = safety
        self._publish = publish
        self.clock = clock

    async def run(self) -> None:
        await asyncio.gather(*(w.run() for w in self.workers.values()))

    async def publish_all(self) -> None:
        for w in self.workers.values():
            await w.publish_state()

    async def handle_command(self, heater_id: str, payload: bytes | str) -> None:
        worker = self.workers.get(heater_id)
        try:
            cmd = json.loads(payload)
            if not isinstance(cmd, dict):
                raise ValueError("JSON-Objekt erwartet")
        except ValueError as err:
            await self._publish(f"heater/{heater_id}/ack", {"ok": False, "message": f"Ungültiger Befehl: {err}"}, False)
            return
        if worker is None:
            ok, message = False, f"Unbekannte Heizung {heater_id}"
        else:
            try:
                ok, message = await worker.handle(cmd)
            except (HeaterError, ValueError) as err:
                ok, message = False, str(err)
        log.info("Befehl %s an %s: %s", cmd.get("action"), heater_id, message)
        await self._publish(f"heater/{heater_id}/ack", {"id": cmd.get("id"), "ok": ok, "message": message}, False)

    async def handle_co(self, payload: bytes | str) -> None:
        try:
            ppm = float(payload)
        except ValueError:
            log.warning("Ungültiger CO-Wert: %r", payload)
            return
        if self.safety.update_co(ppm, self.clock()):
            await self._publish("alarm", {"level": "critical", "message": f"CO-Alarm: {ppm:.0f} ppm – alle Heizungen AUS",
                                          "ts": self.clock()}, False)
            for w in self.workers.values():
                if w.status and w.status.on and w.heater.connected:
                    try:
                        await w.force_off("CO-Alarm")
                    except HeaterError as err:
                        await w.alarm("critical", f"Abschalten bei CO-Alarm fehlgeschlagen: {err}")
