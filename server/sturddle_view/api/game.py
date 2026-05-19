from __future__ import annotations

import hashlib
import logging
import os

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import Response

from ..auth import require_token
from ..engines import resolve_selected
from ..play.human_vs_engine import HumanVsEngine, TimeControl, ViewModeParams
from ..play.import_position import PositionImportError, parse_fen, parse_pgn

log = logging.getLogger(__name__)

# Sanity cap on /game/import payload size. Real PGNs (even a 1000-game
# Megabase chunk) are well under 2 MB; this just stops a runaway paste
# or malicious LAN client from filling disk via the recent-imports
# store. Override at import time via SV_MAX_IMPORT_BYTES (used by
# tests to exercise the limit without large fixtures).
MAX_IMPORT_TEXT_BYTES = int(
    os.environ.get("SV_MAX_IMPORT_BYTES", 2 * 1024 * 1024)
)

router = APIRouter(prefix="/game", tags=["game"], dependencies=[Depends(require_token)])


def _hash_import_text(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


async def _get_hve(request: Request) -> HumanVsEngine:
    s = request.app.state
    launch = resolve_selected(s.engines, s.settings)
    if launch.path is None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "no_engine_configured",
                "message": "No engine configured. Add one in Settings > Engines.",
            },
        )
    if s.hve is not None and s.hve.engine_path != launch.path:
        # Swap in-place to preserve the active game across engine changes.
        await s.hve.swap_engine(launch.path)
    if s.hve is None:
        s.hve = HumanVsEngine(
            launch.path,
            s.event_bus,
            openings=getattr(s, "openings", None),
            settings=s.settings,
            store=getattr(s, "game_store", None),
        )
    # Refresh display name + UCI options on every fetch so registry edits
    # take effect on the next engine launch without restarting the server.
    s.hve.set_engine_name(launch.name)
    s.hve.set_engine_options(launch.options)
    s.hve.set_engine_args(launch.args)
    s.hve.set_engine_env(launch.env)
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
    """Parse a FEN/PGN payload. `format` may be 'fen', 'pgn', or 'auto'
    (the default) — under 'auto' we try FEN first, then PGN, and report
    which one succeeded via `detected_format` so the UI can highlight
    the matching tab."""
    fmt = payload.get("format", "auto")
    text = payload.get("text", "")
    if fmt not in ("fen", "pgn", "auto"):
        raise HTTPException(
            status_code=400, detail="format must be 'fen', 'pgn', or 'auto'",
        )
    if not isinstance(text, str):
        raise HTTPException(status_code=400, detail="missing 'text'")
    size = len(text.encode("utf-8"))
    if size > MAX_IMPORT_TEXT_BYTES:
        log.warning("rejected oversized import: %d bytes (cap %d)", size, MAX_IMPORT_TEXT_BYTES)
        raise HTTPException(
            status_code=400,
            detail=f"text too large ({size} bytes; max {MAX_IMPORT_TEXT_BYTES})",
        )
    parsed = None
    detected = fmt
    if fmt == "fen":
        try:
            parsed = parse_fen(text)
        except PositionImportError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    elif fmt == "pgn":
        try:
            parsed = parse_pgn(text)
        except PositionImportError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    else:  # auto
        fen_err = pgn_err = None
        try:
            parsed = parse_fen(text)
            detected = "fen"
        except PositionImportError as e:
            fen_err = e
        if parsed is None:
            try:
                parsed = parse_pgn(text)
                detected = "pgn"
            except PositionImportError as e:
                pgn_err = e
        if parsed is None:
            # Surface the more informative error. PGN parser tends to be
            # noisier; FEN's "expected 8 rows" is a clearer first message
            # for the common case of someone pasting a half-FEN.
            detail = str(fen_err) if fen_err else str(pgn_err)
            raise HTTPException(status_code=400, detail=detail)
    return {
        "start_fen": parsed.start_fen,
        "moves_uci": parsed.moves_uci,
        "final_fen": parsed.final_fen,
        "side_to_move": parsed.side_to_move,
        "ply": parsed.ply,
        "summary": parsed.summary,
        "headers": parsed.headers,
        "clock_history": parsed.clock_history,
        "final_white_time": parsed.final_white_time,
        "final_black_time": parsed.final_black_time,
        "eval_history": parsed.eval_history,
        "comments": parsed.comments,
        "root_comment": parsed.root_comment,
        "detected_format": detected,
    }


@router.post("/import/validate")
async def import_validate(payload: dict) -> dict:
    """Parse a FEN/PGN payload and report the resulting position. Read-only.
    Returns ``hash`` (SHA-256 of stripped text) so the caller can compare
    against the currently viewed game before committing a full import."""
    parsed = _parse_import_payload(payload)
    raw_text = payload.get("text", "")
    parsed["hash"] = _hash_import_text(raw_text)
    return parsed


@router.post("/import")
async def import_game(payload: dict, request: Request) -> dict:
    """Import a FEN or PGN into VIEW MODE at the last ply.

    The user inspects via /game/view/* navigation; exit view by calling
    /game/view/play-from-here, which seeds a fresh play game from the
    cursor with the configured/payload TC.

    Side effect: a successful import is recorded in the recent-imports
    store (server-side history of opened positions). The hash is
    returned so the client can cache it as a metadata-only pointer.
    """
    parsed = _parse_import_payload(payload)
    hve = await _get_hve(request)
    headers = parsed.get("headers") or {}
    raw_text = payload.get("text", "")
    summary = parsed.get("summary") or {}
    view_hash = _hash_import_text(raw_text)
    try:
        game_id = await hve.enter_view_mode(ViewModeParams(
            start_fen=parsed["start_fen"],
            moves_uci=parsed["moves_uci"],
            clock_history=parsed["clock_history"],
            final_white_time=parsed["final_white_time"],
            final_black_time=parsed["final_black_time"],
            white_name=headers.get("White"),
            black_name=headers.get("Black"),
            eval_history=parsed.get("eval_history"),
            comments=parsed.get("comments"),
            root_comment=parsed.get("root_comment"),
            pgn_result=headers.get("Result"),
            pgn_termination=headers.get("Termination"),
            view_hash=view_hash,
            view_summary=summary,
            view_raw_text=raw_text if parsed["detected_format"] == "pgn" else None,
        ))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # Record in recent-imports. Use the format detected by the parser
    # (matters for auto: it's "fen" or "pgn" by now).
    h = await request.app.state.recent_imports.save(
        fmt=parsed["detected_format"],
        text=raw_text,
        summary=summary,
    )
    return {"game_id": game_id, "viewing": True, "hash": h, "summary": summary}


@router.get("/recent-imports")
async def list_recent_imports(request: Request) -> dict:
    """Return the recent-imports index sorted by ts desc.

    No blobs are returned -- callers GET /game/recent-imports/{hash} to
    fetch the text of an entry. Behind the same auth token as the rest
    of /game/*."""
    return {"entries": request.app.state.recent_imports.list()}


@router.get("/recent-imports/{h}")
async def get_recent_import(h: str, request: Request) -> dict:
    """Return the full text + metadata for a single recent import.

    Side effect: bumps ``ts`` so frequently revisited entries stay at
    the top of the recents list (the dropdown shows newest first and
    eviction drops oldest). See docs/recent-imports.md for the
    GET-with-side-effect trade-off."""
    recents = request.app.state.recent_imports
    got = recents.get(h)
    if got is None:
        raise HTTPException(status_code=404, detail="not found")
    row, text = got
    await recents.touch(h)
    return {"hash": h, "format": row["format"], "summary": row["summary"], "ts": row["ts"], "text": text}


@router.delete("/recent-imports/{h}")
async def delete_recent_import(h: str, request: Request) -> dict:
    """Remove a single entry from the recent-imports store."""
    removed = await request.app.state.recent_imports.remove(h)
    if not removed:
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True}


@router.get("/pgn")
async def export_pgn(request: Request) -> Response:
    """Download the current game as a PGN file.

    Works in play mode (in-progress or finished) and in view mode after a
    PGN import or a play_from_here fork.  Returns 409 for FEN-only view
    (no game moves to export).
    """
    hve = await _get_hve(request)
    result = hve.get_pgn_text()
    if result is None:
        raise HTTPException(status_code=409, detail="no game to export")
    pgn_text, filename = result
    return Response(
        content=pgn_text,
        media_type="application/x-chess-pgn; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/view/start")
async def view_start(request: Request) -> dict:
    """Enter view mode at the current play-mode position.

    State flip only: no parsing, no recents write. Intended as the
    play -> view transition for clients that want to reach view mode
    (e.g. as a precondition to edit mode) without going through
    `/game/import`, which has the side effect of saving to the
    recent-imports store.
    """
    hve = await _get_hve(request)
    start_fen, moves_uci, clock_history, white_time, black_time = hve.play_game_snapshot()
    try:
        game_id = await hve.enter_view_mode(ViewModeParams(
            start_fen=start_fen,
            moves_uci=moves_uci,
            clock_history=clock_history or None,
            final_white_time=white_time,
            final_black_time=black_time,
        ))
        await hve.view_last()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"game_id": game_id, "viewing": True}


def _nav_payload(payload: dict) -> dict:
    return {"include_comment_nav": bool(payload.get("include_comment_nav", False))}


@router.post("/view/first")
async def view_first(request: Request, payload: dict = Body(default={})) -> dict:
    hve = await _get_hve(request)
    try:
        extra = await hve.view_first(**_nav_payload(payload))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, **extra}


@router.post("/view/back")
async def view_back(request: Request, payload: dict = Body(default={})) -> dict:
    hve = await _get_hve(request)
    try:
        extra = await hve.view_back(**_nav_payload(payload))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, **extra}


@router.post("/view/forward")
async def view_forward(request: Request, payload: dict = Body(default={})) -> dict:
    hve = await _get_hve(request)
    try:
        extra = await hve.view_forward(**_nav_payload(payload))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, **extra}


@router.post("/view/last")
async def view_last(request: Request, payload: dict = Body(default={})) -> dict:
    hve = await _get_hve(request)
    try:
        extra = await hve.view_last(**_nav_payload(payload))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, **extra}


@router.post("/view/goto")
async def view_goto(payload: dict, request: Request) -> dict:
    hve = await _get_hve(request)
    ply = payload.get("ply")
    if not isinstance(ply, int):
        raise HTTPException(status_code=400, detail="missing or non-integer 'ply'")
    try:
        extra = await hve.view_goto(ply, **_nav_payload(payload))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, **extra}


@router.post("/view/play-from-here")
async def view_play_from_here(payload: dict, request: Request) -> dict:
    """Exit view mode by seeding a fresh play game from plies 0..cursor."""
    hve = await _get_hve(request)
    s = request.app.state.settings
    tc = TimeControl(
        initial_seconds=float(payload.get("initial_seconds", s.tc_initial_seconds)),
        increment_seconds=float(payload.get("increment_seconds", s.tc_increment_seconds)),
    )
    inherit_clocks = bool(payload.get("inherit_pgn_clocks", s.inherit_pgn_clocks))
    try:
        game_id = await hve.play_from_here(tc=tc, inherit_clocks=inherit_clocks)
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=f"engine not found: {e}") from e
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"game_id": game_id, "viewing": False}


@router.post("/edit/start")
async def edit_start(request: Request) -> dict:
    hve = await _get_hve(request)
    try:
        fen = await hve.enter_edit_mode()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"fen": fen}


@router.post("/edit/commit")
async def edit_commit(payload: dict, request: Request) -> dict:
    hve = await _get_hve(request)
    fen = payload.get("fen")
    if not isinstance(fen, str) or not fen:
        raise HTTPException(status_code=400, detail="missing 'fen'")
    prev_id = hve.game_id
    try:
        game_id = await hve.commit_edit(fen)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    h = None
    summary = None
    if game_id != prev_id:
        summary = parse_fen(fen).summary
        h = await request.app.state.recent_imports.save(
            fmt="fen", text=fen, summary=summary,
        )
    return {"game_id": game_id, "hash": h, "summary": summary}


@router.post("/edit/cancel")
async def edit_cancel(request: Request) -> dict:
    hve = await _get_hve(request)
    try:
        game_id = await hve.cancel_edit()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
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


@router.post("/analysis/start")
async def analysis_start(request: Request) -> dict:
    hve = await _get_hve(request)
    try:
        await hve.start_analysis()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True}


@router.post("/analysis/stop")
async def analysis_stop(request: Request) -> dict:
    hve = await _get_hve(request)
    try:
        await hve.stop_analysis()
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
