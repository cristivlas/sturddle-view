from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

import psutil
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api import agent as agent_api
from .api import engines as engines_api
from .api import game as game_api
from .api import settings as settings_api
from .api import ws as ws_api
from .config import Settings
from .engines import EngineRegistry
from .events import EventBus

log = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
    for task in list(app.state.ws_tasks):
        task.cancel()
    if app.state.ws_tasks:
        await asyncio.gather(*app.state.ws_tasks, return_exceptions=True)
    if app.state.hve is not None:
        await app.state.hve.shutdown()


def create_app(
    settings: Settings | None = None,
    *,
    engine_registry: EngineRegistry | None = None,
) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="sturddle-view", version="0.0.1", lifespan=_lifespan)

    app.state.settings = settings
    app.state.event_bus = EventBus()
    app.state.hve = None  # lazy: HumanVsEngine, created on first /game/new
    app.state.ws_tasks = set()
    app.state.engines = engine_registry or EngineRegistry()
    app.state.selected_engine_id = None

    app.include_router(settings_api.router)
    app.include_router(engines_api.router)
    app.include_router(game_api.router)
    app.include_router(agent_api.router)
    app.include_router(ws_api.router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        target = "/ui/" if settings.auth_disabled else f"/ui/?token={settings.token}"
        return RedirectResponse(url=target)

    if settings.web_dir.is_dir():
        app.mount("/ui", StaticFiles(directory=settings.web_dir, html=True), name="ui")
    else:
        log.warning("web_dir %s does not exist; UI will not be served", settings.web_dir)

    _print_banner(settings)
    return app


def _reachable_hosts(bind: str) -> list[str]:
    """For wildcard binds, list non-loopback IPv4 addresses on up interfaces.
    For specific binds, return just the bind address.
    """
    if bind not in ("0.0.0.0", "::", ""):
        return [bind]

    stats = psutil.net_if_stats()
    candidates: list[str] = []
    for name, addrs in psutil.net_if_addrs().items():
        nic = stats.get(name)
        if nic is None or not nic.isup:
            continue
        for a in addrs:
            ip = a.address
            if a.family.name != "AF_INET":
                continue
            if not ip or ip.startswith("127.") or ip.startswith("169.254."):
                continue
            if ip not in candidates:
                candidates.append(ip)
    candidates.append("127.0.0.1")
    return candidates


def _print_banner(settings: Settings) -> None:
    hosts = _reachable_hosts(settings.host)
    print(f"sturddle-view: bound on {settings.host}:{settings.port}", file=sys.stderr)
    if settings.auth_disabled:
        print("auth: DISABLED (--no-auth)", file=sys.stderr)
    print("open one of:", file=sys.stderr)
    for h in hosts:
        suffix = "" if settings.auth_disabled else f"?token={settings.token}"
        print(f"  http://{h}:{settings.port}/{suffix}", file=sys.stderr)
    sys.stderr.flush()
