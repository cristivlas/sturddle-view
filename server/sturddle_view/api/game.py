from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import require_token
from ..play.human_vs_engine import HumanVsEngine, TimeControl

router = APIRouter(prefix="/game", tags=["game"], dependencies=[Depends(require_token)])


async def _get_hve(request: Request) -> HumanVsEngine:
    s = request.app.state
    path = _resolve_engine_path(request)
    if path is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "no engine configured; register one via POST /engines and "
                "POST /engines/{id}/select, or start with --engine <path>"
            ),
        )
    if s.hve is not None and s.hve.engine_path != path:
        await s.hve.shutdown()
        s.hve = None
    if s.hve is None:
        s.hve = HumanVsEngine(path, s.event_bus)
    return s.hve


def _resolve_engine_path(request: Request) -> str | None:
    s = request.app.state
    if s.selected_engine_id:
        try:
            return s.engines.get(s.selected_engine_id).path
        except KeyError:
            s.selected_engine_id = None
    if s.settings.engine_path:
        return str(s.settings.engine_path)
    return None


@router.post("/new")
async def new_game(payload: dict, request: Request) -> dict:
    hve = await _get_hve(request)
    human_white = bool(payload.get("human_white", True))
    tc = TimeControl(
        initial_seconds=float(payload.get("initial_seconds", 300.0)),
        increment_seconds=float(payload.get("increment_seconds", 0.0)),
    )
    try:
        game_id = await hve.new_game(human_white=human_white, tc=tc)
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=f"engine not found: {e}") from e
    return {"game_id": game_id}


@router.post("/move")
async def submit_move(payload: dict, request: Request) -> dict:
    hve = await _get_hve(request)
    uci = payload.get("uci")
    if not isinstance(uci, str) or not uci:
        raise HTTPException(status_code=400, detail="missing 'uci'")
    try:
        await hve.submit_move(uci)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True}


@router.post("/resign")
async def resign(request: Request) -> dict:
    hve = await _get_hve(request)
    await hve.resign()
    return {"ok": True}


@router.post("/takeback")
def takeback() -> dict:
    raise HTTPException(status_code=501, detail="not implemented")


@router.post("/tournament/start")
def tournament_start(payload: dict) -> dict:
    raise HTTPException(status_code=501, detail="not implemented")


@router.post("/tournament/stop")
def tournament_stop() -> dict:
    raise HTTPException(status_code=501, detail="not implemented")


@router.post("/tournament/attach")
def tournament_attach(payload: dict) -> dict:
    raise HTTPException(status_code=501, detail="not implemented")
