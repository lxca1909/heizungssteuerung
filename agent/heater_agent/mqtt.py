"""MQTT-Anbindung mit automatischem Wiederverbinden."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiomqtt

from .agent import Agent
from .config import MqttConfig

log = logging.getLogger(__name__)


class MqttBridge:
    def __init__(self, cfg: MqttConfig, co_topic: str | None = None) -> None:
        self.cfg = cfg
        self.prefix = cfg.topic_prefix.rstrip("/")
        self.co_topic = co_topic
        self._client: aiomqtt.Client | None = None

    async def publish(self, suffix: str, payload: Any, retain: bool = False) -> None:
        """Veröffentlichen, falls verbunden. Offline wird verworfen (State ist retained
        und wird beim Wiederverbinden komplett neu gesendet)."""
        client = self._client
        if client is None:
            return
        data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        try:
            await client.publish(f"{self.prefix}/{suffix}", data, qos=1, retain=retain)
        except aiomqtt.MqttError as err:
            log.debug("publish fehlgeschlagen: %s", err)

    async def run(self, agent: Agent) -> None:
        status_topic = f"{self.prefix}/agent/status"
        will = aiomqtt.Will(status_topic, "offline", qos=1, retain=True)
        backoff = 2.0
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self.cfg.host,
                    port=self.cfg.port,
                    username=self.cfg.username,
                    password=self.cfg.password,
                    identifier=self.cfg.client_id,
                    will=will,
                    keepalive=30,
                ) as client:
                    self._client = client
                    backoff = 2.0
                    log.info("MQTT verbunden mit %s:%s", self.cfg.host, self.cfg.port)
                    await client.publish(status_topic, "online", qos=1, retain=True)
                    await client.subscribe(f"{self.prefix}/heater/+/cmd", qos=1)
                    if self.co_topic:
                        await client.subscribe(self.co_topic, qos=1)
                    await agent.publish_all()
                    async for msg in client.messages:
                        await self._dispatch(agent, str(msg.topic), msg.payload)
            except aiomqtt.MqttError as err:
                log.warning("MQTT getrennt: %s – neuer Versuch in %.0f s", err, backoff)
            finally:
                self._client = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _dispatch(self, agent: Agent, topic: str, payload: Any) -> None:
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8", "replace")
        if self.co_topic and topic == self.co_topic:
            await agent.handle_co(payload)
            return
        parts = topic.split("/")
        # <prefix...>/heater/<id>/cmd
        if len(parts) >= 3 and parts[-1] == "cmd" and parts[-3] == "heater":
            # Befehle nacheinander, aber ohne die Nachrichtenschleife zu blockieren.
            asyncio.create_task(agent.handle_command(parts[-2], payload))
