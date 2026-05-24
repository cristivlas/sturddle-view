from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import chess.engine
import hmac
import psutil
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from . import __version__
from .auth import AUTH_COOKIE, origin_ok
from .api import agent as agent_api
from .api import ai as ai_api
from .api import chess_utils as chess_api
from .api import engines as engines_api
from .api import fs as fs_api
from .api import game as game_api
from .api import settings as settings_api
from .api import tournaments as tournaments_api
from .api import ws as ws_api
from .config import Settings
from .engines import EngineRegistry, resolve_selected
from .events import Event, EventBus
from .llm import CannedProvider
from .openings import OpeningBook
from .play.ai_analysis import AIAnalysisCoordinator
from .play.game_store import GameStore
from .play.human_vs_engine import HumanVsEngine
from .recent_imports import RecentImports
from .tournament.fastchess import FastchessRunner
from .tournament.orchestrator import Orchestrator, wrap_event_for_bus
from .tournament.store import TournamentStore, default_root

log = logging.getLogger(__name__)


class _NoCacheUIMiddleware(BaseHTTPMiddleware):
    # Stamp /ui/* and / responses with cache-busting headers so browsers
    # don't serve stale HTML/JS/CSS after an upgrade.
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/ui"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    # Same-origin app: lock framing, strip Referer, restrict resource origins.
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        # connect-src includes data: so component libraries that fetch
        # inline-SVG icon payloads (e.g. webawesome) work without warnings.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; "
            "connect-src 'self' data: ws: wss:; "
            "frame-ancestors 'none'",
        )
        return response


class _OriginMiddleware(BaseHTTPMiddleware):
    # CSRF / DNS-rebinding mitigation: state-changing REST must come from
    # an Origin matching the server's own. Same-origin browser fetches and
    # loopback CLI tools (no Origin header) are unaffected.
    SAFE = {"GET", "HEAD", "OPTIONS"}

    async def dispatch(self, request: Request, call_next):
        if request.method not in self.SAFE and not origin_ok(request):
            return PlainTextResponse("bad origin", status_code=403)
        return await call_next(request)


# WinError codes asyncio-on-Windows mishandles on the listener socket
# when a Job-killed proxy aborts mid-AcceptEx. Stock cpython closes the
# listener — we re-arm instead and silence the orphan-task trace.
_TRANSIENT_ACCEPT_WINERR = {64, 1236, 10054}  # NETNAME_DELETED, ABORTED, RST


def _install_proactor_accept_resilience() -> None:
    """Re-arm Windows AcceptEx on transient OSErrors instead of dropping
    the listener (stock cpython closes the socket on any accept OSError)."""
    if sys.platform != "win32":
        return
    from asyncio import proactor_events
    from asyncio import trsock

    def _start_serving(self, protocol_factory, sock,
                       sslcontext=None, server=None, backlog=100,
                       ssl_handshake_timeout=None,
                       ssl_shutdown_timeout=None):
        def accept_loop(f=None):
            try:
                if f is not None:
                    conn, addr = f.result()
                    protocol = protocol_factory()
                    if sslcontext is not None:
                        self._make_ssl_transport(
                            conn, protocol, sslcontext,
                            waiter=None, server_side=True, server=server,
                            ssl_handshake_timeout=ssl_handshake_timeout,
                            ssl_shutdown_timeout=ssl_shutdown_timeout,
                        )
                    else:
                        self._make_socket_transport(conn, protocol,
                                                    waiter=None, server=server)
                f = self._proactor.accept(sock)
            except OSError as exc:
                winerr = getattr(exc, "winerror", None)
                if sock.fileno() != -1 and winerr in _TRANSIENT_ACCEPT_WINERR:
                    log.debug("transient AcceptEx WinError %s; re-arming", winerr)
                    self.call_soon(accept_loop)
                    return
                if sock.fileno() != -1:
                    self.call_exception_handler({
                        "message": "Accept failed on a socket",
                        "exception": exc,
                        "socket": trsock.TransportSocket(sock),
                    })
                    sock.close()
            except (SystemExit, KeyboardInterrupt):
                raise
            except BaseException as exc:
                if sock.fileno() != -1:
                    self.call_exception_handler({
                        "message": "Accept failed on a socket",
                        "exception": exc,
                        "socket": trsock.TransportSocket(sock),
                    })
                    sock.close()
            else:
                f.add_done_callback(accept_loop)

        self.call_soon(accept_loop)

    proactor_events.BaseProactorEventLoop._start_serving = _start_serving


_ws_shutdown_patched = False


def _install_uvicorn_ws_shutdown_state_guard() -> None:
    """Gate uvicorn's WSProtocol.shutdown on wsproto state.

    Upstream uvicorn's WSProtocol.shutdown() sends CloseConnection(1012)
    without checking conn.state. When a client cleanly closes right
    before server shutdown iterates connections, state is already CLOSED
    and the send raises wsproto LocalProtocolError on the loop thread.
    Idempotent and applied once per process.
    """
    global _ws_shutdown_patched
    if _ws_shutdown_patched:
        return
    try:
        import wsproto
        from uvicorn.protocols.websockets import wsproto_impl
        from wsproto.connection import ConnectionState
    except ImportError:
        return

    def _safe_shutdown(self):
        self.stop_keepalive()
        if self.handshake_complete and self.conn.state != ConnectionState.CLOSED:
            self.queue.put_nowait({"type": "websocket.disconnect", "code": 1012})
            output = self.conn.send(wsproto.events.CloseConnection(code=1012))
            self.transport.write(output)
        elif not self.handshake_complete:
            self.send_500_response()
        self.transport.close()

    wsproto_impl.WSProtocol.shutdown = _safe_shutdown
    _ws_shutdown_patched = True


def _install_engine_sigkill_filter() -> None:
    # Drop "Future exception was never retrieved" from intentional engine
    # termination on takeback/cancel (transport.close() after failed stop),
    # plus orphan accept_coro tasks from transient AcceptEx WinErrors
    # (see _install_proactor_accept_resilience).
    loop = asyncio.get_running_loop()
    prev = loop.get_exception_handler()

    def handler(loop_, ctx):
        exc = ctx.get("exception")
        if isinstance(exc, chess.engine.EngineTerminatedError):
            return
        if isinstance(exc, OSError) and getattr(exc, "winerror", None) in _TRANSIENT_ACCEPT_WINERR:
            return
        if prev is not None:
            prev(loop_, ctx)
        else:
            loop_.default_exception_handler(ctx)

    loop.set_exception_handler(handler)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _install_proactor_accept_resilience()
    _install_uvicorn_ws_shutdown_state_guard()
    _install_engine_sigkill_filter()
    _maybe_restore_game(app)
    # Tournament reconciliation: any 'running' rows on disk are stale.
    # Phase 1 has no Resume -- mark them stopped.
    try:
        reconciled = app.state.tournament_orch.reconcile_on_startup()
        for t in reconciled:
            log.info("reconciled stale running tournament: %s (%s)", t.id, t.name)
    except Exception:
        log.exception("tournament reconcile failed")
    _signal_ready_port()
    yield
    # Best-effort: stop any active tournament on shutdown.
    try:
        active = app.state.tournament_orch.active_id()
        if active is not None:
            await app.state.tournament_orch.stop(active)
    except Exception:
        log.exception("tournament shutdown stop failed")
    for task in list(app.state.ws_tasks):
        task.cancel()
    if app.state.ws_tasks:
        await asyncio.gather(*app.state.ws_tasks, return_exceptions=True)
    if app.state.hve is not None:
        await app.state.hve.shutdown()


def _maybe_restore_game(app: FastAPI) -> None:
    """Rehydrate the saved game (if any) into app.state.hve.

    Skipped when no engine is resolvable (registry has no selection AND
    no fallback --engine path), because HumanVsEngine needs a path to
    eventually spawn the engine on the next move. The saved file is left
    in place so a later engine selection still picks it up.
    """
    s = app.state
    state = s.game_store.load()
    if state is None:
        return
    launch = resolve_selected(s.engines, s.settings)
    if launch.path is None:
        log.warning(
            "saved game found but no engine is configured; "
            "register and select one to resume"
        )
        return
    hve = HumanVsEngine(
        launch.path,
        s.event_bus,
        openings=s.openings,
        settings=s.settings,
        store=s.game_store,
        recents=s.recent_imports,
    )
    hve.restore_from(state)
    # Seed name + UCI options from the registry so a client reconnecting
    # before any move sees the same label, and the engine spawns with the
    # user's saved options on its first invocation.
    hve.set_engine_name(launch.name)
    hve.set_engine_options(launch.options)
    hve.set_engine_args(launch.args)
    hve.set_engine_env(launch.env)
    s.hve = hve
    log.info("restored saved game (%d plies)", len(state.moves_uci))


def create_app(
    settings: Settings | None = None,
    *,
    engine_registry: EngineRegistry | None = None,
    game_store: GameStore | None = None,
    recent_imports: RecentImports | None = None,
) -> FastAPI:
    settings = settings or Settings()
    settings.apply_persisted()
    app = FastAPI(title="sturddle-view", version=__version__, lifespan=_lifespan)

    app.state.settings = settings
    app.state.event_bus = EventBus()
    app.state.hve = None  # lazy: HumanVsEngine, created on first /game/new
    app.state.ws_tasks = set()
    app.state.engines = engine_registry or EngineRegistry()
    app.state.game_store = game_store or GameStore()
    app.state.recent_imports = recent_imports or RecentImports.load()
    # Pin the active HVE session's game_id during eviction so the live
    # game is never dropped from the store. HVE is lazy (created on
    # first /game/new), so resolve it through app.state on each call.
    app.state.recent_imports.set_active_game_id_getter(
        lambda: getattr(app.state.hve, "game_id", None)
    )
    log.info(
        "recent imports: %d entries at %s",
        len(app.state.recent_imports.list()),
        app.state.recent_imports.root,
    )
    app.state.openings = OpeningBook.load()
    log.info("loaded %d opening lines", len(app.state.openings))

    # Walking-skeleton: coordinator wired with the canned provider so the
    # end-to-end transport runs without any LLM credentials. Real provider
    # selection (anthropic/ollama from settings) lands in a later cycle.
    app.state.ai_coordinator = AIAnalysisCoordinator(
        app.state.event_bus, CannedProvider()
    )

    # Tournament subsystem: store + runner + orchestrator. Wired even
    # when fastchess isn't installed; the Tournaments UI surfaces an
    # empty-state until a binary is configured.
    t_root = settings.tournament_root or str(default_root())
    app.state.tournament_store = TournamentStore(Path(t_root))
    app.state.tournament_runner = FastchessRunner(
        binary_path=settings.tournament_fastchess_path
    )
    app.state.tournament_orch = Orchestrator(
        app.state.tournament_store, app.state.tournament_runner
    )

    async def _tournament_broadcast(kind: str, payload: dict) -> None:
        # Map orchestrator events onto the existing EventBus. The wrap
        # helper is shared with the history-replay REST endpoint so
        # both sources deliver the same shape to the client.
        wrapped = wrap_event_for_bus(kind, payload)
        await app.state.event_bus.publish(
            Event(kind=wrapped["kind"], payload=wrapped["payload"])
        )

    app.state.tournament_orch.set_broadcast(_tournament_broadcast)

    # Slice 9b: tell the orchestrator where the proxy should POST.
    # The proxy runs as a subprocess on this same host; loopback only.
    proxy_scheme = "https" if settings.tls_cert else "http"
    proxy_url = f"{proxy_scheme}://127.0.0.1:{settings.port}/internal/proxy"
    app.state.tournament_orch.set_proxy_broadcast_url(proxy_url)
    # Live settings reference so each tournament start picks up the
    # current Defaults-tab values without needing a restart.
    app.state.tournament_orch.set_settings(settings)

    app.include_router(chess_api.router)
    app.include_router(settings_api.router)
    app.include_router(engines_api.router)
    app.include_router(fs_api.router)
    app.include_router(game_api.router)
    app.include_router(agent_api.router)
    app.include_router(ai_api.router)
    app.include_router(tournaments_api.router)
    app.include_router(tournaments_api.internal_router)
    app.include_router(ws_api.router)
    if settings.test_mode:
        from .api import test_hooks as test_hooks_api
        app.include_router(test_hooks_api.router)
        log.info("test-mode endpoints (/_test/*) mounted")

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        # Token-less redirect; client carries the cookie set by /auth.
        return RedirectResponse(url="/ui/")

    @app.get("/auth", include_in_schema=False)
    def auth_handshake(request: Request, token: str = "") -> RedirectResponse:
        """One-shot handshake: validate ?token=, set HttpOnly cookie, redirect
        to a token-less URL so the token never appears in history or Referer.
        """
        if not settings.auth_disabled:
            if not token or not hmac.compare_digest(token, settings.token):
                return PlainTextResponse("invalid token", status_code=401)
        resp = RedirectResponse(url="/ui/", status_code=303)
        if not settings.auth_disabled:
            secure = request.url.scheme == "https"
            # SameSite=Lax: still defeats CSRF (cross-site POSTs strip the
            # cookie) but lets top-level GET navigations (including the 303
            # from /auth) carry it. Strict breaks the handshake in some
            # embedded webviews.
            resp.set_cookie(
                AUTH_COOKIE,
                settings.token,
                httponly=True,
                samesite="lax",
                secure=secure,
                path="/",
                max_age=60 * 60 * 24 * 7,
            )
        return resp

    if settings.web_dir.is_dir():
        app.mount("/ui", StaticFiles(directory=settings.web_dir, html=True), name="ui")
    else:
        log.warning("web_dir %s does not exist; UI will not be served", settings.web_dir)

    app.add_middleware(_NoCacheUIMiddleware)
    app.add_middleware(_SecurityHeadersMiddleware)
    app.add_middleware(_OriginMiddleware)

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
    scheme = "https" if settings.tls_cert else "http"
    log.info("bound on %s:%d%s", settings.host, settings.port,
             " (auth DISABLED)" if settings.auth_disabled else "")
    for h in hosts:
        path = "/" if settings.auth_disabled else f"/auth?token={settings.token}"
        log.info("  open: %s://%s:%d%s", scheme, h, settings.port, path)


def _signal_ready_port() -> None:
    """Test hook: connect once to SV_READY_PORT to signal startup completion.

    The test harness opens a listening socket on an ephemeral port before
    spawning this subprocess and passes the port via SV_READY_PORT. By
    connecting here (after lifespan startup, before uvicorn enters the
    serve loop), the harness gets a deterministic ready event with no
    polling. Silent no-op when the env var is unset or unreachable.
    """
    port_s = os.environ.get("SV_READY_PORT")
    if not port_s:
        return
    try:
        port = int(port_s)
    except ValueError:
        return
    try:
        socket.create_connection(("127.0.0.1", port), timeout=2.0).close()
    except OSError:
        pass
