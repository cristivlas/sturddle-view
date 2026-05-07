"""REST + WS surface for the tournament subsystem.

Live events flow on the shared ``EventBus`` /ws channel
(``tournament_status`` / ``tournament_update``); per-proxy line streams
have their own ``/ws/tournament/proxy/...`` channel.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel, Field

from ..auth import require_token
from ..tournament.fastchess import FastchessRunner
from ..tournament.orchestrator import Orchestrator, TournamentBusyError, wrap_event_for_bus
from ..tournament.rescheck import RescheckError, check as rescheck_run
from ..tournament.pgn_stats import compute_games_list, compute_sprt, compute_standings
from ..tournament.store import (
    CorruptStateError,
    DuplicateNameError,
    TournamentNotFoundError,
    TournamentStore,
)


log = logging.getLogger(__name__)


router = APIRouter(tags=["tournaments"], dependencies=[Depends(require_token)])

# Internal router: no user-token auth. Authenticated via per-tournament
# secret (proxy posts) or via the standard token (?token=) for the WS.
internal_router = APIRouter(tags=["tournaments-internal"])


class EngineRef(BaseModel):
    id: str
    name: str
    cmd: str
    args: str | None = None
    dir: str | None = None


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
        try:
            standings = compute_standings(store.pgn_path(t.id)).to_dict()
        except FileNotFoundError:
            standings = {"games": 0, "engines": []}
        out["standings"] = standings
    if with_stats and store is not None:
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
        if sprt_params:
            try:
                out["sprt"] = compute_sprt(store.pgn_path(t.id), sprt_params).to_dict()
            except (NotImplementedError, KeyError):
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
        "tournaments": [_serialize(t, with_standings=True, store=s) for t in s.list()],
    }


@router.post("/api/tournaments", status_code=201)
def create_tournament(payload: TournamentCreate, request: Request) -> dict:
    s = _store(request)
    if not payload.engines:
        raise HTTPException(status_code=400, detail="at least one engine required")
    if len(payload.engines) < 2:
        raise HTTPException(status_code=400, detail="at least two engines required")
    name = payload.name.strip() or "tournament"
    # Freeze ALL engine_default_* fields at create time — including Nones —
    # so a tournament's behavior cannot drift if Settings change later.
    settings = request.app.state.settings
    engine_defaults = {
        k: getattr(settings, f"engine_default_{k}", None)
        for k in ("threads", "hash_mb", "syzygy_path",
                  "book_path", "book_plies", "book_order")
    }
    try:
        t = s.create(
            name=name,
            template=payload.template,
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
    # Re-freeze engine_default_* — same rationale as create: a tournament's
    # behavior should not silently drift if Settings change later.
    settings = request.app.state.settings
    engine_defaults = {
        k: getattr(settings, f"engine_default_{k}", None)
        for k in ("threads", "hash_mb", "syzygy_path",
                  "book_path", "book_plies", "book_order")
    }
    try:
        t, _ = s.update(
            tournament_id,
            name=name,
            template=payload.template,
            engines=[e.model_dump(exclude_none=True) for e in payload.engines],
            engine_defaults=engine_defaults,
        )
    except TournamentNotFoundError as e:
        raise HTTPException(status_code=404, detail="tournament not found") from e
    except DuplicateNameError:
        raise HTTPException(status_code=409, detail="tournament name already exists")
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
            wrap_event_for_bus(e["kind"], e["payload"])
            for e in orch.event_history(tournament_id)
        ]
    }


@router.post("/api/tournaments/{tournament_id}/start")
async def start_tournament(tournament_id: str, request: Request) -> dict:
    orch = _orch(request)
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


# ---------------------------------------------------------------------------
# Tournament settings
# ---------------------------------------------------------------------------


def _serialize_settings(s) -> dict:
    return {
        "fastchess_path": s.tournament_fastchess_path,
        "tournaments_root": s.tournament_root or str(_default_root_for_settings()),
        "default_template": dict(s.tournament_default_template or {}),
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

    try:
        s.save_persisted()
    except OSError:
        pass
    return _serialize_settings(s)


# ---------------------------------------------------------------------------
# Slice 9b: proxy ingest + per-proxy WS subscription
# ---------------------------------------------------------------------------


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


@internal_router.post("/internal/proxy", status_code=204)
async def ingest_proxy(payload: ProxyBatch, request: Request) -> None:
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


@internal_router.websocket("/ws/tournament/proxy/{proxy_id}")
async def proxy_subscribe(
    websocket: WebSocket,
    proxy_id: str,
    token: str = Query(default=""),
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
    if not settings.auth_disabled:
        import hmac
        if not hmac.compare_digest(token, settings.token):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

    await websocket.accept()
    orch: Orchestrator = websocket.app.state.tournament_orch
    queue = orch.subscribe_to_proxy(proxy_id)

    from ..tournament.uci_parse import parse_uci_line

    async def _drain_recv() -> None:
        while True:
            await websocket.receive()

    recv_task = asyncio.create_task(_drain_recv())
    try:
        while True:
            get_task = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait(
                {get_task, recv_task}, return_when=asyncio.FIRST_COMPLETED,
            )
            if recv_task in done:
                get_task.cancel()
                break
            payload = get_task.result()
            if payload.get("ended"):
                await websocket.send_json(payload)
                break
            # Enrich with parsed fields where possible. ``line`` is
            # always present in non-ended payloads. Reuse the parse the
            # orchestrator may have already done for pairing detection.
            if "parsed" not in payload:
                line = payload.get("line", "")
                parsed = parse_uci_line(line)
                if parsed is not None:
                    payload = {**payload, "parsed": parsed}
            await websocket.send_json(payload)
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("proxy WS handler error")
    finally:
        recv_task.cancel()
        orch.unsubscribe_from_proxy(proxy_id, queue)
        try:
            await websocket.close()
        except Exception:
            pass


@internal_router.websocket("/ws/tournament/game/{pair_id}")
async def game_subscribe(
    websocket: WebSocket,
    pair_id: str,
    token: str = Query(default=""),
) -> None:
    """WS endpoint scoped to a confirmed game pair (pair_id = UUID).

    Same frame format as ``/ws/tournament/proxy/{proxy_id}`` but the
    stream closes automatically when the pairing dissolves."""
    settings = websocket.app.state.settings
    if not settings.auth_disabled:
        import hmac
        if not hmac.compare_digest(token, settings.token):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

    await websocket.accept()
    orch: Orchestrator = websocket.app.state.tournament_orch
    queue = orch.subscribe_to_game(pair_id)

    from ..tournament.uci_parse import parse_uci_line

    async def _drain_recv() -> None:
        while True:
            await websocket.receive()

    recv_task = asyncio.create_task(_drain_recv())
    try:
        while True:
            get_task = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait(
                {get_task, recv_task}, return_when=asyncio.FIRST_COMPLETED,
            )
            if recv_task in done:
                get_task.cancel()
                break
            payload = get_task.result()
            if payload.get("ended"):
                await websocket.send_json(payload)
                break
            if "parsed" not in payload:
                line = payload.get("line", "")
                parsed = parse_uci_line(line)
                if parsed is not None:
                    payload = {**payload, "parsed": parsed}
            await websocket.send_json(payload)
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("game WS handler error")
    finally:
        recv_task.cancel()
        orch.unsubscribe_from_game(pair_id, queue)
        try:
            await websocket.close()
        except Exception:
            pass
