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
    if s.engines.selected_id:
        try:
            return s.engines.get(s.engines.selected_id).path
        except KeyError:
            pass
    if s.settings.engine_path:
        return str(s.settings.engine_path)
    return None


@router.post("/new")
async def new_game(payload: dict, request: Request) -> dict:
    import random

    hve = await _get_hve(request)
    s = request.app.state.settings

    side = payload.get("human_side", s.human_side)
    if side == "random":
        human_white = random.random() < 0.5
    elif side == "black":
        human_white = False
    else:
        human_white = True

    tc = TimeControl(
        initial_seconds=float(payload.get("initial_seconds", s.tc_initial_seconds)),
        increment_seconds=float(payload.get("increment_seconds", s.tc_increment_seconds)),
    )
    try:
        game_id = await hve.new_game(human_white=human_white, tc=tc)
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=f"engine not found: {e}") from e
    return {"game_id": game_id, "human_white": human_white}


@router.post("/move")
async def submit_move(payload: dict, request: Request) -> dict:
    hve = await _get_hve(request)
    uci = payload.get("uci")
    if not isinstance(uci, str) or not uci:
        raise HTTPException(status_code=400, detail="missing 'uci'")
    try:
        await hve.submit_move(uci)
    except RuntimeError as e:
        # Republish so a client whose UI moved optimistically can snap back.
        try:
            await hve.republish_state()
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True}


@router.post("/resign")
async def resign(request: Request) -> dict:
    hve = await _get_hve(request)
    await hve.resign()
    return {"ok": True}


@router.post("/takeback")
async def takeback(request: Request) -> dict:
    s = request.app.state.settings
    if not getattr(s, "allow_takeback", True):
        raise HTTPException(status_code=403, detail="take-back is disabled in settings")
    hve = await _get_hve(request)
    try:
        await hve.takeback()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True}


@router.post("/tournament/start")
def tournament_start(payload: dict) -> dict:
    raise HTTPException(status_code=501, detail="not implemented")


@router.post("/tournament/stop")
def tournament_stop() -> dict:
    raise HTTPException(status_code=501, detail="not implemented")


@router.post("/tournament/attach")
def tournament_attach(payload: dict) -> dict:
    raise HTTPException(status_code=501, detail="not implemented")
