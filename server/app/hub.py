"""Verbindung zum MQTT-Broker: hält den aktuellen Zustand, verteilt Live-Updates, sendet Befehle."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from typing import Any, Callable

import aiomqtt

from .db import Database
from .notify import Notifier

log = logging.getLogger(__name__)

SAMPLE_INTERVAL_S = 60


class AgentOffline(Exception):
    pass


class CommandTimeout(Exception):
    pass


class Hub:
    def __init__(
        self,
        db: Database,
        notifier: Notifier,
        prefix: str = "werkstatt",
        command_timeout_s: float = 30.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = db
        self.notifier = notifier
        self.prefix = prefix.rstrip("/")
        self.command_timeout_s = command_timeout_s
        self.clock = clock
        self.heaters: dict[str, dict[str, Any]] = {}
        self.agent_online: bool | None = None
        self.broker_connected = False
        self.alarms: deque[dict[str, Any]] = deque(maxlen=50)
        self._subscribers: set[asyncio.Queue] = set()
        self._pending: dict[str, asyncio.Future] = {}
        self._last_sample: dict[str, float] = {}
        self._client: aiomqtt.Client | None = None

    # ---------------------------------------------------------- Zustand
    def snapshot(self) -> dict[str, Any]:
        return {
            "agent_online": self.agent_online,
            "broker_connected": self.broker_connected,
            "heaters": sorted(self.heaters.values(), key=lambda h: h.get("id", "")),
            "alarms": list(self.alarms)[-20:][::-1],
            "ts": self.clock(),
        }

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _broadcast(self, event: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Langsamer Client: verwerfen, er bekommt beim Neuverbinden einen Snapshot.
                self._subscribers.discard(q)

    # ------------------------------------------------------ Nachrichten
    async def handle_message(self, topic: str, payload: bytes | str) -> None:
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8", "replace")
        rel = topic[len(self.prefix) + 1:] if topic.startswith(self.prefix + "/") else None
        if rel is None:
            return
        parts = rel.split("/")

        if rel == "agent/status":
            online = payload.strip() == "online"
            was = self.agent_online
            self.agent_online = online
            self._broadcast({"type": "agent", "online": online})
            if was is True and not online:
                await self._alarm({"level": "error", "message": "Werkstatt-PC ist offline", "ts": self.clock()})
            elif was is False and online:
                await self.notifier.send("Heizung", "Werkstatt-PC ist wieder online", "info")
            return

        if len(parts) == 3 and parts[0] == "heater":
            hid, kind = parts[1], parts[2]
            data = _json(payload)
            if data is None:
                return
            if kind == "state":
                data["id"] = hid
                self.heaters[hid] = data
                self._broadcast({"type": "heater", "data": data})
                await self._maybe_sample(hid, data)
            elif kind == "ack":
                fut = self._pending.pop(str(data.get("id")), None)
                if fut and not fut.done():
                    fut.set_result(data)
            return

        if rel == "alarm":
            data = _json(payload)
            if data is not None:
                await self._alarm(data)

    async def _maybe_sample(self, hid: str, state: dict[str, Any]) -> None:
        status = state.get("status")
        now = self.clock()
        if status and now - self._last_sample.get(hid, 0) >= SAMPLE_INTERVAL_S:
            self._last_sample[hid] = now
            await asyncio.to_thread(self.db.add_sample, hid, status, now)

    async def _alarm(self, alarm: dict[str, Any]) -> None:
        alarm.setdefault("ts", self.clock())
        self.alarms.append(alarm)
        self._broadcast({"type": "alarm", "data": alarm})
        await asyncio.to_thread(self.db.add_alarm, alarm)
        name = alarm.get("name") or ("Heizung " + alarm["heater"] if alarm.get("heater") else "Werkstatt")
        await self.notifier.send(name, alarm.get("message", ""), alarm.get("level", "info"))

    # ----------------------------------------------------------- Befehle
    async def send_command(self, heater_id: str, cmd: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            raise AgentOffline("Keine Verbindung zum MQTT-Broker")
        if self.agent_online is not True:
            raise AgentOffline("Werkstatt-PC ist offline")
        cmd_id = uuid.uuid4().hex
        msg = {**cmd, "id": cmd_id, "ts": self.clock()}
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[cmd_id] = fut
        try:
            await self._client.publish(f"{self.prefix}/heater/{heater_id}/cmd", json.dumps(msg), qos=1)
            return await asyncio.wait_for(fut, self.command_timeout_s)
        except asyncio.TimeoutError:
            raise CommandTimeout("Keine Antwort vom Werkstatt-PC") from None
        finally:
            self._pending.pop(cmd_id, None)

    # ------------------------------------------------------------ Schleife
    async def run(self, host: str, port: int, username: str | None, password: str | None) -> None:
        backoff = 2.0
        while True:
            try:
                async with aiomqtt.Client(hostname=host, port=port, username=username, password=password,
                                          identifier=f"heizung-web-{uuid.uuid4().hex[:6]}", keepalive=30) as client:
                    self._client = client
                    self.broker_connected = True
                    backoff = 2.0
                    log.info("MQTT verbunden")
                    await client.subscribe(f"{self.prefix}/#", qos=1)
                    async for msg in client.messages:
                        try:
                            await self.handle_message(str(msg.topic), msg.payload)
                        except Exception:  # noqa: BLE001
                            log.exception("Fehler bei Nachricht auf %s", msg.topic)
            except aiomqtt.MqttError as err:
                log.warning("MQTT getrennt: %s", err)
            finally:
                self._client = None
                self.broker_connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


def _json(payload: str) -> dict[str, Any] | None:
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
