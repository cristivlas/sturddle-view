from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import require_token
from ..engines import resolve_selected
from ..play.human_vs_engine import HumanVsEngine, TimeControl
from ..play.import_position import PositionImportError, parse_fen, parse_pgn

router = APIRouter(prefix="/game", tags=["game"], dependencies=[Depends(require_token)])


async def _get_hve(request: Request) -> HumanVsEngine:
    s = request.app.state
    path, name, options = resolve_selected(s.engines, s.settings)
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
        s.hve = HumanVsEngine(
            path,
            s.event_bus,
            openings=getattr(s, "openings", None),
            settings=s.settings,
            store=getattr(s, "game_store", None),
        )
    # Refresh display name + UCI options on every fetch so registry edits
    # take effect on the next engine launch without restarting the server.
    s.hve.set_engine_name(name)
    s.hve.set_engine_options(options)
    return s.hve


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


def _parse_import_payload(payload: dict) -> dict:
    fmt = payload.get("format")
    text = payload.get("text", "")
    if fmt not in ("fen", "pgn"):
        raise HTTPException(status_code=400, detail="format must be 'fen' or 'pgn'")
    if not isinstance(text, str):
        raise HTTPException(status_code=400, detail="missing 'text'")
    try:
        parsed = parse_fen(text) if fmt == "fen" else parse_pgn(text)
    except PositionImportError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "start_fen": parsed.start_fen,
        "moves_uci": parsed.moves_uci,
        "final_fen": parsed.final_fen,
        "side_to_move": parsed.side_to_move,
        "ply": parsed.ply,
        "summary": parsed.summary,
        "headers": parsed.headers,
    }


@router.post("/import/validate")
async def import_validate(payload: dict) -> dict:
    """Parse a FEN/PGN payload and report the resulting position. Read-only."""
    return _parse_import_payload(payload)


@router.post("/import")
async def import_game(payload: dict, request: Request) -> dict:
    """Start a new game from a FEN or PGN. Same shape as /game/new."""
    parsed = _parse_import_payload(payload)
    hve = await _get_hve(request)
    s = request.app.state.settings

    # Side selection: "white"|"black"|"side_to_move" (default).
    side = payload.get("human_side", "side_to_move")
    if side == "white":
        human_white = True
    elif side == "black":
        human_white = False
    else:
        human_white = parsed["side_to_move"] == "white"

    tc = TimeControl(
        initial_seconds=float(payload.get("initial_seconds", s.tc_initial_seconds)),
        increment_seconds=float(payload.get("increment_seconds", s.tc_increment_seconds)),
    )
    try:
        game_id = await hve.new_game(
            human_white=human_white,
            tc=tc,
            start_fen=parsed["start_fen"],
            start_moves_uci=parsed["moves_uci"],
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=f"engine not found: {e}") from e
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
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


@router.post("/sync")
async def sync(request: Request) -> dict:
    """Re-emit the current state (board + clock) so a freshly-mounted client
    can resync without server-side mutation."""
    s = request.app.state
    if s.hve is not None:
        try:
            await s.hve.republish_state()
        except Exception:
            pass
    return {"ok": True}


@router.post("/pause")
async def pause(request: Request) -> dict:
    hve = await _get_hve(request)
    try:
        await hve.pause()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True}


@router.post("/resume")
async def resume(request: Request) -> dict:
    hve = await _get_hve(request)
    try:
        await hve.resume()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True}


@router.post("/switch-sides")
async def switch_sides(request: Request) -> dict:
    hve = await _get_hve(request)
    try:
        await hve.switch_sides()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
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
