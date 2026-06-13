"""REST + WS surface for the tournament subsystem.

Live events flow on the shared ``EventBus`` /ws channel
(``tournament_status`` / ``tournament_update``); per-proxy line streams
have their own ``/ws/tournament/proxy/...`` channel.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Path as FastApiPath,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from ..auth import AUTH_COOKIE, check_token_value, origin_ok, require_token
from ..engines import InvalidLaunchProfileError, validate_launch_profile
from ..events import ENVELOPE_KIND, ENVELOPE_PAYLOAD
from ..tournament.fastchess import FastchessRunner
from ..tournament.orchestrator import Orchestrator, TournamentBusyError, wrap_event_for_bus
from ..tournament.rescheck import RescheckError, check as rescheck_run
from ..tournament.uci_parse import parse_uci_line
from ..tournament.pgn_stats import (
    compute_games_list,
    compute_sprt,
    compute_standings,
    count_partial_pairs,
    read_game_record,
)
from ..tournament.store import (
    CorruptStateError,
    DuplicateNameError,
    STATUS_FAILED,
    STATUS_STOPPED,
    TournamentNotFoundError,
    TournamentStore,
)


log = logging.getLogger(__name__)


router = APIRouter(tags=["tournaments"], dependencies=[Depends(require_token)])

# Internal router: no user-token auth on REST (proxy uses a per-tournament
# secret). WS endpoints are authenticated by cookie / Bearer / ?token= and
# must pass the Origin check.
internal_router = APIRouter(tags=["tournaments-internal"])


def _check_ws_auth(websocket: WebSocket, settings) -> bool:
    if not origin_ok(websocket):
        return False
    presented = websocket.cookies.get(AUTH_COOKIE)
    if not presented:
        auth = websocket.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            presented = auth.split(None, 1)[1].strip()
    return check_token_value(settings, presented)


class EngineRef(BaseModel):
    id: str
    name: str
    cmd: str
    # Per-engine launch profile. ``args`` accepts either the modern
    # list[str] form (one literal argv element per entry) or the legacy
    # single pre-joined string for older saved tournaments. ``env`` is
    # an overlay applied on top of inherited env at engine spawn time.
    args: list[str] | str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    dir: str | None = None

    @field_validator("args", "env")
    @classmethod
    def _check_launch_profile(cls, v, info):
        # Reuse the registry-layer validator so a direct tournament POST
        # can't ship malformed argv/env that would only blow up at
        # proxy spawn time. Legacy `args` as a single string is allowed
        # through unchecked (validator only enforces list-form rules).
        try:
            if info.field_name == "args":
                validate_launch_profile(v if isinstance(v, list) else None, None)
            else:
                validate_launch_profile(None, v)
        except InvalidLaunchProfileError as e:
            raise ValueError(str(e)) from e
        return v


_SPRT_DEFAULTS = {"elo0": 0, "elo1": 10, "alpha": 0.05, "beta": 0.05, "model": "normalized"}

# Engine-default keys frozen into a tournament at create/edit time.
# Mirrors `Settings.engine_default_<key>` fields. Snapshotting all of
# them (including Nones) means a tournament's behavior cannot drift
# if global Settings change later.
_ENGINE_DEFAULT_KEYS = (
    "threads", "hash_mb", "syzygy_path",
    "book_path", "book_plies", "book_order",
)


def _freeze_engine_defaults(settings) -> dict:
    return {
        k: getattr(settings, f"engine_default_{k}", None)
        for k in _ENGINE_DEFAULT_KEYS
    }


# 409 payload for /start when a stop/fail tournament restart needs the
# user's confirmation. The reason string is part of the API contract
# (clients branch on it); keep it stable.
_WIPE_REQUIRED_REASON = "wipe_required"
_WIPE_REQUIRED_MESSAGE = (
    "Restarting will discard all previously recorded games. Continue?"
)
_WIPE_REQUIRED_DETAIL = {
    "reason": _WIPE_REQUIRED_REASON,
    "message": _WIPE_REQUIRED_MESSAGE,
}


def _resolve_sprt(template: dict, settings) -> dict:
    """If template.sprt is truthy but not a full dict, merge with sprt_defaults."""
    sprt = template.get("sprt")
    if not sprt:
        return template
    if not isinstance(sprt, dict):
        sprt = {}
    base = dict(_SPRT_DEFAULTS)
    base.update(settings.tournament_sprt_defaults or {})
    base.update(sprt)
    return {**template, "sprt": base}


class TournamentCreate(BaseModel):
    name: str
    template: dict[str, Any] = Field(default_factory=dict)
    engines: list[EngineRef]


class TournamentUpdate(BaseModel):
    name: str
    template: dict[str, Any] = Field(default_factory=dict)
    engines: list[EngineRef]


class TournamentSettingsUpdate(BaseModel):
    fastchess_path: str | None = None
    tournaments_root: str | None = None
    default_template: dict[str, Any] | None = None
    sprt_defaults: dict[str, Any] | None = None


def _store(request: Request) -> TournamentStore:
    return request.app.state.tournament_store


def _orch(request: Request) -> Orchestrator:
    return request.app.state.tournament_orch


def _serialize(
    t,
    *,
    with_stats: bool = False,
    with_standings: bool = False,
    store: TournamentStore | None = None,
    orch: Orchestrator | None = None,
) -> dict:
    out = t.to_dict()
    if (with_stats or with_standings) and store is not None:
        tournament_type = (t.template or {}).get("tournament_type", "roundrobin")
        try:
            standings = compute_standings(
                store.pgn_path(t.id), tournament_type=tournament_type,
            ).to_dict()
        except FileNotFoundError:
            standings = {"games": 0, "engines": []}
        standings["tournament_type"] = tournament_type
        out["standings"] = standings
    if with_stats and store is not None:
        games_per_round = (t.template or {}).get("games_per_round", 2)
        paired = games_per_round != 1
        try:
            out["partial_pairs"] = count_partial_pairs(
                store.pgn_path(t.id), paired=paired,
            )
        except FileNotFoundError:
            out["partial_pairs"] = 0
        try:
            out["games"] = compute_games_list(store.pgn_path(t.id))
        except FileNotFoundError:
            out["games"] = []
        # Surface the orchestrator's currently-active proxies so the
        # workspace's Schedule can seed its rows on mount, not just from
        # forward-going `proxy_started` events. Only meaningful when this
        # tournament is the running one.
        if orch is not None and orch.active_id() == t.id:
            out["proxies_active"] = orch.active_proxies()
            out["pairings_active"] = orch.active_pairings()
        else:
            out["proxies_active"] = []
            out["pairings_active"] = []
        sprt_params = (t.template or {}).get("sprt")
        if sprt_params and len(t.engines) >= 2:
            try:
                out["sprt"] = compute_sprt(
                    store.pgn_path(t.id),
                    sprt_params,
                    engine_a=t.engines[0]["name"],
                    engine_b=t.engines[1]["name"],
                ).to_dict()
            except (NotImplementedError, KeyError, ValueError) as e:
                log.warning("compute_sprt failed for %s: %s", t.id, e)
                out["sprt"] = None
    return out


# ---------------------------------------------------------------------------
# Tournaments
# ---------------------------------------------------------------------------


@router.get("/api/tournaments")
def list_tournaments(request: Request) -> dict:
    s = _store(request)
    return {
        "active_id": _orch(request).active_id(),
        "tournaments": [
            _serialize(t, with_standings=True, store=s, orch=_orch(request))
            for t in s.list()
        ],
    }


@router.post("/api/tournaments", status_code=201)
def create_tournament(payload: TournamentCreate, request: Request) -> dict:
    s = _store(request)
    if not payload.engines:
        raise HTTPException(status_code=400, detail="at least one engine required")
    if len(payload.engines) < 2:
        raise HTTPException(status_code=400, detail="at least two engines required")
    name = payload.name.strip() or "tournament"
    settings = request.app.state.settings
    engine_defaults = _freeze_engine_defaults(settings)
    try:
        t = s.create(
            name=name,
            template=_resolve_sprt(payload.template, settings),
            engines=[e.model_dump(exclude_none=True) for e in payload.engines],
            engine_defaults=engine_defaults,
        )
    except DuplicateNameError:
        raise HTTPException(status_code=409, detail="tournament name already exists")
    return _serialize(t)


@router.get("/api/tournaments/{tournament_id}")
def get_tournament(tournament_id: str, request: Request) -> dict:
    s = _store(request)
    try:
        t = s.get(tournament_id)
    except TournamentNotFoundError as e:
        raise HTTPException(status_code=404, detail="tournament not found") from e
    except CorruptStateError as e:
        raise HTTPException(status_code=500, detail=f"corrupt state: {e}") from e
    return _serialize(t, with_stats=True, store=s, orch=_orch(request))


@router.patch("/api/tournaments/{tournament_id}")
def edit_tournament(tournament_id: str, payload: TournamentUpdate, request: Request) -> dict:
    """Replace a tournament's name, template, and engines; reset it to idle.

    Rejected if the tournament is currently running. Any recorded games
    (games.pgn) are deleted — the caller must have confirmed this with
    the user before posting.
    """
    orch = _orch(request)
    if orch.active_id() == tournament_id:
        raise HTTPException(
            status_code=409, detail="tournament is running; stop it first"
        )
    if len(payload.engines) < 2:
        raise HTTPException(status_code=400, detail="at least two engines required")
    s = _store(request)
    name = payload.name.strip() or "tournament"
    settings = request.app.state.settings
    engine_defaults = _freeze_engine_defaults(settings)
    try:
        t = s.update(
            tournament_id,
            name=name,
            template=_resolve_sprt(payload.template, settings),
            engines=[e.model_dump(exclude_none=True) for e in payload.engines],
            engine_defaults=engine_defaults,
        )
    except TournamentNotFoundError as e:
        raise HTTPException(status_code=404, detail="tournament not found") from e
    except DuplicateNameError:
        raise HTTPException(status_code=409, detail="tournament name already exists")
    # Wipe the orchestrator's in-memory event-history buffer for this
    # tournament: any post-edit live-window re-subscribe would otherwise
    # replay stale events from the pre-edit run. Mirrors delete_tournament.
    orch.clear_event_history(tournament_id)
    return _serialize(t)


@router.delete("/api/tournaments/{tournament_id}", status_code=204)
def delete_tournament(tournament_id: str, request: Request) -> None:
    orch = _orch(request)
    if orch.active_id() == tournament_id:
        raise HTTPException(
            status_code=409, detail="tournament is running; stop it first"
        )
    try:
        _store(request).remove(tournament_id)
    except TournamentNotFoundError as e:
        raise HTTPException(status_code=404, detail="tournament not found") from e
    orch.clear_event_history(tournament_id)


@router.get("/api/tournaments/{tournament_id}/events")
def get_tournament_events(tournament_id: str, request: Request) -> dict:
    """Recent emitted events for this tournament, oldest first.

    Workspace clients call this on open to backfill the event log so
    a workspace opened after a restart still sees the fastchess
    startup chatter that fired before the subscriber attached. The
    items use the same shape the WS bus delivers; payload carries
    ``_seq`` (for dedup against the live stream) and ``_ts`` (for
    display).
    """
    orch = _orch(request)
    return {
        "events": [
            wrap_event_for_bus(e[ENVELOPE_KIND], e[ENVELOPE_PAYLOAD])
            for e in orch.event_history(tournament_id)
        ]
    }


@router.get("/api/tournaments/{tournament_id}/games/{game_n}/pgn")
def get_tournament_game_pgn(
    tournament_id: str,
    game_n: Annotated[int, FastApiPath(ge=1)],
    request: Request,
) -> dict:
    """Return PGN + final-position metadata for the Nth completed game.

    Replay uses ``pgn``; frozen-window rehydration uses ``final_fen``,
    ``last_move``, ``engine_white``, ``engine_black``, ``result``,
    ``termination``.
    """
    s = _store(request)
    try:
        s.get(tournament_id)
    except TournamentNotFoundError as e:
        raise HTTPException(status_code=404, detail="tournament not found") from e
    except CorruptStateError as e:
        raise HTTPException(status_code=500, detail=f"corrupt state: {e}") from e
    pgn_path = s.pgn_path(tournament_id)
    if not pgn_path.exists():
        raise HTTPException(status_code=404, detail="no games recorded")
    record = read_game_record(pgn_path, game_n)
    if record is None:
        raise HTTPException(status_code=404, detail="game not found")
    return record


@router.post("/api/tournaments/{tournament_id}/start")
async def start_tournament(
    tournament_id: str, request: Request, confirm_wipe: bool = False,
) -> dict:
    orch = _orch(request)
    store = _store(request)
    # Stop/fail restart wipes the dir (fastchess resume is unreliable).
    # confirm_wipe gates the destructive path server-side so a stray
    # /start can't silently destroy data.
    try:
        t = store.get(tournament_id)
    except TournamentNotFoundError as e:
        raise HTTPException(status_code=404, detail="tournament not found") from e
    needs_wipe = t.status in (STATUS_STOPPED, STATUS_FAILED)
    if needs_wipe:
        if not confirm_wipe:
            raise HTTPException(status_code=409, detail=_WIPE_REQUIRED_DETAIL)
        store.wipe_for_restart(tournament_id)
    try:
        t = await orch.start(tournament_id)
    except TournamentNotFoundError as e:
        raise HTTPException(status_code=404, detail="tournament not found") from e
    except TournamentBusyError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except FileNotFoundError as e:
        # fastchess binary missing
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RescheckError as e:
        raise HTTPException(
            status_code=400,
            detail={"reason": e.reason, "message": str(e), **e.details},
        ) from e
    return _serialize(t)


class RescheckRequest(BaseModel):
    """Resolved by the client from the template + selected engines'
    UCI option_schema. Server compares against local CPU / RAM."""
    parallel: int = 1
    max_threads: int = 1
    max_hash_mb: int = 16
    ponder: bool = False
    pin_affinity: bool = False
    allow_oversubscribe: bool = False


@router.post("/api/tournaments/rescheck")
def rescheck_tournament(payload: RescheckRequest) -> dict:
    """Resource sanity check called by the New Tournament dialog before
    POSTing the template. 200 = OK (with optional warnings); 400 carries
    a structured ``{reason, message, details}`` for the UI to render."""
    try:
        warnings = rescheck_run(
            parallel=payload.parallel,
            max_threads=payload.max_threads,
            max_hash_mb=payload.max_hash_mb,
            ponder=payload.ponder,
            pin_affinity=payload.pin_affinity,
            allow_oversubscribe=payload.allow_oversubscribe,
        )
    except RescheckError as e:
        raise HTTPException(
            status_code=400,
            detail={"reason": e.reason, "message": str(e), **e.details},
        ) from e
    return {"ok": True, "warnings": warnings}


@router.post("/api/tournaments/{tournament_id}/stop")
async def stop_tournament(tournament_id: str, request: Request) -> dict:
    orch = _orch(request)
    try:
        t = await orch.stop(tournament_id)
    except TournamentNotFoundError as e:
        raise HTTPException(status_code=404, detail="tournament not found") from e
    return _serialize(t)


@router.post("/api/tournaments/{tournament_id}/reveal", status_code=204)
def reveal_tournament_folder(tournament_id: str, request: Request) -> None:
    path = _store(request).dir_for(tournament_id)
    if not path.is_dir():
        raise HTTPException(status_code=404, detail="tournament folder not found")
    if sys.platform == "win32":
        subprocess.Popen(["explorer", str(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


# ---------------------------------------------------------------------------
# Tournament settings
# ---------------------------------------------------------------------------


def _serialize_settings(s) -> dict:
    return {
        "fastchess_path": s.tournament_fastchess_path,
        "tournaments_root": s.tournament_root or str(_default_root_for_settings()),
        "default_template": dict(s.tournament_default_template or {}),
        "sprt_defaults": dict(s.tournament_sprt_defaults or {}),
        "fastchess_detected": FastchessRunner.detect_binary(s.tournament_fastchess_path),
    }


def _default_root_for_settings() -> Path:
    from ..tournament.store import default_root
    return default_root()


@router.get("/api/tournament-settings")
def get_tournament_settings(request: Request) -> dict:
    return _serialize_settings(request.app.state.settings)


@router.put("/api/tournament-settings")
def update_tournament_settings(payload: TournamentSettingsUpdate, request: Request) -> dict:
    s = request.app.state.settings
    runner: FastchessRunner = request.app.state.tournament_runner
    store: TournamentStore = request.app.state.tournament_store

    if payload.fastchess_path is not None:
        s.tournament_fastchess_path = payload.fastchess_path or None
        runner.set_binary_path(s.tournament_fastchess_path)
    if payload.tournaments_root is not None:
        new_root = (payload.tournaments_root or "").strip() or None
        s.tournament_root = new_root
        # Live-update the store's root. Phase 1: a change while a
        # tournament is running affects only future tournaments (the
        # running runner has its paths frozen in RunSpec).
        store.set_root(Path(new_root) if new_root else _default_root_for_settings())
    if payload.default_template is not None:
        s.tournament_default_template = dict(payload.default_template)
    if payload.sprt_defaults is not None:
        s.tournament_sprt_defaults = dict(payload.sprt_defaults)

    try:
        s.save_persisted()
    except OSError:
        pass
    return _serialize_settings(s)


# ---------------------------------------------------------------------------
# Slice 9b: proxy ingest + per-proxy WS subscription
# ---------------------------------------------------------------------------


# Response field carrying the want-info gate back to the proxy. Sent
# only when the value flips, so steady-state responses stay empty (204).
WANT_INFO_KEY = "want_info"


class ProxyBatch(BaseModel):
    """Batched UCI lines from one proxy. Posted by the proxy script
    every ~50ms or every ~32 lines (whichever first)."""
    proxy_id: str
    secret: str
    engine_name: str | None = None
    lines: list[str] = Field(default_factory=list)
    # When the proxy is shutting down, set ``ended=True`` (with empty
    # ``lines``) to release subscribers.
    ended: bool = False


@internal_router.post("/internal/proxy")
async def ingest_proxy(payload: ProxyBatch, request: Request) -> Response:
    orch: Orchestrator = _orch(request)
    if not orch.verify_proxy_secret(payload.secret):
        # Stale or unknown proxy posting after tournament ended, or
        # someone unauthorized. Quietly reject.
        raise HTTPException(status_code=401, detail="invalid proxy secret")

    if payload.engine_name and payload.lines == []:
        # First post from a proxy: register it.
        await orch.proxy_session_started(payload.proxy_id, payload.engine_name)

    if payload.lines:
        await orch.ingest_proxy_lines(payload.proxy_id, payload.lines)

    if payload.ended:
        await orch.proxy_session_ended(payload.proxy_id)
        return Response(status_code=204)

    # Tell the proxy whether to keep tapping ``info`` -- only when the
    # gate has flipped since we last told it, so the common case (no
    # change) stays a bodiless 204.
    signal = orch.want_info_signal(payload.proxy_id)
    if signal is None:
        return Response(status_code=204)
    return JSONResponse({WANT_INFO_KEY: signal})


async def _stream_queue_to_websocket(websocket: WebSocket, queue) -> None:
    """Pump CoalescingQueue to WS until terminal, client disconnect, or
    cancel. Terminal beats recv on close so the banner always lands."""
    if queue.terminal is not None:
        try:
            await websocket.send_json(queue.terminal)
        except Exception:
            log.warning(
                "tournament WS: send_json failed on race-on-attach terminal flush; "
                "banner lost: %s", queue.terminal,
            )
        return

    async def _drain_recv() -> None:
        while True:
            await websocket.receive()

    recv_task = asyncio.create_task(_drain_recv())
    term_task = asyncio.create_task(queue.wait_terminal())
    get_task: asyncio.Task | None = None
    try:
        while True:
            if get_task is None or get_task.done():
                get_task = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait(
                {get_task, recv_task, term_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if term_task in done:
                # Race-recovery path: if recv also resolved this tick, the
                # old (pre-sticky-channel) handler would have dropped the
                # terminal. Log so we can correlate with banner reports.
                if recv_task in done:
                    log.warning(
                        "tournament WS: recv and terminal landed same tick; "
                        "flushing terminal via sticky channel: %s",
                        term_task.result(),
                    )
                try:
                    await websocket.send_json(term_task.result())
                except Exception:
                    log.warning(
                        "tournament WS: send_json failed on terminal flush; "
                        "banner lost: %s", term_task.result(),
                    )
                break
            if recv_task in done:
                break
            payload = get_task.result()
            get_task = None
            if "parsed" not in payload:
                line = payload.get("line", "")
                parsed = parse_uci_line(line)
                if parsed is not None:
                    payload = {**payload, "parsed": parsed}
            await websocket.send_json(payload)
    except WebSocketDisconnect:
        if term_task.done():
            try:
                await websocket.send_json(term_task.result())
            except Exception:
                log.warning(
                    "tournament WS: send_json failed flushing terminal after "
                    "WebSocketDisconnect; banner lost: %s", term_task.result(),
                )
    except asyncio.CancelledError:
        pass
    except Exception:
        log.error("tournament WS handler error", exc_info=True)
    finally:
        recv_task.cancel()
        if get_task is not None:
            get_task.cancel()
        if not term_task.done():
            term_task.cancel()


@internal_router.websocket("/ws/tournament/proxy/{proxy_id}")
async def proxy_subscribe(
    websocket: WebSocket,
    proxy_id: str,
) -> None:
    """WS endpoint that streams one proxy's UCI lines to a subscriber.

    Each frame is JSON. Recognized line types are enriched with a
    ``parsed`` dict (see ``uci_parse.py``):

      ``{"proxy_id": ..., "line": ..., "parsed": {kind: "position",
         fen, moves, last_move, ply, side_to_move}}``

    Or for ended sessions:

      ``{"proxy_id": ..., "ended": true}``
    """
    settings = websocket.app.state.settings
    if not _check_ws_auth(websocket, settings):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    orch: Orchestrator = websocket.app.state.tournament_orch
    queue = orch.subscribe_to_proxy(proxy_id)
    try:
        await _stream_queue_to_websocket(websocket, queue)
    finally:
        orch.unsubscribe_from_proxy(proxy_id, queue)
        try:
            await websocket.close()
        except Exception:
            pass


@internal_router.websocket("/ws/tournament/game/{pair_id}")
async def game_subscribe(
    websocket: WebSocket,
    pair_id: str,
) -> None:
    """WS endpoint scoped to a confirmed game pair (pair_id = UUID).

    Same frame format as ``/ws/tournament/proxy/{proxy_id}`` but the
    stream closes automatically when the pairing dissolves."""
    settings = websocket.app.state.settings
    if not _check_ws_auth(websocket, settings):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    orch: Orchestrator = websocket.app.state.tournament_orch
    queue = orch.subscribe_to_game(pair_id)
    try:
        await _stream_queue_to_websocket(websocket, queue)
    finally:
        orch.unsubscribe_from_game(pair_id, queue)
        try:
            await websocket.close()
        except Exception:
            pass
