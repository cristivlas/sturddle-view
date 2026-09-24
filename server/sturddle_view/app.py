from __future__ import annotations

import asyncio
import hmac
import logging
import socket
from asyncio import proactor_events, trsock
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import chess.engine
from fastapi import FastAPI, Query, Request, status
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

try:
    import wsproto
    from uvicorn.protocols.websockets import wsproto_impl
    from wsproto.connection import ConnectionState
except ImportError:  # optional: uvicorn falls back to its websockets impl
    wsproto = wsproto_impl = ConnectionState = None

from . import APP_NAME, __version__, is_windows
from . import llm as llm_pkg
from .auth import AUTH_COOKIE, AUTH_PATH, AUTH_TOKEN_PARAM, INVALID_TOKEN_DETAIL, origin_ok
from .api import chess_utils as chess_api
from .api import connect as connect_api
from .api import engines as engines_api
from .api import fs as fs_api
from .api import game as game_api
from .api import openings as openings_api
from .api import settings as settings_api
from .api import test_hooks as test_hooks_api
from .api import tournaments as tournaments_api
from .api import ws as ws_api
from .api._ai_kick import ai_coordinator
from .config import (
    LOOPBACK_HOST,
    PROVIDER_ANTHROPIC,
    PROVIDER_GEMINI,
    PROVIDER_OLLAMA,
    ROOT_PATH,
    Settings,
)
from .engine_tmp import sweep_orphans
from .engines import EngineRegistry, resolve_selected
from .env_utils import env_bool, env_int
from .events import EVT_REMOTE_CONNECTED, Event, EventBus
from .llm import CannedProvider, LLMProvider, ToolRegistry
from .llm.anthropic import AnthropicProvider
from .llm.gemini import DEFAULT_BASE_URL as _DEFAULT_GEMINI_BASE_URL, GeminiProvider
from .llm.ollama import DEFAULT_BASE_URL as _DEFAULT_OLLAMA_BASE_URL, OllamaProvider
from .netinfo import SCHEME_HTTPS, entry_url, is_this_machine, reachable_hosts, url_scheme
from .openings import OpeningBook
from .play.ai_analysis import (
    AIAnalysisCoordinator,
    DELEGATE_TOOL_SPEC,
    make_delegate_tool,
)
from .play.engine_analysis import make_analysis_supervisor
from .play.engine_supervisor import EngineSupervisor
from .play.game_store import GameStore
from .play.human_vs_engine import HumanVsEngine, live_hve
from .play.tools_engine import (
    ANALYZE_TOOL_SPEC,
    MATERIAL_TOOL_SPEC,
    PIECE_AT_TOOL_SPEC,
    RECOMMEND_MOVE_TOOL_SPEC,
    REPORT_LINE_TOOL_SPEC,
    TACTICS_TOOL_SPEC,
    TOP_MOVES_TOOL_SPEC,
    VALIDATE_MOVE_TOOL_SPEC,
    SearchCache,
    make_analyze_tool,
    make_material_tool,
    make_piece_at_tool,
    make_recommend_move_tool,
    make_recommend_verifier,
    make_report_line_tool,
    make_tactics_tool,
    make_top_moves_tool,
    make_validate_move_tool,
)
from .play.tools_openings import (
    RELATED_OPENINGS_TOOL_SPEC,
    make_related_openings_tool,
)
from .recent_imports import RecentImports
from .tournament.fastchess import FastchessRunner
from .tournament.orchestrator import Orchestrator, wrap_event_for_bus
from .tournament.store import TournamentStore, default_root

log = logging.getLogger(__name__)

_UI_PATH = "/ui"
_UI_ENTRY = f"{_UI_PATH}/"
_AUTH_COOKIE_MAX_AGE_S = env_int(
    "SV_AUTH_COOKIE_MAX_AGE_S", int(timedelta(weeks=1).total_seconds()), min_value=1,
)
_AI_DEBUG_ENV = "SV_AI_DEBUG"
_READY_PORT_ENV = "SV_READY_PORT"
# SV_READY_PORT unset: not launched by the test harness.
_NO_READY_PORT = 0
_READY_SIGNAL_TIMEOUT_S = 2.0
# WebSocket close code 1012 "service restart".
_WS_CLOSE_SERVICE_RESTART = 1012
# cpython's own default listen backlog for loop.create_server.
_DEFAULT_ACCEPT_BACKLOG = 100
_ACCEPT_FAILED_MSG = "Accept failed on a socket"
# asyncio exception-handler context key.
_CTX_EXCEPTION = "exception"


class _NoCacheUIMiddleware(BaseHTTPMiddleware):
    # Stamp /ui/* and / responses with cache-busting headers so browsers
    # don't serve stale HTML/JS/CSS after an upgrade.
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == ROOT_PATH or path.startswith(_UI_PATH):
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
            return PlainTextResponse("bad origin", status_code=status.HTTP_403_FORBIDDEN)
        return await call_next(request)


# WinError codes asyncio-on-Windows mishandles on the listener socket
# when a Job-killed proxy aborts mid-AcceptEx. Stock cpython closes the
# listener -- we re-arm instead and silence the orphan-task trace.
_TRANSIENT_ACCEPT_WINERR = {64, 1236, 10054}  # NETNAME_DELETED, ABORTED, RST


def _winerror(exc: OSError) -> int | None:
    """Windows error code of an OSError (None elsewhere)."""
    return getattr(exc, "winerror", None)


def _install_proactor_accept_resilience() -> None:
    """Re-arm Windows AcceptEx on transient OSErrors instead of dropping
    the listener (stock cpython closes the socket on any accept OSError)."""
    if not is_windows():
        return

    def _start_serving(self, protocol_factory, sock,
                       sslcontext=None, server=None, backlog=_DEFAULT_ACCEPT_BACKLOG,
                       ssl_handshake_timeout=None,
                       ssl_shutdown_timeout=None):
        def fail(exc: BaseException) -> None:
            if sock.fileno() != -1:
                self.call_exception_handler({
                    "message": _ACCEPT_FAILED_MSG,
                    _CTX_EXCEPTION: exc,
                    "socket": trsock.TransportSocket(sock),
                })
                sock.close()

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
                winerr = _winerror(exc)
                if sock.fileno() != -1 and winerr in _TRANSIENT_ACCEPT_WINERR:
                    log.debug("transient AcceptEx WinError %s; re-arming", winerr)
                    self.call_soon(accept_loop)
                    return
                fail(exc)
            except (SystemExit, KeyboardInterrupt):
                raise
            except BaseException as exc:
                fail(exc)
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
    if _ws_shutdown_patched or wsproto is None:
        return

    def _safe_shutdown(self):
        self.stop_keepalive()
        if self.handshake_complete and self.conn.state != ConnectionState.CLOSED:
            self.queue.put_nowait(
                {"type": "websocket.disconnect", "code": _WS_CLOSE_SERVICE_RESTART}
            )
            output = self.conn.send(
                wsproto.events.CloseConnection(code=_WS_CLOSE_SERVICE_RESTART)
            )
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
        exc = ctx.get(_CTX_EXCEPTION)
        if isinstance(exc, chess.engine.EngineTerminatedError):
            return
        if isinstance(exc, OSError) and _winerror(exc) in _TRANSIENT_ACCEPT_WINERR:
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
    # Orphaned engine temp dirs (crashed/killed engines from a previous
    # run) -- safe pre-spawn: no engine can be running yet.
    try:
        swept = sweep_orphans()
        if swept:
            log.info("engine temp sweep: removed %d orphan dir(s)", swept)
    except Exception:
        log.error("engine temp sweep failed", exc_info=True)
    _maybe_restore_game(app)
    # Tournament reconciliation: any 'running' rows on disk are stale.
    # There is no Resume -- mark them stopped.
    try:
        reconciled = app.state.tournament_orch.reconcile_on_startup()
        for t in reconciled:
            log.info("reconciled stale running tournament: %s (%s)", t.id, t.name)
    except Exception:
        log.error("tournament reconcile failed", exc_info=True)
    _signal_ready_port()
    yield
    # Best-effort: stop any active tournament on shutdown.
    try:
        active = app.state.tournament_orch.active_id()
        if active is not None:
            await app.state.tournament_orch.stop(active)
    except Exception:
        log.error("tournament shutdown stop failed", exc_info=True)
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
    hve.apply_launch(launch)
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
    app = FastAPI(title=APP_NAME, version=__version__, lifespan=_lifespan)

    app.state.settings = settings
    # Desktop mode installs a LanListener; server mode has a fixed bind.
    app.state.lan_listener = None
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
        lambda: app.state.hve.game_id if app.state.hve is not None else None
    )
    log.info(
        "recent imports: %d entries at %s",
        len(app.state.recent_imports.list()),
        app.state.recent_imports.root,
    )
    app.state.openings = OpeningBook.load()
    log.info("loaded %d opening lines", len(app.state.openings))

    _setup_ai(app)
    _setup_tournament(app, settings)

    app.include_router(chess_api.router)
    app.include_router(connect_api.router)
    app.include_router(settings_api.router)
    app.include_router(engines_api.router)
    app.include_router(fs_api.router)
    app.include_router(game_api.router)
    app.include_router(openings_api.router)
    app.include_router(tournaments_api.router)
    app.include_router(tournaments_api.internal_router)
    app.include_router(ws_api.router)
    if settings.test_mode:
        app.include_router(test_hooks_api.router)
        log.info("test-mode endpoints (/_test/*) mounted")

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"ok": True}

    async def _announce_remote(request: Request) -> None:
        """A phone scanned the About-dialog QR: let that dialog close itself."""
        client = request.client
        if client is not None and not is_this_machine(client.host):
            await app.state.event_bus.publish(
                Event(kind=EVT_REMOTE_CONNECTED, payload={"host": client.host})
            )

    @app.get(ROOT_PATH, include_in_schema=False)
    async def root(request: Request) -> RedirectResponse:
        # Token-less redirect; client carries the cookie set by /auth.
        # With --no-auth this is the entry URL itself (netinfo.entry_url).
        if settings.auth_disabled:
            await _announce_remote(request)
        return RedirectResponse(url=_UI_ENTRY)

    @app.get(AUTH_PATH, include_in_schema=False)
    async def auth_handshake(
        request: Request, token: str = Query("", alias=AUTH_TOKEN_PARAM),
    ) -> RedirectResponse:
        """One-shot handshake: validate ?token=, set HttpOnly cookie, redirect
        to a token-less URL so the token never appears in history or Referer.
        """
        if not settings.auth_disabled:
            if not token or not hmac.compare_digest(token, settings.token):
                return PlainTextResponse(
                    INVALID_TOKEN_DETAIL, status_code=status.HTTP_401_UNAUTHORIZED,
                )
        await _announce_remote(request)
        resp = RedirectResponse(url=_UI_ENTRY, status_code=status.HTTP_303_SEE_OTHER)
        if not settings.auth_disabled:
            secure = request.url.scheme == SCHEME_HTTPS
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
                path=ROOT_PATH,
                max_age=_AUTH_COOKIE_MAX_AGE_S,
            )
        return resp

    if settings.web_dir.is_dir():
        app.mount(_UI_PATH, StaticFiles(directory=settings.web_dir, html=True), name="ui")
    else:
        log.warning("web_dir %s does not exist; UI will not be served", settings.web_dir)

    app.add_middleware(_NoCacheUIMiddleware)
    app.add_middleware(_SecurityHeadersMiddleware)
    app.add_middleware(_OriginMiddleware)

    _print_banner(settings)
    return app


def _setup_ai(app: FastAPI) -> None:
    # AI analysis: tool registries, the per-turn provider factory, and the
    # coordinator. Engine-backed tools resolve the current engine on every
    # call, so engine swaps in Settings are honored without rebuilding.
    def _ai_engine_launcher() -> EngineSupervisor:
        return make_analysis_supervisor(
            app.state.engines, app.state.settings, app.state.event_bus,
        )

    def _ai_game_id_provider() -> str | None:
        hve = live_hve(app.state)
        return hve.game_id if hve else None

    def _ai_board_provider():
        hve = live_hve(app.state)
        return hve.current_board() if hve else None

    def _ai_book_provider():
        return getattr(app.state, "openings", None)

    def _ai_settings_provider():
        return app.state.settings

    def _ai_book_move_provider():
        coord = ai_coordinator(app.state)
        return coord.turn_book_move() if coord is not None else None

    # Side-to-move Situation for the recommend_move gate: the pick under
    # check is the mover's, whichever persona asked for it.
    def _ai_situation_provider():
        hve = live_hve(app.state)
        board = hve.current_board() if hve else None
        return hve.situation(board.turn) if board is not None else None

    # Shared across all engine-backed tools so a position searched once
    # this turn (analyze, top_moves, recommend_move, the verifier) isn't
    # re-searched. The coordinator clears it at turn start.
    ai_search_cache = SearchCache()

    # Strict narrator/verifier split (docs/ai-analysis-spec.md):
    # the narrator plans + delegates + recommends but never searches; the
    # verifier sub-run owns all engine tools. This keeps raw eval numbers
    # out of the narrator's context (structural fix for engine over-trust)
    # and the verifier's searches off the UI.
    ai_verifier_registry = ToolRegistry()
    ai_verifier_registry.register(
        ANALYZE_TOOL_SPEC,
        make_analyze_tool(
            _ai_engine_launcher,
            bus=app.state.event_bus,
            game_id_provider=_ai_game_id_provider,
            settings_provider=_ai_settings_provider,
            search_cache=ai_search_cache,
        ),
    )
    ai_verifier_registry.register(
        PIECE_AT_TOOL_SPEC,
        make_piece_at_tool(board_provider=_ai_board_provider),
    )
    ai_verifier_registry.register(
        VALIDATE_MOVE_TOOL_SPEC,
        make_validate_move_tool(board_provider=_ai_board_provider),
    )
    ai_verifier_registry.register(
        MATERIAL_TOOL_SPEC,
        make_material_tool(),
    )
    ai_verifier_registry.register(
        TACTICS_TOOL_SPEC,
        make_tactics_tool(),
    )
    ai_verifier_registry.register(
        TOP_MOVES_TOOL_SPEC,
        make_top_moves_tool(
            _ai_engine_launcher,
            bus=app.state.event_bus,
            board_provider=_ai_board_provider,
            game_id_provider=_ai_game_id_provider,
            settings_provider=_ai_settings_provider,
            search_cache=ai_search_cache,
        ),
    )

    # Narrator registry: recommend_move now; delegate registered below,
    # once the coordinator exists (it owns the verifier sub-run).
    ai_registry = ToolRegistry()
    ai_registry.register(
        RECOMMEND_MOVE_TOOL_SPEC,
        make_recommend_move_tool(
            _ai_engine_launcher,
            bus=app.state.event_bus,
            board_provider=_ai_board_provider,
            game_id_provider=_ai_game_id_provider,
            settings_provider=_ai_settings_provider,
            search_cache=ai_search_cache,
            book_move_provider=_ai_book_move_provider,
            situation_provider=_ai_situation_provider,
        ),
    )
    # top_moves: the narrator's one-call way to rank its candidate moves
    # against each other, so it weighs alternatives without probing them
    # one at a time through recommend_move.
    ai_registry.register(
        TOP_MOVES_TOOL_SPEC,
        make_top_moves_tool(
            _ai_engine_launcher,
            bus=app.state.event_bus,
            board_provider=_ai_board_provider,
            game_id_provider=_ai_game_id_provider,
            settings_provider=_ai_settings_provider,
            search_cache=ai_search_cache,
        ),
    )
    # report_line: structured grounding for the narrator -- pure legality
    # replay of a model-supplied line (no engine), returning SAN + per-ply
    # FENs the model can reason from before naming the line in prose.
    ai_registry.register(
        REPORT_LINE_TOOL_SPEC,
        make_report_line_tool(board_provider=_ai_board_provider),
    )
    # Board-read grounding (no engine, no eval): the rules block tells the
    # narrator to settle squares and legality before naming them in prose.
    ai_registry.register(
        PIECE_AT_TOOL_SPEC,
        make_piece_at_tool(board_provider=_ai_board_provider),
    )
    ai_registry.register(
        VALIDATE_MOVE_TOOL_SPEC,
        make_validate_move_tool(board_provider=_ai_board_provider),
    )
    # Pins and forks the prose may name (docs/ai-analysis-spec.md,
    # Tactical grounding); board read only.
    ai_registry.register(
        TACTICS_TOOL_SPEC,
        make_tactics_tool(),
    )
    # Opening-context grounding (no engine, no eval): real sibling lines from
    # the vendored dataset so a variation contrast cites the book, not memory.
    # Narrator-only -- the verifier red-teams moves, not opening theory.
    ai_registry.register(
        RELATED_OPENINGS_TOOL_SPEC,
        make_related_openings_tool(
            book_provider=_ai_book_provider,
            board_provider=_ai_board_provider,
        ),
    )
    app.state.ai_tool_registry = ai_registry
    app.state.ai_verifier_registry = ai_verifier_registry

    # SV_AI_DEBUG=1: flip the AI loggers to DEBUG so the system prompt,
    # user message, text deltas, and tool calls are visible. Off by
    # default; meant to be set when diagnosing a misbehaving model.
    if env_bool(_AI_DEBUG_ENV, False):
        logging.getLogger(llm_pkg.__name__).setLevel(logging.DEBUG)
        logging.getLogger(AIAnalysisCoordinator.__module__).setLevel(logging.DEBUG)

    def _ai_provider_factory() -> LLMProvider:
        s = app.state.settings
        provider_name = (s.ai_provider or "").lower()
        if provider_name == PROVIDER_OLLAMA:
            return OllamaProvider(
                base_url=(s.ai_base_url or _DEFAULT_OLLAMA_BASE_URL),
                model=s.ai_model,
                thinking_enabled=s.ai_thinking_enabled,
            )
        if provider_name == PROVIDER_GEMINI:
            # OpenAI-compatible SSE provider against Google's API. Bearer
            # auth from the keyring/env key. Base URL is fixed to Google's
            # endpoint -- ai_base_url is Ollama's field (the UI hides the
            # URL row for key-based providers), so reading it here would
            # wrongly point Gemini at the user's Ollama daemon.
            return GeminiProvider(
                api_key=s.ai_api_key,
                model=s.ai_model,
                base_url=_DEFAULT_GEMINI_BASE_URL,
                thinking_enabled=s.ai_thinking_enabled,
            )
        if provider_name == PROVIDER_ANTHROPIC:
            # Live SSE provider (stream() POSTs /v1/messages). Raises
            # RuntimeError if the API key is unset or the API returns
            # non-200 -- surfaced as a done/error event on the bus.
            return AnthropicProvider(
                api_key=s.ai_api_key,
                model=s.ai_model,
                thinking_enabled=s.ai_thinking_enabled,
                thinking_budget_tokens=s.ai_thinking_budget_tokens,
            )
        # Unknown / unset provider: canned stand-in so the pipeline
        # still flows end-to-end. Picked by tests that don't care which
        # provider runs.
        return CannedProvider()

    app.state.ai_provider_factory = _ai_provider_factory
    _ai_recommend_verifier = make_recommend_verifier(
        _ai_engine_launcher,
        bus=app.state.event_bus,
        board_provider=_ai_board_provider,
        game_id_provider=_ai_game_id_provider,
        settings_provider=_ai_settings_provider,
        search_cache=ai_search_cache,
    )
    app.state.ai_coordinator = AIAnalysisCoordinator(
        app.state.event_bus, CannedProvider(), registry=ai_registry,
        board_provider=_ai_board_provider,
        recommend_verifier=_ai_recommend_verifier,
        verifier_registry=ai_verifier_registry,
        search_cache=ai_search_cache,
    )
    # Register `delegate` last: it dispatches to the coordinator's verifier
    # sub-run, so the coordinator must exist first.
    ai_registry.register(
        DELEGATE_TOOL_SPEC,
        make_delegate_tool(
            app.state.ai_coordinator.delegate_runner(),
            board_provider=_ai_board_provider,
        ),
    )


def _setup_tournament(app: FastAPI, settings: Settings) -> None:
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

    # Tell the orchestrator where the proxy should POST. The proxy runs as
    # a subprocess on this same host; loopback only.
    proxy_path = tournaments_api.internal_router.url_path_for(
        tournaments_api.ingest_proxy.__name__
    )
    proxy_url = f"{url_scheme(settings)}://{LOOPBACK_HOST}:{settings.port}{proxy_path}"
    app.state.tournament_orch.set_proxy_broadcast_url(proxy_url)
    # Live settings reference so each tournament start picks up the
    # current Defaults-tab values without needing a restart.
    app.state.tournament_orch.set_settings(settings)


def _print_banner(settings: Settings) -> None:
    log.info("bound on %s:%d%s", settings.host, settings.port,
             " (auth DISABLED)" if settings.auth_disabled else "")
    for h in reachable_hosts(settings.host):
        log.info("  open: %s", entry_url(settings, h))


def _signal_ready_port() -> None:
    """Test hook: connect once to SV_READY_PORT to signal startup completion.

    The test harness opens a listening socket on an ephemeral port before
    spawning this subprocess and passes the port via SV_READY_PORT. By
    connecting here (after lifespan startup, before uvicorn enters the
    serve loop), the harness gets a deterministic ready event with no
    polling. Silent no-op when the env var is unset or unreachable.
    """
    port = env_int(_READY_PORT_ENV, _NO_READY_PORT, min_value=1)
    if port == _NO_READY_PORT:
        return
    try:
        socket.create_connection((LOOPBACK_HOST, port), timeout=_READY_SIGNAL_TIMEOUT_S).close()
    except OSError:
        pass
