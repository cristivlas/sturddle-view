from __future__ import annotations

import asyncio
import logging
import random
import uuid
from contextlib import contextmanager
from dataclasses import fields
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response

from ..auth import require_token
from ..chess.pgn_tags import TAG_BLACK, TAG_RESULT, TAG_TERMINATION, TAG_WHITE
from ..chess.results import SIDE_BLACK, SIDE_WHITE
from ..config import BOOK_ORDER_RANDOM, BOOK_ORDER_SEQUENTIAL, HUMAN_SIDE_RANDOM
from ..engines import resolve_selected
from ..env_utils import env_int
from ..error_detail import ERROR_KEY, error_detail
from ..play.canonical_hash import FMT_FEN, FMT_PGN, canonical_hash, canonical_hash_from_game
from ..play.human_vs_engine import (
    FEN_KEY,
    VIEWING_KEY,
    EditChange,
    HumanVsEngine,
    TimeControl,
    ViewModeParams,
    idle_status,
)
from ..play.import_position import (
    ImportedPosition,
    PositionImportError,
    parse_fen,
    parse_pgn,
)
from ..play.opening_lines import BookRef, is_epd_book, select_epd_seed
from ..recent_imports import (
    ROW_ALIAS_IDS,
    ROW_FORK_PLY,
    ROW_FORMAT,
    ROW_GAME_ID,
    ROW_HASH,
    ROW_PARENT_GAME_ID,
    ROW_REFS,
    ROW_SUMMARY,
    ROW_TS,
    RemoveStatus,
)
from ._ai_kick import ai_coordinator, cancel_ai_turn, start_ai_turn
from ._http import bad_request, conflict, not_found

log = logging.getLogger(__name__)

# Sanity cap on /game/import payload size. Real PGNs (even a 1000-game
# Megabase chunk) are well under 2 MB; this just stops a runaway paste
# or malicious LAN client from filling disk via the recent-imports
# store. Override at import time via SV_MAX_IMPORT_BYTES (used by
# tests to exercise the limit without large fixtures).
MAX_IMPORT_TEXT_BYTES = env_int("SV_MAX_IMPORT_BYTES", 2 * 1024 * 1024)
MAX_ANNOTATION_LENGTH = env_int("SV_MAX_ANNOTATION_LENGTH", 10_000)

# Wire keys shared by several endpoints. Game-id / format / summary share
# their names with the recent-imports row fields.
_GAME_ID_KEY = ROW_GAME_ID
_FORMAT_KEY = ROW_FORMAT
_SUMMARY_KEY = ROW_SUMMARY
_VIEWING_KEY = VIEWING_KEY
_HASH_KEY = ROW_HASH
_TEXT_KEY = "text"
_FEN_KEY = FEN_KEY
_OPENING_KEY = "opening"
_LAND_AT_PLY_KEY = "land_at_ply"
_COMMENT_TEXT_KEY = "comment_text"
_DETECTED_FORMAT_KEY = "detected_format"
_CHILDREN_KEY = "children"
_EVENTS_KEY = "events"
_ERROR_KEY = ERROR_KEY
# Import `format` values: FEN, PGN, or try FEN then PGN.
_FMT_AUTO = "auto"
_VALID_FORMATS = (FMT_FEN, FMT_PGN, _FMT_AUTO)
# ImportedPosition field kept server-side (not JSON-serializable).
_PARSED_GAME_FIELD = "parsed_game"
_NOT_FOUND = "not found"
_OK = {"ok": True}
_PGN_MEDIA_TYPE = "application/x-chess-pgn; charset=utf-8"
_RECENT_IMPORT_PATH = "/recent-imports/{h}"

router = APIRouter(prefix="/game", tags=["game"], dependencies=[Depends(require_token)])


def _engine_not_found(e: FileNotFoundError) -> HTTPException:
    return bad_request(f"engine not found: {e}")


@contextmanager
def _runtime_error_is_bad_request():
    """HVE rejects requests invalid in the current mode with RuntimeError."""
    try:
        yield
    except RuntimeError as e:
        raise bad_request(str(e)) from e


def _time_control(payload: dict, s) -> TimeControl:
    """The payload's TC, falling back to the configured one."""
    return TimeControl(
        initial_seconds=float(payload.get("initial_seconds", s.tc_initial_seconds)),
        increment_seconds=float(payload.get("increment_seconds", s.tc_increment_seconds)),
    )


def _player_name(s) -> str | None:
    return s.player_name.strip() or None


def _land_at_ply(payload: dict) -> int | None:
    """Optional ply to land the view cursor on directly (flicker-free)."""
    raw = payload.get(_LAND_AT_PLY_KEY)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise bad_request(f"'{_LAND_AT_PLY_KEY}' must be a non-negative integer")
    return raw


def _import_hash(pos: ImportedPosition, detected: str, raw_text: str) -> str:
    """Hash from the already-parsed game if available; else fall back to text."""
    if pos.parsed_game is not None:
        return canonical_hash_from_game(pos.parsed_game)
    return canonical_hash(raw_text, detected)


async def _get_hve(request: Request) -> HumanVsEngine:
    s = request.app.state
    launch = resolve_selected(s.engines, s.settings)
    if launch.path is None:
        raise bad_request(error_detail(
            "no_engine_configured", "No engine configured. Add one in Settings > Engines.",
        ))
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
    s.hve.apply_launch(launch)
    return s.hve


def _resolve_human_white(side: str) -> bool:
    """Map a human_side setting ('white'|'black'|'random') to a boolean,
    coin-flipping 'random'."""
    if side == HUMAN_SIDE_RANDOM:
        return random.choice((True, False))
    return side != SIDE_BLACK


@router.post("/new")
async def new_game(payload: dict, request: Request) -> dict:
    hve = await _get_hve(request)
    s = request.app.state.settings

    human_white = _resolve_human_white(payload.get("human_side", s.human_side))
    tc = _time_control(payload, s)
    await _cancel_ai_analysis(request)
    seed_fen, book = await _resolve_book(s)
    try:
        game_id = await hve.new_game(
            human_white=human_white,
            tc=tc,
            player_name=_player_name(s),
            start_fen=seed_fen,
            book=book,
        )
    except FileNotFoundError as e:
        raise _engine_not_found(e) from e
    return {_GAME_ID_KEY: game_id, "human_white": human_white}


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


def _parse_import_payload(payload: dict) -> tuple[ImportedPosition, str]:
    """Parse a FEN/PGN payload; returns the position and the detected format.
    `format` may be 'fen', 'pgn', or 'auto' (the default) -- under 'auto'
    we try FEN first, then PGN, and report which one succeeded so the UI
    can highlight the matching tab."""
    fmt = payload.get(_FORMAT_KEY, _FMT_AUTO)
    text = payload.get(_TEXT_KEY, "")
    if fmt not in _VALID_FORMATS:
        raise bad_request("format must be 'fen', 'pgn', or 'auto'")
    if not isinstance(text, str):
        raise bad_request(f"missing '{_TEXT_KEY}'")
    size = len(text.encode("utf-8"))
    if size > MAX_IMPORT_TEXT_BYTES:
        log.warning("rejected oversized import: %d bytes (cap %d)", size, MAX_IMPORT_TEXT_BYTES)
        raise bad_request(f"text too large ({size} bytes; max {MAX_IMPORT_TEXT_BYTES})")
    if fmt != _FMT_AUTO:
        parse = parse_fen if fmt == FMT_FEN else parse_pgn
        try:
            return parse(text), fmt
        except PositionImportError as e:
            raise bad_request(str(e)) from e
    try:
        return parse_fen(text), FMT_FEN
    except PositionImportError as fen_err:
        try:
            return parse_pgn(text), FMT_PGN
        except PositionImportError:
            # Surface the FEN error: the PGN parser is noisier, and FEN's
            # "expected 8 rows" is the clearer message for a pasted half-FEN.
            raise bad_request(str(fen_err)) from fen_err


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
    supplied_id = payload.get(_GAME_ID_KEY) if isinstance(payload, dict) else None
    if supplied_id is not None and not isinstance(supplied_id, str):
        raise bad_request(f"{_GAME_ID_KEY} must be a string")
    existing = recents.get(view_hash)
    if existing is not None:
        stored_id = existing[0].get(ROW_GAME_ID)
        if supplied_id is not None and stored_id is not None and supplied_id != stored_id:
            log.warning(
                "import: game_id assertion failed for hash=%s "
                "stored=%s supplied=%s",
                view_hash, stored_id, supplied_id,
            )
            raise conflict({
                **error_detail("game_id_mismatch", "supplied game_id does not match stored row"),
                "stored_game_id": stored_id,
                "supplied_game_id": supplied_id,
            })
        return stored_id or supplied_id or str(uuid.uuid4())
    return supplied_id or str(uuid.uuid4())


@router.post("/import/validate")
async def import_validate(payload: dict) -> dict:
    """Parse a FEN/PGN payload and report the resulting position. Read-only.
    Returns ``hash`` (SHA-256 of stripped text) so the caller can compare
    against the currently viewed game before committing a full import."""
    pos, detected = _parse_import_payload(payload)
    position = {
        f.name: getattr(pos, f.name) for f in fields(pos) if f.name != _PARSED_GAME_FIELD
    }
    return {
        **position,
        _DETECTED_FORMAT_KEY: detected,
        _HASH_KEY: _import_hash(pos, detected, payload.get(_TEXT_KEY, "")),
    }


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
    pos, detected = _parse_import_payload(payload)
    hve = await _get_hve(request)
    headers = pos.headers or {}
    raw_text = payload.get(_TEXT_KEY, "")
    summary = pos.summary or {}
    # Opening imports carry {eco, name}: label the game by the opening and
    # name the sides player-vs-engine per gameplay settings (instead of the
    # dataset PGN's "?" headers).
    opening = payload.get(_OPENING_KEY)
    white_name = headers.get(TAG_WHITE)
    black_name = headers.get(TAG_BLACK)
    if opening is not None:
        if not isinstance(opening, dict):
            raise bad_request(f"{_OPENING_KEY} must be an object")
        eco = opening.get("eco", "")
        name = opening.get("name", "")
        if not isinstance(eco, str) or not isinstance(name, str):
            raise bad_request(f"{_OPENING_KEY} eco/name must be strings")
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
                SIDE_WHITE: white_name,
                SIDE_BLACK: black_name,
                _OPENING_KEY: f"{eco} {name}".strip(),
            }
    view_hash = _import_hash(pos, detected, raw_text)
    recents = request.app.state.recent_imports
    game_id = _resolve_game_id_for_import(recents, payload, view_hash)
    # Used by x-game nav to open parent/child at the fork ply in a single
    # round-trip, which avoids an animation flicker when the FEN at the
    # target ply is identical to what was on screen.
    land_at_ply = _land_at_ply(payload)
    await _cancel_ai_analysis(request)
    with _runtime_error_is_bad_request():
        game_id = await hve.enter_view_mode(
            ViewModeParams(
                start_fen=pos.start_fen,
                moves_uci=pos.moves_uci,
                clock_history=pos.clock_history,
                final_white_time=pos.final_white_time,
                final_black_time=pos.final_black_time,
                white_name=white_name,
                black_name=black_name,
                eval_history=pos.eval_history,
                comments=pos.comments,
                root_comment=pos.root_comment,
                pgn_result=headers.get(TAG_RESULT),
                pgn_termination=headers.get(TAG_TERMINATION),
                view_hash=view_hash,
                view_summary=summary,
                view_original_text=raw_text if detected == FMT_PGN else None,
            ),
            game_id=game_id,
            land_at_ply=land_at_ply,
        )
    # Record in recent-imports under the detected format (matters for
    # auto: it's "fen" or "pgn" by now).
    h = await recents.save(
        fmt=detected,
        text=raw_text,
        summary=summary,
        game_id=game_id,
        precomputed_hash=view_hash,
    )
    return {_GAME_ID_KEY: game_id, _VIEWING_KEY: True, _HASH_KEY: h, _SUMMARY_KEY: summary}


@router.get("/recent-imports")
async def list_recent_imports(request: Request) -> dict:
    """Return the recent-imports index sorted by ts desc.

    No blobs are returned -- callers GET /game/recent-imports/{hash} to
    fetch the text of an entry. Behind the same auth token as the rest
    of /game/*."""
    return {"entries": request.app.state.recent_imports.list()}


def _recent_import_payload(recents, h, game_id, row: dict, text: str) -> dict:
    """One recent import plus its x-game navigation fields."""
    return {
        _HASH_KEY: h,
        _GAME_ID_KEY: game_id,
        ROW_FORMAT: row[ROW_FORMAT],
        ROW_SUMMARY: row[ROW_SUMMARY],
        ROW_TS: row[ROW_TS],
        _TEXT_KEY: text,
        ROW_PARENT_GAME_ID: row.get(ROW_PARENT_GAME_ID),
        "parent_summary": recents.parent_summary_of(game_id) if game_id else None,
        ROW_FORK_PLY: row.get(ROW_FORK_PLY),
        _CHILDREN_KEY: recents.children_of(game_id) if game_id else [],
    }


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
        raise not_found(_NOT_FOUND)
    row, text = got
    h = recents.hash_for_id(game_id)
    if h is not None:
        await recents.touch(h)
    return _recent_import_payload(recents, h, game_id, row, text)


@router.get(_RECENT_IMPORT_PATH)
async def get_recent_import(h: str, request: Request) -> dict:
    """Return the full text + metadata for a single recent import.

    Side effect: bumps ``ts`` so frequently revisited entries stay at
    the top of the recents list (the dropdown shows newest first and
    eviction drops oldest)."""
    recents = request.app.state.recent_imports
    got = recents.get(h)
    if got is None:
        raise not_found(_NOT_FOUND)
    row, text = got
    game_id = row.get(ROW_GAME_ID)
    if game_id is not None:
        await recents.scrub_dangling_parent(game_id)
        # Re-read after potential scrub.
        got = recents.get(h)
        if got is None:
            raise not_found(_NOT_FOUND)
        row, text = got
    await recents.touch(h)
    return _recent_import_payload(recents, h, game_id, row, text)


@router.delete(_RECENT_IMPORT_PATH)
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
        raise conflict({_ERROR_KEY: "in_view"})
    result = await store.remove(h)
    if result.status is RemoveStatus.NOT_FOUND:
        raise not_found(_NOT_FOUND)
    if result.status is RemoveStatus.BLOCKED_BY_REFS:
        raise conflict({_ERROR_KEY: "has_children", _CHILDREN_KEY: result.children})
    if in_view:
        await hve.close_view()
    return _OK


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
    parent_game_id before game-end / edit-commit.
    """
    hve = await _get_hve(request)
    result = hve.get_pgn_text()
    if result is None:
        raise conflict("no game to export")
    pgn_text, filename = result
    # Best-effort recents write. View-mode games are already in
    # recents (or were imported there); export_to_recents is a no-op
    # for them.
    await hve.export_to_recents()
    return Response(
        content=pgn_text,
        media_type=_PGN_MEDIA_TYPE,
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
    land_at_ply = _land_at_ply(payload)
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
    with _runtime_error_is_bad_request():
        game_id = await hve.enter_view_mode(
            ViewModeParams(
                start_fen=start_fen,
                moves_uci=moves_uci,
                clock_history=clock_history or None,
                final_white_time=white_time,
                final_black_time=black_time,
                eval_history=eval_history if any(e is not None for e in eval_history) else None,
                white_name=summary[SIDE_WHITE] if summary else None,
                black_name=summary[SIDE_BLACK] if summary else None,
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
    return {_GAME_ID_KEY: game_id, _VIEWING_KEY: True}


@router.post("/view/first")
async def view_first(request: Request) -> dict:
    hve = await _get_hve(request)
    with _runtime_error_is_bad_request():
        await hve.view_first()
    return _OK


@router.post("/view/back")
async def view_back(request: Request) -> dict:
    hve = await _get_hve(request)
    with _runtime_error_is_bad_request():
        await hve.view_back()
    return _OK


@router.post("/view/forward")
async def view_forward(request: Request) -> dict:
    hve = await _get_hve(request)
    with _runtime_error_is_bad_request():
        await hve.view_forward()
    return _OK


@router.post("/view/last")
async def view_last(request: Request) -> dict:
    hve = await _get_hve(request)
    with _runtime_error_is_bad_request():
        await hve.view_last()
    return _OK


@router.post("/view/goto")
async def view_goto(payload: dict, request: Request) -> dict:
    hve = await _get_hve(request)
    ply = payload.get("ply")
    if not isinstance(ply, int):
        raise bad_request("missing or non-integer 'ply'")
    with _runtime_error_is_bad_request():
        await hve.view_goto(ply)
    return _OK


@router.post("/view/play-from-here")
async def view_play_from_here(payload: dict, request: Request) -> dict:
    """Exit view mode by seeding a fresh play game from plies 0..cursor."""
    hve = await _get_hve(request)
    s = request.app.state.settings
    tc = _time_control(payload, s)
    inherit_clocks = bool(payload.get("inherit_pgn_clocks", s.inherit_pgn_clocks))
    await _cancel_ai_analysis(request)
    with _runtime_error_is_bad_request():
        try:
            game_id = await hve.play_from_here(
                tc=tc, inherit_clocks=inherit_clocks, player_name=_player_name(s),
            )
        except FileNotFoundError as e:
            raise _engine_not_found(e) from e
    return {_GAME_ID_KEY: game_id, _VIEWING_KEY: False}


@router.post("/view/resume-play")
async def view_resume_play(request: Request) -> dict:
    """Exit view mode back into the SAME play game suspended by /view/start
    (no fork). 400 when there is no suspended game to resume."""
    hve = await _get_hve(request)
    await _cancel_ai_analysis(request)
    with _runtime_error_is_bad_request():
        game_id = await hve.resume_play()
    return {_GAME_ID_KEY: game_id, _VIEWING_KEY: False}


@router.post("/edit/start")
async def edit_start(request: Request) -> dict:
    hve = await _get_hve(request)
    await _cancel_ai_analysis(request)
    with _runtime_error_is_bad_request():
        fen = await hve.enter_edit_mode()
    return {_FEN_KEY: fen}


@router.post("/edit/commit")
async def edit_commit(payload: dict, request: Request) -> dict:
    hve = await _get_hve(request)
    fen = payload.get(_FEN_KEY)
    if not isinstance(fen, str) or not fen:
        raise bad_request(f"missing '{_FEN_KEY}'")
    apply_comment = bool(payload.get("apply_comment", False))
    comment_text = payload.get(_COMMENT_TEXT_KEY, "")
    if not isinstance(comment_text, str):
        raise bad_request(f"'{_COMMENT_TEXT_KEY}' must be a string")
    if len(comment_text) > MAX_ANNOTATION_LENGTH:
        raise bad_request(f"'{_COMMENT_TEXT_KEY}' exceeds maximum length")
    prev_id = hve.game_id
    prev_hash = request.app.state.recent_imports.hash_for_id(prev_id) if prev_id else None
    # Capture the fork link before commit_edit -- the FEN-change branch
    # routes through enter_view_mode which would clear it (correct
    # behavior: FEN edit == new lineage). Annotation-only commit does
    # NOT go through enter_view_mode, so the live HVE link stays, but
    # we capture here anyway so the call site is symmetric.
    pre_commit_fork_link = hve.fork_link
    with _runtime_error_is_bad_request():
        result = await hve.commit_edit(
            fen, apply_comment=apply_comment, comment_text=comment_text,
        )
    recents = request.app.state.recent_imports
    h: str | None = None
    summary = None
    if result.changed is EditChange.FEN:
        # FEN edit == new lineage; do NOT carry the fork link forward.
        summary = parse_fen(fen).summary
        h = await recents.save(
            fmt=FMT_FEN, text=fen, summary=summary, game_id=result.game_id,
        )
    elif result.changed is EditChange.COMMENT:
        # Annotation-only commit: same game_id, content hash changed.
        # Replace the pre-edit recents row (if any) with the new PGN.
        # Carry the fork link through so a previously-unsaved child
        # gets promoted into recents with the link attached.
        summary = result.summary
        parent_game_id, fork_ply = (
            pre_commit_fork_link
            if pre_commit_fork_link is not None
            else (None, None)
        )
        h = await recents.replace_at(
            old_hash=prev_hash,
            fmt=FMT_PGN,
            text=result.pgn_text,
            summary=summary or {},
            game_id=result.game_id,
            precomputed_hash=result.pgn_hash,
            parent_game_id=parent_game_id,
            fork_ply=fork_ply,
        )
        # Consumed -- clear the live link so it doesn't re-fire on a
        # subsequent transition.
        hve.clear_fork_link()
    return {_GAME_ID_KEY: result.game_id, _HASH_KEY: h, _SUMMARY_KEY: summary}


@router.post("/edit/cancel")
async def edit_cancel(request: Request) -> dict:
    hve = await _get_hve(request)
    with _runtime_error_is_bad_request():
        game_id = await hve.cancel_edit()
    return {_GAME_ID_KEY: game_id}


async def _republish_best_effort(hve) -> None:
    try:
        await hve.republish_state()
    except Exception:
        log.warning("republish_state failed", exc_info=True)


@router.post("/move")
async def submit_move(payload: dict, request: Request) -> dict:
    hve = await _get_hve(request)
    uci = payload.get("uci")
    if not isinstance(uci, str) or not uci:
        raise bad_request("missing 'uci'")
    try:
        await hve.submit_move(uci)
    except RuntimeError as e:
        # Republish so a client whose UI moved optimistically can snap back.
        await _republish_best_effort(hve)
        raise bad_request(str(e)) from e
    return _OK


@router.post("/resign")
async def resign(request: Request) -> dict:
    hve = await _get_hve(request)
    await hve.resign()
    return _OK


@router.post("/sync")
async def sync(request: Request) -> dict:
    """Re-emit the current state (board + clock) so a freshly-mounted client
    can resync without server-side mutation."""
    s = request.app.state
    if s.hve is not None:
        await _republish_best_effort(s.hve)
    return _OK


@router.get("/status")
async def game_status(request: Request) -> dict:
    """Authoritative state for the client's discard/replace confirmations
    (tournament replay). Client mirrors of this die on page reload; this
    endpoint doesn't. Never creates an HVE."""
    s = request.app.state
    if s.hve is None:
        return idle_status()
    return await s.hve.status()


@router.post("/pause")
async def pause(request: Request) -> dict:
    hve = await _get_hve(request)
    with _runtime_error_is_bad_request():
        await hve.pause()
    return _OK


@router.post("/resume")
async def resume(request: Request) -> dict:
    hve = await _get_hve(request)
    with _runtime_error_is_bad_request():
        await hve.resume()
    return _OK


def _clear_replay(request: Request) -> None:
    coord = ai_coordinator(request.app.state)
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
    with _runtime_error_is_bad_request():
        await hve.start_analysis()
    # Drop any prior session's buffer before a new one begins, in case
    # the previous /analysis/stop was skipped (mode toggle, crash).
    _clear_replay(request)
    if getattr(request.app.state.settings, "ai_enabled", False):
        await start_ai_turn(request)
    return _OK


@router.get("/analysis/replay")
async def analysis_replay(request: Request) -> dict:
    """Return the buffered AI events for the in-flight analysis turn.
    Lets a client reconnecting mid-analysis rebuild the panel from the
    bus events it missed. Empty list when no analysis is live."""
    coord = ai_coordinator(request.app.state)
    if coord is None:
        return {_EVENTS_KEY: []}
    return {_EVENTS_KEY: coord.replay()}


@router.post("/analysis/stop")
async def analysis_stop(request: Request) -> dict:
    # Cancel before flipping out of ANALYZING so the agent stops cleanly.
    await _cancel_ai_analysis(request)
    hve = await _get_hve(request)
    with _runtime_error_is_bad_request():
        await hve.stop_analysis()
    return _OK


@router.post("/switch-sides")
async def switch_sides(request: Request) -> dict:
    hve = await _get_hve(request)
    with _runtime_error_is_bad_request():
        await hve.switch_sides()
    return _OK


@router.post("/takeback")
async def takeback(request: Request) -> dict:
    s = request.app.state.settings
    if not getattr(s, "allow_takeback", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="take-back is disabled in settings",
        )
    hve = await _get_hve(request)
    with _runtime_error_is_bad_request():
        await hve.takeback()
    return _OK
