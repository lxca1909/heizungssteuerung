"""Start: python -m heater_agent -c config.yaml"""
from __future__ import annotations

import argparse
import asyncio
import logging

from .agent import Agent, HeaterWorker
from .config import load_config
from .mqtt import MqttBridge
from .safety import Safety
from .sunster import SunsterHeater


async def amain(config_path: str) -> None:
    cfg = load_config(config_path)
    if not cfg.heaters:
        raise SystemExit("Keine Heizungen in der Konfiguration")
    bridge = MqttBridge(cfg.mqtt, cfg.co_topic)
    safety = Safety(cfg.safety)
    workers = [
        HeaterWorker(h.id, h.name, SunsterHeater(h.address, h.pin), safety, bridge.publish, cfg.poll_interval_s)
        for h in cfg.heaters
    ]
    agent = Agent(workers, safety, bridge.publish)
    await asyncio.gather(agent.run(), bridge.run(agent))


def main() -> None:
    p = argparse.ArgumentParser(description="Heizungs-Agent für den Werkstatt-PC")
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("bleak").setLevel(logging.WARNING)
    try:
        asyncio.run(amain(args.config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
