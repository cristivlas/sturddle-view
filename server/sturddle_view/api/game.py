from __future__ import annotations

import asyncio
import logging
import random
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from ..auth import require_token
from ..engines import resolve_selected
from ..env_utils import env_int
from ._ai_kick import cancel_ai_turn, start_ai_turn
from ..play.canonical_hash import canonical_hash, canonical_hash_from_game
from ..play.human_vs_engine import HumanVsEngine, TimeControl, ViewModeParams
from ..config import BOOK_ORDER_RANDOM, BOOK_ORDER_SEQUENTIAL
from ..play.import_position import PositionImportError, parse_fen, parse_pgn
from ..play.opening_lines import BookRef, is_epd_book, select_epd_seed
from ..recent_imports import ROW_ALIAS_IDS, ROW_GAME_ID, ROW_REFS, RemoveStatus

log = logging.getLogger(__name__)

# Sanity cap on /game/import payload size. Real PGNs (even a 1000-game
# Megabase chunk) are well under 2 MB; this just stops a runaway paste
# or malicious LAN client from filling disk via the recent-imports
# store. Override at import time via SV_MAX_IMPORT_BYTES (used by
# tests to exercise the limit without large fixtures).
MAX_IMPORT_TEXT_BYTES = env_int("SV_MAX_IMPORT_BYTES", 2 * 1024 * 1024)
MAX_ANNOTATION_LENGTH = env_int("SV_MAX_ANNOTATION_LENGTH", 10_000)

router = APIRouter(prefix="/game", tags=["game"], dependencies=[Depends(require_token)])


def _hash_parsed(parsed: dict, raw_text: str) -> str:
    """Hash from the already-parsed game if available; else fall back to text."""
    game = parsed.get("_parsed_game")
    if game is not None:
        return canonical_hash_from_game(game)
    return canonical_hash(raw_text, parsed["detected_format"])


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
            recents=getattr(s, "recent_imports", None),
        )
    # Refresh display name + UCI options on every fetch so registry edits
    # take effect on the next engine launch without restarting the server.
    # set_engines here is the single wiring point -- no construction site can
    # forget the registry and silently leave analysis on the play engine.
    s.hve.set_engines(s.engines)
    s.hve.set_engine_name(launch.name)
    s.hve.set_engine_options(launch.options)
    s.hve.set_engine_args(launch.args)
    s.hve.set_engine_env(launch.env)
    return s.hve


def _resolve_human_white(side: str) -> bool:
    """Map a human_side setting ('white'|'black'|'random') to a boolean,
    coin-flipping 'random'."""
    if side == "random":
        return random.random() < 0.5
    return side != "black"


@router.post("/new")
async def new_game(payload: dict, request: Request) -> dict:
    hve = await _get_hve(request)
    s = request.app.state.settings

    human_white = _resolve_human_white(payload.get("human_side", s.human_side))

    tc = TimeControl(
        initial_seconds=float(payload.get("initial_seconds", s.tc_initial_seconds)),
        increment_seconds=float(payload.get("increment_seconds", s.tc_increment_seconds)),
    )
    player_name = s.player_name.strip() or None
    await _cancel_ai_analysis(request)
    seed_fen, book = await _resolve_book(s)
    try:
        game_id = await hve.new_game(
            human_white=human_white,
            tc=tc,
            player_name=player_name,
            start_fen=seed_fen,
            book=book,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=f"engine not found: {e}") from e
    return {"game_id": game_id, "human_white": human_white}


async def _resolve_book(s) -> tuple[str | None, BookRef | None]:
    """Book config for a new HVE game when the toggle is on and a book is
    set. EPD books seed a start position here (selection runs off the
    event loop: a large book takes real time to index before the mtime
    cache is warm, and would otherwise stall the whole server). PGN books
    return a BookRef the game consults on every engine turn. Either way
    the sequential cursor advances per game (random ignores it); for PGN
    it becomes the game's matching-pool anchor."""
    if not s.hve_use_opening_book or not s.engine_default_book_path:
        return None, None
    path = s.engine_default_book_path
    order = s.engine_default_book_order
    # Missing book: no seed, no BookRef, and -- matching the EPD miss
    # below -- no cursor advance.
    if not Path(path).is_file():
        return None, None
    if is_epd_book(path):
        fen = await asyncio.to_thread(
            select_epd_seed, path, order, s.engine_default_book_cursor,
        )
        if fen is None:
            return None, None
        log.debug(
            "opening book: seeding position from %s (order=%s, cursor=%d): %s",
            path, order or BOOK_ORDER_SEQUENTIAL, s.engine_default_book_cursor, fen,
        )
        _advance_book_cursor(s)
        return fen, None
    book = BookRef(
        path=path,
        plies=s.engine_default_book_plies,
        order=order,
        anchor=s.engine_default_book_cursor,
    )
    _advance_book_cursor(s)
    return None, book


def _advance_book_cursor(s) -> None:
    """Sequential order walks the book across games (EPD: next position;
    PGN: rotates the anchor). Random leaves the cursor alone."""
    if s.engine_default_book_order == BOOK_ORDER_RANDOM:
        return
    s.engine_default_book_cursor += 1
    try:
        s.save_persisted()
    except OSError:
        log.warning("failed to persist opening-book cursor", exc_info=True)


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
        "_parsed_game": parsed.parsed_game,
    }


def _resolve_game_id_for_import(recents, payload: dict, view_hash: str) -> str:
    """Pick the game_id for an /game/import call.

    Rules:
    - new hash + supplied id  -> use supplied
    - new hash + no supplied  -> mint uuid4
    - existing hash + match   -> reuse stored id
    - existing hash + mismatch -> 409
    - existing hash + no supplied -> reuse stored id

    Raises HTTPException(409) on mismatch and HTTPException(400) if
    ``game_id`` in the payload is not a string."""
    supplied_id = payload.get("game_id") if isinstance(payload, dict) else None
    if supplied_id is not None and not isinstance(supplied_id, str):
        raise HTTPException(status_code=400, detail="game_id must be a string")
    existing = recents.get(view_hash)
    if existing is not None:
        stored_id = existing[0].get("game_id")
        if supplied_id is not None and stored_id is not None and supplied_id != stored_id:
            log.warning(
                "import: game_id assertion failed for hash=%s "
                "stored=%s supplied=%s",
                view_hash, stored_id, supplied_id,
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "game_id_mismatch",
                    "message": "supplied game_id does not match stored row",
                    "stored_game_id": stored_id,
                    "supplied_game_id": supplied_id,
                },
            )
        return stored_id or supplied_id or str(uuid.uuid4())
    return supplied_id or str(uuid.uuid4())


@router.post("/import/validate")
async def import_validate(payload: dict) -> dict:
    """Parse a FEN/PGN payload and report the resulting position. Read-only.
    Returns ``hash`` (SHA-256 of stripped text) so the caller can compare
    against the currently viewed game before committing a full import."""
    parsed = _parse_import_payload(payload)
    raw_text = payload.get("text", "")
    parsed["hash"] = _hash_parsed(parsed, raw_text)
    parsed.pop("_parsed_game", None)
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
    # Opening imports carry {eco, name}: label the game by the opening and
    # name the sides player-vs-engine per gameplay settings (instead of the
    # dataset PGN's "?" headers).
    opening = payload.get("opening")
    white_name = headers.get("White")
    black_name = headers.get("Black")
    if opening is not None:
        if not isinstance(opening, dict):
            raise HTTPException(status_code=400, detail="opening must be an object")
        eco = opening.get("eco", "")
        name = opening.get("name", "")
        if not isinstance(eco, str) or not isinstance(name, str):
            raise HTTPException(status_code=400, detail="opening eco/name must be strings")
        eco, name = eco.strip(), name.strip()
        # Only relabel when there's a real opening name; a blank name leaves
        # the sides as parsed (no spurious player/engine relabel).
        if name:
            # Assign the human side now (coin-flipping 'random'); this import
            # IS this game's side assignment, so the labels match the side
            # that will be played.
            s = request.app.state.settings
            human_white = _resolve_human_white(s.human_side)
            white_name, black_name = hve.play_side_names(human_white)
            summary = {
                **summary,
                "white": white_name,
                "black": black_name,
                "opening": f"{eco} {name}".strip(),
            }
    view_hash = _hash_parsed(parsed, raw_text)
    recents = request.app.state.recent_imports
    game_id = _resolve_game_id_for_import(recents, payload, view_hash)
    # Optional: caller can request the view cursor land at a specific
    # ply directly (no follow-up /view/goto). Used by x-game nav to
    # open parent/child at the fork ply in a single round-trip, which
    # avoids an animation flicker when the FEN at the target ply is
    # identical to what was on screen.
    land_at_ply = payload.get("land_at_ply")
    if land_at_ply is not None:
        try:
            land_at_ply = int(land_at_ply)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="land_at_ply must be int")
        if land_at_ply < 0:
            raise HTTPException(status_code=400, detail="land_at_ply must be >= 0")
    await _cancel_ai_analysis(request)
    try:
        game_id = await hve.enter_view_mode(
            ViewModeParams(
                start_fen=parsed["start_fen"],
                moves_uci=parsed["moves_uci"],
                clock_history=parsed["clock_history"],
                final_white_time=parsed["final_white_time"],
                final_black_time=parsed["final_black_time"],
                white_name=white_name,
                black_name=black_name,
                eval_history=parsed.get("eval_history"),
                comments=parsed.get("comments"),
                root_comment=parsed.get("root_comment"),
                pgn_result=headers.get("Result"),
                pgn_termination=headers.get("Termination"),
                view_hash=view_hash,
                view_summary=summary,
                view_original_text=raw_text if parsed["detected_format"] == "pgn" else None,
            ),
            game_id=game_id,
            land_at_ply=land_at_ply,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # Record in recent-imports. Use the format detected by the parser
    # (matters for auto: it's "fen" or "pgn" by now).
    h = await recents.save(
        fmt=parsed["detected_format"],
        text=raw_text,
        summary=summary,
        game_id=game_id,
        precomputed_hash=view_hash,
    )
    return {"game_id": game_id, "viewing": True, "hash": h, "summary": summary}


@router.get("/recent-imports")
async def list_recent_imports(request: Request) -> dict:
    """Return the recent-imports index sorted by ts desc.

    No blobs are returned -- callers GET /game/recent-imports/{hash} to
    fetch the text of an entry. Behind the same auth token as the rest
    of /game/*."""
    return {"entries": request.app.state.recent_imports.list()}


@router.get("/recent-imports/by-id/{game_id}")
async def get_recent_import_by_id(game_id: str, request: Request) -> dict:
    """Resolve a recent import by ``game_id`` instead of hash.

    Same payload shape as the hash route plus x-game navigation fields:
    ``parent_game_id``, ``fork_ply`` (when this row is a child), and
    ``children`` (list of live children with their fork_ply + summary).
    Side effect: touch (ts bump). Returns 404 for unknown or evicted ids.

    Registered before ``/recent-imports/{h}`` so the literal ``by-id``
    segment doesn't get captured as a hash."""
    recents = request.app.state.recent_imports
    # Lazy scrub: if this row's parent_game_id no longer resolves,
    # drop the dangling link before serving so the client doesn't see
    # a stale ancestor pointer.
    await recents.scrub_dangling_parent(game_id)
    got = recents.get_by_id(game_id)
    if got is None:
        raise HTTPException(status_code=404, detail="not found")
    row, text = got
    h = recents.hash_for_id(game_id)
    if h is not None:
        await recents.touch(h)
    return {
        "hash": h,
        "game_id": game_id,
        "format": row["format"],
        "summary": row["summary"],
        "ts": row["ts"],
        "text": text,
        "parent_game_id": row.get("parent_game_id"),
        "parent_summary": recents.parent_summary_of(game_id),
        "fork_ply": row.get("fork_ply"),
        "children": recents.children_of(game_id),
    }


@router.get("/recent-imports/{h}")
async def get_recent_import(h: str, request: Request) -> dict:
    """Return the full text + metadata for a single recent import.

    Side effect: bumps ``ts`` so frequently revisited entries stay at
    the top of the recents list (the dropdown shows newest first and
    eviction drops oldest)."""
    recents = request.app.state.recent_imports
    got = recents.get(h)
    if got is None:
        raise HTTPException(status_code=404, detail="not found")
    row, text = got
    game_id = row.get("game_id")
    if game_id is not None:
        await recents.scrub_dangling_parent(game_id)
        # Re-read after potential scrub.
        got = recents.get(h)
        if got is None:
            raise HTTPException(status_code=404, detail="not found")
        row, text = got
    await recents.touch(h)
    return {
        "hash": h,
        "game_id": game_id,
        "format": row["format"],
        "summary": row["summary"],
        "ts": row["ts"],
        "text": text,
        "parent_game_id": row.get("parent_game_id"),
        "parent_summary": (
            recents.parent_summary_of(game_id) if game_id else None
        ),
        "fork_ply": row.get("fork_ply"),
        "children": recents.children_of(game_id) if game_id else [],
    }


@router.delete("/recent-imports/{h}")
async def delete_recent_import(h: str, request: Request, force: bool = False) -> dict:
    """Remove a single entry from the recent-imports store.

    Returns 409 with a ``children`` list if the row has live forked
    children (refs non-empty); the row is pinned in that case and not
    deleted. Returns 409 ``{"error": "in_view"}`` when the row is the
    currently-viewed game and ``force`` is not set; with ``force=1`` the
    row is deleted and the view session is closed (idle board). The
    children pin is not overridable by force.
    """
    store = request.app.state.recent_imports
    hve = request.app.state.hve
    in_view = False
    blocked_by_refs = False
    got = store.get(h)
    if got is not None:
        row, _ = got
        blocked_by_refs = bool(row.get(ROW_REFS))
        if hve is not None:
            row_ids = {row.get(ROW_GAME_ID), *(row.get(ROW_ALIAS_IDS) or [])}
            viewed = hve.viewing_game_id
            in_view = viewed is not None and viewed in row_ids
    # The children pin is not overridable, so it must win over the
    # confirmable in_view gate -- no point confirming a delete that
    # remove() will refuse anyway. remove() stays authoritative for it.
    if in_view and not force and not blocked_by_refs:
        raise HTTPException(status_code=409, detail={"error": "in_view"})
    result = await store.remove(h)
    if result.status is RemoveStatus.NOT_FOUND:
        raise HTTPException(status_code=404, detail="not found")
    if result.status is RemoveStatus.BLOCKED_BY_REFS:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "has_children",
                "children": result.children,
            },
        )
    if in_view:
        await hve.close_view()
    return {"ok": True}


@router.get("/pgn")
async def export_pgn(request: Request) -> Response:
    """Download the current game as a PGN file.

    Works in play mode (in-progress or finished) and in view mode after a
    PGN import or a play_from_here fork. Returns 409 for FEN-only view
    (no game moves to export).

    Side effect: for play-mode games, also writes the PGN to the
    recents store (best-effort; failures are logged and swallowed --
    the download must not break if the side-effect fails). This makes
    user-driven Save PGN durable, including persisting a fork's
    parent_game_id before game-end / edit-commit (B11).
    """
    hve = await _get_hve(request)
    result = hve.get_pgn_text()
    if result is None:
        raise HTTPException(status_code=409, detail="no game to export")
    pgn_text, filename = result
    # Best-effort recents write. View-mode games are already in
    # recents (or were imported there); export_to_recents is a no-op
    # for them.
    await hve.export_to_recents()
    return Response(
        content=pgn_text,
        media_type="application/x-chess-pgn; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/view/start")
async def view_start(payload: dict, request: Request) -> dict:
    """Enter view mode at the current play-mode position.

    State flip only: no parsing, no recents write. Intended as the
    play -> view transition for clients that want to reach view mode
    (e.g. as a precondition to edit mode, or to scrub past moves) without
    going through `/game/import`, which saves to the recent-imports store.

    With ``suspend: true`` the live play game is held in memory so the client
    can resume the SAME game (no fork) via `/view/resume-play` -- used by the
    scrub-back feature. The edit precondition omits it (no resume, unchanged
    POV). Optional ``land_at_ply`` lands the cursor directly at a past ply
    (flicker-free) instead of the default last ply.
    """
    land_at_ply = payload.get("land_at_ply")
    if land_at_ply is not None and (isinstance(land_at_ply, bool) or not isinstance(land_at_ply, int)):
        raise HTTPException(status_code=400, detail="'land_at_ply' must be an integer")
    suspend = bool(payload.get("suspend", False))
    hve = await _get_hve(request)
    (
        start_fen,
        moves_uci,
        clock_history,
        white_time,
        black_time,
        eval_history,
    ) = hve.play_game_snapshot()
    summary = hve.play_game_summary()
    comments, root_comment = hve.play_game_comments()
    # Preserve the fork link across the play -> view state-flip so a
    # downstream annotation-only edit can still record it. (Import or
    # FEN-edit branches do NOT preserve.)
    fork_link = hve.fork_link
    await _cancel_ai_analysis(request)
    try:
        game_id = await hve.enter_view_mode(
            ViewModeParams(
                start_fen=start_fen,
                moves_uci=moves_uci,
                clock_history=clock_history or None,
                final_white_time=white_time,
                final_black_time=black_time,
                eval_history=eval_history if any(e is not None for e in eval_history) else None,
                white_name=summary["white"] if summary else None,
                black_name=summary["black"] if summary else None,
                view_summary=summary,
                comments=comments,
                root_comment=root_comment,
            ),
            fork_link=fork_link,
            land_at_ply=land_at_ply,
            suspend_play=suspend,
        )
        # enter_view_mode already lands (and publishes) at land_at_ply when
        # given; only jump to the last ply for the default (no target) entry.
        if land_at_ply is None:
            await hve.view_last()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"game_id": game_id, "viewing": True}


@router.post("/view/first")
async def view_first(request: Request) -> dict:
    hve = await _get_hve(request)
    try:
        await hve.view_first()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True}


@router.post("/view/back")
async def view_back(request: Request) -> dict:
    hve = await _get_hve(request)
    try:
        await hve.view_back()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True}


@router.post("/view/forward")
async def view_forward(request: Request) -> dict:
    hve = await _get_hve(request)
    try:
        await hve.view_forward()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True}


@router.post("/view/last")
async def view_last(request: Request) -> dict:
    hve = await _get_hve(request)
    try:
        await hve.view_last()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True}


@router.post("/view/goto")
async def view_goto(payload: dict, request: Request) -> dict:
    hve = await _get_hve(request)
    ply = payload.get("ply")
    if not isinstance(ply, int):
        raise HTTPException(status_code=400, detail="missing or non-integer 'ply'")
    try:
        await hve.view_goto(ply)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True}


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
    player_name = s.player_name.strip() or None
    await _cancel_ai_analysis(request)
    try:
        game_id = await hve.play_from_here(
            tc=tc, inherit_clocks=inherit_clocks, player_name=player_name,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=f"engine not found: {e}") from e
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"game_id": game_id, "viewing": False}


@router.post("/view/resume-play")
async def view_resume_play(request: Request) -> dict:
    """Exit view mode back into the SAME play game suspended by /view/start
    (no fork). 400 when there is no suspended game to resume."""
    hve = await _get_hve(request)
    await _cancel_ai_analysis(request)
    try:
        game_id = await hve.resume_play()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"game_id": game_id, "viewing": False}


@router.post("/edit/start")
async def edit_start(request: Request) -> dict:
    hve = await _get_hve(request)
    await _cancel_ai_analysis(request)
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
    apply_comment = bool(payload.get("apply_comment", False))
    comment_text = payload.get("comment_text", "")
    if not isinstance(comment_text, str):
        raise HTTPException(status_code=400, detail="'comment_text' must be a string")
    if len(comment_text) > MAX_ANNOTATION_LENGTH:
        raise HTTPException(status_code=400, detail="'comment_text' exceeds maximum length")
    prev_id = hve.game_id
    prev_hash = request.app.state.recent_imports.hash_for_id(prev_id) if prev_id else None
    # Capture the fork link before commit_edit -- the FEN-change branch
    # routes through enter_view_mode which would clear it (correct
    # behavior: FEN edit == new lineage). Annotation-only commit does
    # NOT go through enter_view_mode, so the live HVE link stays, but
    # we capture here anyway so the call site is symmetric.
    pre_commit_fork_link = hve.fork_link
    try:
        result = await hve.commit_edit(
            fen, apply_comment=apply_comment, comment_text=comment_text,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    recents = request.app.state.recent_imports
    h: str | None = None
    summary = None
    if result["changed"] == "fen":
        # FEN edit == new lineage; do NOT carry the fork link forward.
        summary = parse_fen(fen).summary
        h = await recents.save(
            fmt="fen", text=fen, summary=summary, game_id=result["game_id"],
        )
    elif result["changed"] == "comment":
        # Annotation-only commit: same game_id, content hash changed.
        # Replace the pre-edit recents row (if any) with the new PGN.
        # Carry the fork link through so a previously-unsaved child
        # gets promoted into recents with the link attached.
        summary = result["summary"]
        parent_game_id, fork_ply = (
            pre_commit_fork_link
            if pre_commit_fork_link is not None
            else (None, None)
        )
        h = await recents.replace_at(
            old_hash=prev_hash,
            fmt="pgn",
            text=result["pgn_text"],
            summary=summary or {},
            game_id=result["game_id"],
            precomputed_hash=result["hash"],
            parent_game_id=parent_game_id,
            fork_ply=fork_ply,
        )
        # Consumed -- clear the live link so it doesn't re-fire on a
        # subsequent transition.
        hve.clear_fork_link()
    return {"game_id": result["game_id"], "hash": h, "summary": summary}


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


# /status response when no HVE exists yet (nothing to guard).
_IDLE_STATUS = {
    "in_progress": False,
    "viewing": False,
    "view_hash": None,
    "view_summary": None,
    "analyzing": False,
}


@router.get("/status")
async def game_status(request: Request) -> dict:
    """Authoritative state for the client's discard/replace confirmations
    (tournament replay). Client mirrors of this die on page reload; this
    endpoint doesn't. Never creates an HVE."""
    s = request.app.state
    if s.hve is None:
        return dict(_IDLE_STATUS)
    return await s.hve.status()


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


def _clear_replay(request: Request) -> None:
    coord = getattr(request.app.state, "ai_coordinator", None)
    if coord is not None:
        coord.clear_replay()


async def _cancel_ai_analysis(request: Request) -> None:
    """Cancel any in-flight AI turn and drop its replay buffer.

    The AI agent and its rehydrate buffer are owned by the coordinator,
    not HVE. Endpoints that flip out of ANALYZING must call this so the
    agent stops streaming into a discarded context and a page reload
    doesn't rehydrate a stale panel.
    """
    await cancel_ai_turn(request)
    _clear_replay(request)


@router.post("/analysis/start")
async def analysis_start(request: Request) -> dict:
    """Enter analysis mode. The server picks the path:
    - engine go-infinite (default): HVE drives a live PV stream
    - AI agent (when settings.ai_enabled): tool-call-driven, engine is
      spawned per analyze call, no parallel go-infinite
    The client doesn't care which; both produce the same UI signal
    (analyzing=true on board_update, engine_info events on the bus).
    """
    hve = await _get_hve(request)
    try:
        await hve.start_analysis()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # Drop any prior session's buffer before a new one begins, in case
    # the previous /analysis/stop was skipped (mode toggle, crash).
    _clear_replay(request)
    if getattr(request.app.state.settings, "ai_enabled", False):
        await start_ai_turn(request)
    return {"ok": True}


@router.get("/analysis/replay")
async def analysis_replay(request: Request) -> dict:
    """Return the buffered AI events for the in-flight analysis turn.
    Lets a client reconnecting mid-analysis rebuild the panel from the
    bus events it missed. Empty list when no analysis is live."""
    coord = getattr(request.app.state, "ai_coordinator", None)
    if coord is None:
        return {"events": []}
    return {"events": coord.replay()}


@router.post("/analysis/stop")
async def analysis_stop(request: Request) -> dict:
    # Cancel before flipping out of ANALYZING so the agent stops cleanly.
    await _cancel_ai_analysis(request)
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
