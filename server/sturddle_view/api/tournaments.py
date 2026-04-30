"""REST surface for the tournament subsystem.

Endpoints (see ``docs/tournament-spec.md``):

  GET    /api/tournaments
  POST   /api/tournaments
  GET    /api/tournaments/{id}
  DELETE /api/tournaments/{id}
  POST   /api/tournaments/{id}/start
  POST   /api/tournaments/{id}/stop
  GET    /api/tournament-settings
  PUT    /api/tournament-settings

Live events flow through the existing ``EventBus`` /ws channel (kinds
``tournament_status`` and ``tournament_update``).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth import require_token
from ..tournament.fastchess import FastchessRunner
from ..tournament.orchestrator import Orchestrator, TournamentBusyError
from ..tournament.pgn_stats import compute_sprt, compute_standings
from ..tournament.store import (
    CorruptStateError,
    TournamentNotFoundError,
    TournamentStore,
)


log = logging.getLogger(__name__)


router = APIRouter(tags=["tournaments"], dependencies=[Depends(require_token)])


class EngineRef(BaseModel):
    name: str
    cmd: str
    args: str | None = None
    dir: str | None = None


class TournamentCreate(BaseModel):
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


def _serialize(t, *, with_stats: bool = False, store: TournamentStore | None = None) -> dict:
    out = t.to_dict()
    if with_stats and store is not None:
        try:
            standings = compute_standings(store.pgn_path(t.id)).to_dict()
        except FileNotFoundError:
            standings = {"games": 0, "engines": []}
        out["standings"] = standings
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
        "tournaments": [_serialize(t) for t in s.list()],
    }


@router.post("/api/tournaments", status_code=201)
def create_tournament(payload: TournamentCreate, request: Request) -> dict:
    s = _store(request)
    if not payload.engines:
        raise HTTPException(status_code=400, detail="at least one engine required")
    if len(payload.engines) < 2:
        raise HTTPException(status_code=400, detail="at least two engines required")
    t = s.create(
        name=payload.name.strip() or "tournament",
        template=payload.template,
        engines=[e.model_dump(exclude_none=True) for e in payload.engines],
    )
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
    return _serialize(t, with_stats=True, store=s)


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
    return _serialize(t)


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
