"""Push-Benachrichtigungen über ntfy (https://ntfy.sh oder selbst gehostet)."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

PRIORITY = {"critical": "urgent", "error": "high", "warning": "default", "info": "low"}
TAGS = {"critical": "rotating_light", "error": "warning", "warning": "fire", "info": "information_source"}


class Notifier:
    def __init__(self, url: str | None, token: str | None = None) -> None:
        self.url = url
        self.token = token

    async def send(self, title: str, message: str, level: str = "info") -> None:
        if not self.url:
            return
        headers = {
            "Title": title.encode("utf-8").decode("latin-1", "ignore"),
            "Priority": PRIORITY.get(level, "default"),
            "Tags": TAGS.get(level, ""),
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(self.url, content=message.encode("utf-8"), headers=headers)
                r.raise_for_status()
        except httpx.HTTPError as err:
            log.warning("ntfy fehlgeschlagen: %s", err)
