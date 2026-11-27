"""FastAPI-WebApp: Login, Live-Status, Heizung starten/stoppen, Verlauf."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from .auth import LoginLimiter, check_login
from .db import Database
from .hub import AgentOffline, CommandTimeout, Hub
from .notify import Notifier
from .settings import Settings

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class LoginRequest(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class StartRequest(BaseModel):
    mode: Literal["vollgas", "temperatur"] = "vollgas"
    temp: int | None = Field(default=None, ge=8, le=36)
    minutes: int = Field(default=120, ge=5, le=240)


class ExtendRequest(BaseModel):
    minutes: int = Field(default=60, ge=15, le=240)


def create_app(settings: Settings | None = None, hub: Hub | None = None, start_mqtt: bool = True) -> FastAPI:
    settings = settings or Settings()
    settings.validate()
    users = settings.user_hashes()
    db = hub.db if hub else Database(settings.db_path)
    hub = hub or Hub(db, Notifier(settings.ntfy_url, settings.ntfy_token, settings.public_url), settings.topic_prefix,
                     settings.command_timeout_s)
    limiter = LoginLimiter()

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        tasks = []
        if start_mqtt:
            tasks.append(asyncio.create_task(
                hub.run(settings.mqtt_host, settings.mqtt_port, settings.mqtt_user, settings.mqtt_password)))
            tasks.append(asyncio.create_task(_prune_daily(db)))
        yield
        for t in tasks:
            t.cancel()

    app = FastAPI(title="Heizung", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="heizung_session",
        max_age=30 * 24 * 3600,
        same_site="strict",
        https_only=settings.cookie_secure,
    )
    app.state.hub = hub

    def user(request: Request) -> str:
        name = request.session.get("user")
        if not name or name not in users:
            raise HTTPException(401, "Nicht angemeldet")
        return name

    def client_ip(request: Request) -> str:
        # Hinter Caddy: echte IP aus X-Forwarded-For (Caddy setzt sie zuverlässig).
        fwd = request.headers.get("x-forwarded-for")
        return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")

    # ------------------------------------------------------------ Auth
    @app.post("/api/login")
    async def login(body: LoginRequest, request: Request) -> dict[str, Any]:
        ip = client_ip(request)
        if limiter.blocked(ip):
            raise HTTPException(429, "Zu viele Fehlversuche, bitte später erneut versuchen")
        ok = await asyncio.to_thread(check_login, users, body.username, body.password)
        if not ok:
            limiter.failed(ip)
            log.warning("Login fehlgeschlagen für %r von %s", body.username, ip)
            raise HTTPException(401, "Benutzername oder Passwort falsch")
        limiter.reset(ip)
        request.session.clear()
        request.session["user"] = body.username
        return {"user": body.username}

    @app.post("/api/logout")
    async def logout(request: Request) -> dict[str, bool]:
        request.session.clear()
        return {"ok": True}

    @app.get("/api/me")
    async def me(name: str = Depends(user)) -> dict[str, str]:
        return {"user": name}

    # ---------------------------------------------------------- Status
    @app.get("/api/state")
    async def state(_: str = Depends(user)) -> dict[str, Any]:
        return hub.snapshot()

    @app.get("/api/events")
    async def events(request: Request, _: str = Depends(user)) -> StreamingResponse:
        q = hub.subscribe()

        async def stream() -> AsyncIterator[str]:
            try:
                yield f"event: snapshot\ndata: {json.dumps(hub.snapshot())}\n\n"
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(q.get(), 20)
                        yield f"data: {json.dumps(event)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                hub.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/history")
    async def history(hours: float = 24, _: str = Depends(user)) -> dict[str, Any]:
        hours = max(1.0, min(hours, 24 * 30))
        rows = await asyncio.to_thread(db.history, time.time() - hours * 3600)
        series: dict[str, list[list[Any]]] = {}
        for r in rows:
            series.setdefault(r["heater"], []).append([r["ts"], r["room_temp"], r["on_"], r["mode"]])
        return {"hours": hours, "series": series}

    @app.get("/api/log")
    async def command_log(_: str = Depends(user)) -> dict[str, Any]:
        return {
            "commands": await asyncio.to_thread(db.recent_commands, 30),
            "alarms": await asyncio.to_thread(db.recent_alarms, 30),
        }

    # --------------------------------------------------------- Befehle
    async def run_command(name: str, heater_id: str, cmd: dict[str, Any]) -> dict[str, Any]:
        if heater_id not in hub.heaters:
            raise HTTPException(404, "Unbekannte Heizung")
        try:
            ack = await hub.send_command(heater_id, cmd)
        except AgentOffline as err:
            await asyncio.to_thread(db.log_command, name, heater_id, cmd["action"], json.dumps(cmd), False, str(err))
            raise HTTPException(503, str(err)) from None
        except CommandTimeout as err:
            await asyncio.to_thread(db.log_command, name, heater_id, cmd["action"], json.dumps(cmd), False, str(err))
            raise HTTPException(504, str(err)) from None
        await asyncio.to_thread(db.log_command, name, heater_id, cmd["action"], json.dumps(cmd),
                                bool(ack.get("ok")), str(ack.get("message", "")))
        return {"ok": bool(ack.get("ok")), "message": ack.get("message", "")}

    @app.post("/api/heater/{heater_id}/start")
    async def start(heater_id: str, body: StartRequest, name: str = Depends(user)) -> dict[str, Any]:
        cmd: dict[str, Any] = {"action": "start", "mode": body.mode, "minutes": body.minutes}
        if body.mode == "temperatur":
            if body.temp is None:
                raise HTTPException(422, "Temperatur fehlt")
            cmd["temp"] = body.temp
        return await run_command(name, heater_id, cmd)

    @app.post("/api/heater/{heater_id}/extend")
    async def extend(heater_id: str, body: ExtendRequest, name: str = Depends(user)) -> dict[str, Any]:
        return await run_command(name, heater_id, {"action": "extend", "minutes": body.minutes})

    @app.post("/api/heater/{heater_id}/stop")
    async def stop(heater_id: str, name: str = Depends(user)) -> dict[str, Any]:
        return await run_command(name, heater_id, {"action": "stop"})

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "broker": hub.broker_connected, "agent": hub.agent_online}

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app


async def _prune_daily(db: Database) -> None:
    while True:
        await asyncio.to_thread(db.prune)
        await asyncio.sleep(86400)


def app_factory() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    return create_app()
