"""Engine-backed tools for the AI analysis agent.

`analyze` is the first real tool: spawns a throwaway UCI engine,
configures a depth-only search limit, drains info events until the
engine signals bestmove (or the cancel token flips), and returns a
structured eval record. Searches are depth-only by design -- a time
limit makes the bestmove non-deterministic on near-equal candidates.

Per spec §Architecture: one throwaway engine per analyze call (pool
later if perf demands). Hard caps live as named consts + SV_ env vars
(no magic numbers); out-of-range requests are clamped, not rejected,
so the agent never stalls on a guardrail.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable

import chess
import chess.engine

from ..chess.results import SIDE_BLACK, SIDE_WHITE
from ..config import (
    _DEFAULT_AI_ANALYZE_MAX_DEPTH,
    _DEFAULT_AI_VERIFICATION_DEPTH,
)
from ..env_utils import env_int
from ..events import EVT_ENGINE_SEARCH_START, Event, EventBus
from ..llm import ToolSpec
from ..llm.cancel import CancelToken
from .engine_analysis import (
    log_spawn_failure,
    resolve_eval_pov_white_or_stm,
    spawn_analysis_engine,
)
from .engine_info_pump import pump_engine_info
from .engine_supervisor import EngineSupervisor


log = logging.getLogger(__name__)


# TODO: dynamic cap -- allow deeper searches when few pieces remain
# (endgames resolve deep cheaply and benefit from it).
# Hard cap -- the agent can request any depth, but we clamp to this.
# Settings (ai_analyze_max_depth) override per call; the env default is the
# fallback when no settings are wired (ops/tests). Searches are depth-only
# (no time limit): a timer firing before the depth completes makes the
# bestmove non-deterministic.
MAX_DEPTH = env_int("SV_AI_ANALYZE_MAX_DEPTH", _DEFAULT_AI_ANALYZE_MAX_DEPTH)

# Floor for the end-of-turn recommendation check: it searches at least
# this deep regardless of the (often shallow) depth the model picked, so
# the authoritative verdict isn't a shallow rubber-stamp. Settings
# (ai_verification_depth) override per call; env is the fallback.
VERIFICATION_DEPTH = env_int("SV_AI_VERIFICATION_DEPTH", _DEFAULT_AI_VERIFICATION_DEPTH)

# recommend_move dominance margin: rival must beat candidate by strictly
# more than this many cp (STM POV) to reject. Filters cosmetic 1-30 cp
# preferences while still catching real blunders. Mate scores ignore it.
_DEFAULT_RECOMMEND_MARGIN_CP = 50
RECOMMEND_MARGIN_CP = env_int("SV_AI_RECOMMEND_MARGIN", _DEFAULT_RECOMMEND_MARGIN_CP)

# Default search depth when the caller omits one. 20 plies gives reliable
# tactical resolution; lower values surface noisy bestmoves. Model-supplied
# depth is floored at MIN_DEPTH below.
_DEFAULT_DEPTH = 20

# Floor on model-supplied depth -- shallower searches rank candidates poorly.
_DEFAULT_MIN_DEPTH = 10
MIN_DEPTH = env_int("SV_AI_MIN_DEPTH", _DEFAULT_MIN_DEPTH)

# top_moves: hard cap on the model-supplied candidate list length.
# Each candidate runs one sequential search; cost scales linearly.
_DEFAULT_TOP_MOVES_MAX_N = 5
TOP_MOVES_MAX_N = env_int("SV_AI_TOP_MOVES_MAX_N", _DEFAULT_TOP_MOVES_MAX_N)

# Per-candidate default depth -- match analyze's default; with the
# model-supplied list capped at TOP_MOVES_MAX_N, worst-case cost is
# bounded and shallower defaults were producing weak candidate ranking.
_DEFAULT_TOP_MOVES_DEPTH = _DEFAULT_DEPTH

# report_line: hard cap on the reported continuation length. A line is
# replayed move-by-move (no engine); the cap just bounds payload size.
_DEFAULT_REPORT_LINE_MAX_PLIES = 40
REPORT_LINE_MAX_PLIES = env_int("SV_AI_REPORT_LINE_MAX_PLIES", _DEFAULT_REPORT_LINE_MAX_PLIES)


EngineLauncher = Callable[[], EngineSupervisor]
GameIdProvider = Callable[[], str | None]
BoardProvider = Callable[[], chess.Board | None]
# Provider that returns the live app settings object (or None). Lets
# the throwaway analysis engine inherit Threads/Hash/Syzygy/etc. from
# the same source HVE's analysis path uses.
SettingsProvider = Callable[[], Any]
AnalyzeTool = Callable[..., Awaitable[dict[str, Any]]]


def _settings_int(
    settings_provider: SettingsProvider | None, attr: str, fallback: int,
) -> int:
    """Read an int setting via the provider, falling back to the module
    const when no provider/setting is wired. One source for the
    settings-or-default depth-cap lookup the tools share."""
    settings = settings_provider() if settings_provider else None
    value = getattr(settings, attr, None) if settings is not None else None
    return int(value) if isinstance(value, int) else fallback


def _max_depth(settings_provider: SettingsProvider | None) -> int:
    return _settings_int(settings_provider, "ai_analyze_max_depth", MAX_DEPTH)


def _verification_depth(settings_provider: SettingsProvider | None) -> int:
    return _settings_int(settings_provider, "ai_verification_depth", VERIFICATION_DEPTH)

# Default fallback game_id when the tool runs outside a live HVE
# session (e.g. unit tests, future post-game path). engine_info events
# still need *some* game_id so the client's per-session muxing works.
_ANALYZE_GAME_ID_FALLBACK = "ai-analyze"

# Move-notation constraint reused in every tool description that takes
# a move string. PGN-style continuation marks ('...d6', '23...Nf6') are
# not SAN; the server strips them defensively (see _strip_move_prefix)
# but the prompt steers models away to keep tool inputs clean.
_MOVE_NOTATION_CONSTRAINT = (
    " Use bare UCI or SAN -- no PGN continuation prefix "
    "('...d6' should be 'd6')."
)

# Tool names -- single source of truth (the ToolSpecs below use them, and
# the coordinator imports them rather than hardcoding string literals).
ANALYZE_TOOL_NAME = "analyze"
MATERIAL_TOOL_NAME = "material"
TOP_MOVES_TOOL_NAME = "top_moves"
PIECE_AT_TOOL_NAME = "piece_at"
VALIDATE_MOVE_TOOL_NAME = "validate_move"
RECOMMEND_MOVE_TOOL_NAME = "recommend_move"
REPORT_LINE_TOOL_NAME = "report_line"

# Shared description for the `fen` arg across every FEN-taking tool spec
# (analyze, material). One source so the startpos affordance stays in sync.
_FEN_ARG_DESCRIPTION = "FEN string, or 'startpos' for the initial position."


# Wire-shape ToolSpec describing this tool to the model. Lives next to
# the implementation so prompt text + schema + behavior move together;
# app.py only wires (spec, callable) into the registry.
TOP_MOVES_TOOL_SPEC = ToolSpec(
    name=TOP_MOVES_TOOL_NAME,
    description=(
        "Rank YOUR candidate moves in the live position. You supply "
        "2-5 moves; each is searched and returned sorted "
        f"best-first for the side to move (capped at {TOP_MOVES_MAX_N}). "
        "Fields per entry: move_uci, move_san, score_cp, score_text, "
        "score_pawns, mate, depth, pv (eval fields are white-POV). "
        "Illegal candidates come back as per-entry errors."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "moves": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Candidate moves in UCI or SAN. Capped at "
                    f"{TOP_MOVES_MAX_N}; extras dropped."
                    + _MOVE_NOTATION_CONSTRAINT
                ),
            },
            "depth": {
                "type": "integer",
                "description": (
                    "Per-candidate depth. Go deeper when candidates score "
                    "close -- shallow ranking is unreliable."
                ),
            },
        },
        "required": ["moves"],
    },
)


_PIECE_AT_CARD = (
    "Any piece-on-square claim is worth confirming first -- explicit "
    "(\"knight on f3\") or implied (centralize, push, capture, defend, "
    "pin, fork, etc). Result: piece symbol (upper=white, lower=black) or "
    "null. Defaults to the live position; pass `fen` to read a square in "
    "any position you are reasoning about."
)


PIECE_AT_TOOL_SPEC = ToolSpec(
    name=PIECE_AT_TOOL_NAME,
    description=(
        "Piece on a square, or null. Reads the live position by default; "
        "pass `fen` to read any position. Call before naming any "
        "piece-on-square in prose."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "square": {
                "type": "string",
                "description": (
                    "Algebraic square name, e.g. 'e4', 'a1', 'h8'. "
                    "Case-insensitive."
                ),
            },
            "fen": {
                "type": "string",
                "description": (
                    "Position to read, or 'startpos'. Omit to use the live "
                    "position."
                ),
            },
        },
        "required": ["square"],
    },
    card=_PIECE_AT_CARD,
)


_VALIDATE_MOVE_CARD = (
    "Any move named as playable in the live position is worth confirming "
    "-- live position only, moves inside calculated lines aren't. Result: "
    "legal (bool), uci, san. A legal=false move is not playable here."
)


VALIDATE_MOVE_TOOL_SPEC = ToolSpec(
    name=VALIDATE_MOVE_TOOL_NAME,
    description=(
        "Check if a move (UCI or SAN) is legal in the live position. "
        "Call before naming any move as playable in the current "
        "position."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "move": {
                "type": "string",
                "description": (
                    "Move in UCI (e.g. 'g1f3', 'e7e8q') or SAN (e.g. "
                    "'Nf3', 'O-O', 'exd5')."
                    + _MOVE_NOTATION_CONSTRAINT
                ),
            },
        },
        "required": ["move"],
    },
    card=_VALIDATE_MOVE_CARD,
)


ANALYZE_TOOL_SPEC = ToolSpec(
    name=ANALYZE_TOOL_NAME,
    description=(
        "Deep search of a position. Returns white-POV eval: score_cp, "
        "score_text, mate (signed plies when forced), depth, pv, bestmove. "
        "Use the numbers internally; eval discipline still applies."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "fen": {
                "type": "string",
                "description": _FEN_ARG_DESCRIPTION,
            },
            "depth": {
                "type": "integer",
                "description": (
                    "Search depth. Go deeper on sharp or close positions -- "
                    "a shallow search misjudges tactics."
                ),
            },
        },
        "required": ["fen"],
    },
)


# Piece types reported by `material`. Kings are omitted -- always one per
# side, so they carry no material signal.
_MATERIAL_PIECE_TYPES = (
    chess.PAWN, chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN,
)


MATERIAL_TOOL_SPEC = ToolSpec(
    name=MATERIAL_TOOL_NAME,
    description=(
        "Ground a material claim with exact piece counts before stating "
        "it. Returns per-color counts keyed by piece name (pawn, knight, "
        "bishop, rook, queen); kings are omitted. Pass a FEN. Raw counts "
        "only -- no values assigned: you judge the balance yourself."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "fen": {
                "type": "string",
                "description": _FEN_ARG_DESCRIPTION,
            },
        },
        "required": ["fen"],
    },
)


def _parse_fen(raw: str) -> chess.Board:
    """`startpos` is a convenience alias for the standard initial position
    that mirrors the UCI/PGN convention; anything else must be a real
    FEN string the python-chess parser accepts."""
    if raw == "startpos":
        return chess.Board()
    return chess.Board(fen=raw)


def _parse_fen_arg(input_: dict) -> tuple[chess.Board | None, dict | None]:
    """Read+parse the `fen` arg for FEN-taking tools (analyze, material).
    Returns (board, None) on success or (None, error_envelope). Strips
    surrounding whitespace first so '  startpos  ' parses like 'startpos'."""
    fen = input_.get("fen")
    if not isinstance(fen, str) or not fen.strip():
        return None, {"error": "missing_fen"}
    try:
        return _parse_fen(fen.strip()), None
    except ValueError as exc:
        return None, {"error": "invalid_fen", "detail": str(exc)}


def _depth_limit(
    input_: dict, default_depth: int, max_depth: int,
) -> tuple[chess.engine.Limit, dict]:
    """Build a depth-only chess.engine.Limit from the agent's requested
    depth (clamped to `max_depth`), falling back to `default_depth`. Returns
    the Limit and a dict echoing the effective depth (tool output + test
    assertions). Time limits are intentionally not supported: a timer that
    fires before the depth is reached makes the bestmove non-deterministic,
    which flips the pick on near-equal candidates."""
    raw_depth = input_.get("depth")
    if raw_depth is not None:
        try:
            depth = max(min(MIN_DEPTH, max_depth), min(int(raw_depth), max_depth))
        except (TypeError, ValueError):
            depth = default_depth
    else:
        depth = default_depth
    return chess.engine.Limit(depth=depth), {"depth": depth}


def _score_to_cp(score: chess.engine.PovScore | None) -> dict:
    """Normalize a python-chess PovScore into wire fields.

    Surfaced fields:
    - `score_cp`: raw centipawns, integer, white POV (positive = white
      better). Authoritative numeric value.
    - `score_pawns`: same value in pawn units, float to 2 decimals.
      Belt-and-suspenders for LLM consumers that have been observed to
      treat `score_cp` as pawns (a 100x interpretation error). Exposing
      both removes the ambiguity at the wire.
    - `score_text`: presentation-ready string, e.g. "+0.02", "-1.45",
      or "mate in 3". Drop-in for prose so the model does not have to
      do arithmetic.
    - `mate`: signed plies-to-mate when present (overrides cp meaning).

    POV: white-relative regardless of side to move. Matches the view-mode
    convention; user-relative flipping is a play-side concern.
    """
    if score is None:
        return {}
    s = score.white()
    out: dict = {}
    cp = s.score(mate_score=None)
    if cp is not None:
        out["score_cp"] = cp
        out["score_pawns"] = round(cp / 100.0, 2)
        out["score_text"] = f"{cp / 100.0:+.2f}"
    mate = s.mate()
    if mate is not None:
        out["mate"] = mate
        # Mate beats cp for the human-readable string. python-chess uses
        # signed plies, but coaching prose works better in moves: "mate
        # in 3" reads cleaner than "mate in 6 plies".
        sign = "+" if mate > 0 else "-"
        moves = (abs(mate) + 1) // 2
        out["score_text"] = f"{sign}M{moves}"
    return out


def _pv_to_uci(board: chess.Board, pv: list[chess.Move] | None) -> list[str]:
    if not pv:
        return []
    return [m.uci() for m in pv]


# Sort key for STM-POV PovScore: mate-for-stm > +cp > -cp > mate-against.
# Used to rank top_moves candidates without re-scoring each comparison.
_MATE_RANK = 10**9


def _stm_pov_sort_key(
    score: chess.engine.PovScore | None, stm: chess.Color,
) -> int | None:
    """STM-POV ranking value (higher = better for the side to move), or
    None for a scoreless candidate (caller sorts those to the bottom).
    STM POV means the caller sorts descending for either side."""
    if score is None:
        return None
    s = score.pov(stm)
    mate = s.mate()
    if mate is not None:
        return (_MATE_RANK - abs(mate)) if mate > 0 else (-_MATE_RANK + abs(mate))
    cp = s.score(mate_score=None)
    return cp if cp is not None else 0


class _SearchError(RuntimeError):
    """Internal error from _run_one_search carrying a structured kind
    (engine_spawn_failed or engine_terminated). Callers catch, convert
    to the tool's error envelope. Local-only (not exported)."""
    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


# Key into SearchCache: the position (EPD) plus the root-move restriction. A
# free search and a candidate-restricted one on the same position are
# different searches (different `searchmoves`), so root_moves is part of it.
_SearchKey = tuple[str, frozenset]


class SearchCache:
    """Per-turn cache of completed engine searches, shared across the
    engine-backed tools. A position+restriction searched once this turn is
    reused if the cached depth >= the request; the coordinator clears it at
    turn start (the position is stable within a turn, which keys safely).

    A cache hit spawns no engine, so no engine_info events fire (the PV panel
    won't re-animate -- accepted). Cancelled searches are never cached: a
    partial result must not satisfy a later request."""

    def __init__(self) -> None:
        self._entries: dict[_SearchKey, tuple[int, chess.engine.InfoDict]] = {}

    def clear(self) -> None:
        self._entries = {}

    @staticmethod
    def _key(board: chess.Board, root_moves: list[chess.Move] | None) -> _SearchKey:
        # epd(), not fen(): drops the halfmove/fullmove counters so the same
        # board with different clocks shares a key. Matches the dedup
        # normalizer (_canonical_fen) -- these searches are position- not
        # history-dependent.
        roots = frozenset(m.uci() for m in root_moves) if root_moves else frozenset()
        return (board.epd(), roots)

    async def get_or_search(
        self,
        engine_launcher: EngineLauncher,
        board: chess.Board,
        limit: chess.engine.Limit,
        *,
        bus: EventBus,
        game_id: str,
        cancel_token: CancelToken,
        root_moves: list[chess.Move] | None = None,
        settings_provider: SettingsProvider | None = None,
    ) -> tuple[chess.engine.InfoDict, bool]:
        """Reuse a cached result when one this turn reached at least the
        requested depth; otherwise run the search and cache it if deeper than
        what's stored. Same return shape as _run_one_search. A reuse hit
        reports (info, cancelled=False)."""
        requested = limit.depth or 0
        key = self._key(board, root_moves)
        cached = self._entries.get(key)
        if cached is not None and cached[0] >= requested:
            log.info("search cache hit: depth %d >= %d, key=%s", cached[0], requested, key)
            return cached[1], False
        last_info, cancelled = await _run_one_search(
            engine_launcher, board, limit,
            bus=bus, game_id=game_id, cancel_token=cancel_token,
            root_moves=root_moves, settings_provider=settings_provider,
        )
        if not cancelled:
            reached = last_info.get("depth") or 0
            if cached is None or reached > cached[0]:
                self._entries[key] = (reached, last_info)
        return last_info, cancelled


async def _run_one_search(
    engine_launcher: EngineLauncher,
    board: chess.Board,
    limit: chess.engine.Limit,
    *,
    bus: EventBus,
    game_id: str,
    cancel_token: CancelToken,
    root_moves: list[chess.Move] | None = None,
    settings_provider: SettingsProvider | None = None,
) -> tuple[chess.engine.InfoDict, bool]:
    """Spawn a throwaway engine, run one search, return (last_info, cancelled).
    Raises _SearchError on spawn/mid-search engine death -> error envelope.

    `root_moves`: restricts the engine to these moves at the root (UCI
    `searchmoves`); top_moves uses it to score one candidate without push/pop.
    `settings_provider`: live settings so the engine inherits Threads/Hash/
    SyzygyPath; None falls back to no overrides (test doubles).

    Emits engine_info but NOT engine_search_start -- the caller clears the
    panel (analyze once; top_moves once for the whole batch)."""
    sup = engine_launcher()
    settings = settings_provider() if settings_provider else None
    try:
        engine, cleanup = await spawn_analysis_engine(sup, settings)
    except Exception as exc:
        log_spawn_failure(exc, "search")
        raise _SearchError("engine_spawn_failed", str(exc)) from exc
    try:
        analysis_kwargs: dict = {"limit": limit}
        if root_moves:
            analysis_kwargs["root_moves"] = root_moves
        with await engine.analysis(board, **analysis_kwargs) as analysis:
            last_info, cancelled = await pump_engine_info(
                analysis,
                bus=bus,
                game_id=game_id,
                board=board,
                pov=resolve_eval_pov_white_or_stm(settings, board.turn),
                cancel_token=cancel_token,
            )
        return last_info, cancelled
    except chess.engine.EngineTerminatedError as exc:
        log.error("search: engine terminated mid-search")
        raise _SearchError("engine_terminated", str(exc)) from exc
    finally:
        # cleanup() is exception-safe by contract; no wrapping needed.
        await cleanup()


def make_analyze_tool(
    engine_launcher: EngineLauncher,
    bus: EventBus,
    game_id_provider: GameIdProvider | None = None,
    settings_provider: SettingsProvider | None = None,
    search_cache: SearchCache | None = None,
) -> AnalyzeTool:
    """Build the `analyze` async tool.

    `search_cache`: cross-tool search cache; defaults to a fresh always-miss
    one. `engine_launcher()`: fresh EngineSupervisor per call (resolution
    lives in app.py). `bus`: HVE's engine_info bus -- publishing there lights
    up the PV window and board arrow during a tool-call search.
    `game_id_provider()`: live game id (fallback id when None, so events still
    mux). `settings_provider()`: settings for Threads/Hash/SyzygyPath; None ok
    (tests)."""
    cache = search_cache or SearchCache()

    async def analyze(input_: dict, *, cancel_token: CancelToken) -> dict:
        board, err = _parse_fen_arg(input_)
        if err is not None:
            return err

        limit, limits_used = _depth_limit(
            input_, _DEFAULT_DEPTH, _max_depth(settings_provider)
        )

        game_id = (game_id_provider() if game_id_provider else None) or _ANALYZE_GAME_ID_FALLBACK
        await bus.publish(Event(kind=EVT_ENGINE_SEARCH_START, game_id=game_id, payload={}))

        try:
            last_info, cancelled = await cache.get_or_search(
                engine_launcher, board, limit,
                bus=bus, game_id=game_id, cancel_token=cancel_token,
                settings_provider=settings_provider,
            )
        except _SearchError as err:
            return {"error": err.kind, "detail": err.detail}

        out: dict = {"limits_used": limits_used}
        if cancelled:
            out["cancelled"] = True
        out.update(_score_to_cp(last_info.get("score")))
        depth = last_info.get("depth")
        if depth is not None:
            out["depth"] = depth
        nodes = last_info.get("nodes")
        if nodes is not None:
            out["nodes"] = nodes
        pv = _pv_to_uci(board, last_info.get("pv"))
        if pv:
            out["pv"] = pv
            out["bestmove"] = pv[0]
        return out

    return analyze


_PGN_CONTINUATION_RE = re.compile(r"^\s*(?:\d+\s*)?\.{2,3}\s*")


def _strip_move_prefix(raw: str) -> str:
    """Strip whitespace and any PGN-style continuation prefix
    ('...', '23...') so 'Nf6'/'...Nf6'/'23...Nf6' all parse the same.
    The prompt also asks for bare notation; this is defense-in-depth."""
    stripped = raw.strip()
    return _PGN_CONTINUATION_RE.sub("", stripped)


def _parse_move_or_error(
    board: chess.Board, candidate: str,
) -> tuple[chess.Move | None, str | None, str | None]:
    """Core UCI-then-SAN parse of an already prefix-stripped move string.
    Returns (move, None, None) on success, or (None, error_kind, detail).
    Single source of truth for the parse + exception->envelope mapping
    shared by top_moves / validate_move / recommend_move (each wraps the
    kind/detail in its own result shape)."""
    if not candidate:
        return None, "invalid_input", "empty move string"
    for parse in (board.parse_uci, board.parse_san):
        try:
            return parse(candidate), None, None
        except chess.IllegalMoveError as exc:
            return None, "illegal_move", str(exc)
        except (chess.InvalidMoveError, chess.AmbiguousMoveError):
            continue
    return None, "invalid_move", f"could not parse {candidate!r} as UCI or SAN"


# SAN leading-letter -> piece type, for naming the piece an illegal move
# meant. A bare-square/file token (no letter) means a pawn.
_SAN_PIECE_LETTERS = {
    "N": chess.KNIGHT, "B": chess.BISHOP, "R": chess.ROOK,
    "Q": chess.QUEEN, "K": chess.KING,
}


def _intended_piece_type(board: chess.Board, raw: str) -> int | None:
    """The piece type an illegal move string meant to move, or None when
    it can't be inferred. SAN names it by leading letter (default pawn);
    UCI names it by the piece standing on the from-square."""
    raw = raw.strip()
    if not raw:
        return None
    head = raw[0]
    if head in _SAN_PIECE_LETTERS:
        return _SAN_PIECE_LETTERS[head]
    if head in "abcdefgh":  # SAN pawn move ('d7', 'exd7')
        return chess.PAWN
    try:  # UCI: read the piece on the from-square
        from_sq = chess.parse_square(raw[:2].lower())
    except ValueError:
        return None
    piece = board.piece_at(from_sq)
    return piece.piece_type if piece is not None else None


def _legal_moves_for_piece(board: chess.Board, piece_type: int) -> list[str]:
    """SANs of every legal move by `piece_type` for the side to move, in
    python-chess move order. Empty when that piece has no legal move."""
    return [
        board.san(m) for m in board.legal_moves
        if (p := board.piece_at(m.from_square)) is not None
        and p.piece_type == piece_type
    ]


def _parse_candidate_move(board: chess.Board, raw: str) -> tuple[chess.Move | None, dict | None]:
    """Try UCI then SAN. Returns (move, None) on success, (None, error_entry)
    on failure. Error entry carries `move_input` so the model can match
    it back to the input list."""
    move, kind, detail = _parse_move_or_error(board, _strip_move_prefix(raw))
    if move is not None:
        return move, None
    return None, {"move_input": raw, "error": kind, "detail": detail}


def parse_move_canonical(board: chess.Board, raw: str) -> chess.Move | None:
    """Canonical Move for a UCI/SAN string, or None on failure. Shares the
    same prefix-strip + UCI-then-SAN parse as the move-taking tools, so
    every caller (tools + the ai_analysis gate/dedup) agrees on the UCI a
    given string maps to."""
    move, _kind, _detail = _parse_move_or_error(board, _strip_move_prefix(raw))
    return move


def parse_move_reporting(
    board: chess.Board, raw: str,
) -> tuple[chess.Move | None, str | None, str | None]:
    """Like parse_move_canonical but surfaces (kind, detail) on failure, so a
    caller can distinguish illegal_move (wrong side to move, blocked) from
    invalid_move (unparseable). Same prefix-strip + parse as every move tool."""
    return _parse_move_or_error(board, _strip_move_prefix(raw))


def make_top_moves_tool(
    engine_launcher: EngineLauncher,
    bus: EventBus,
    board_provider: BoardProvider,
    game_id_provider: GameIdProvider | None = None,
    settings_provider: SettingsProvider | None = None,
    search_cache: SearchCache | None = None,
) -> AnalyzeTool:
    """Deep-evaluate a model-supplied list of candidate moves. Each
    legal move runs a root-restricted search; illegal or unparseable
    moves come back as per-entry errors. Publishes one
    `engine_search_start` for the batch, then per-candidate
    engine_info events. `search_cache`: see make_analyze_tool."""
    cache = search_cache or SearchCache()

    async def top_moves(input_: dict, *, cancel_token: CancelToken) -> dict:
        board = board_provider()
        if board is None:
            return {"error": "no_live_position"}
        board = board.copy(stack=False)

        raw_moves = input_.get("moves")
        if not isinstance(raw_moves, list) or not raw_moves:
            return {"error": "invalid_input", "detail": "moves must be a non-empty list of strings"}
        truncated = len(raw_moves) > TOP_MOVES_MAX_N
        raw_moves = raw_moves[:TOP_MOVES_MAX_N]

        parsed: list[chess.Move] = []
        errors: list[dict] = []
        for raw in raw_moves:
            if not isinstance(raw, str):
                errors.append({"move_input": raw, "error": "invalid_input",
                               "detail": "move must be a string"})
                continue
            move, err = _parse_candidate_move(board, raw)
            if err is not None:
                errors.append(err)
            else:
                parsed.append(move)

        limit, limits_used = _depth_limit(
            input_, _DEFAULT_TOP_MOVES_DEPTH, _max_depth(settings_provider)
        )
        game_id = (game_id_provider() if game_id_provider else None) or _ANALYZE_GAME_ID_FALLBACK
        await bus.publish(Event(kind=EVT_ENGINE_SEARCH_START, game_id=game_id, payload={}))

        stm_is_white = board.turn == chess.WHITE
        cancelled_any = False
        entries: list[dict] = []
        for move in parsed:
            if cancel_token.cancelled:
                cancelled_any = True
                break
            try:
                last_info, cancelled = await cache.get_or_search(
                    engine_launcher, board, limit,
                    bus=bus, game_id=game_id, cancel_token=cancel_token,
                    root_moves=[move],
                    settings_provider=settings_provider,
                )
            except _SearchError as err:
                return {"error": err.kind, "detail": err.detail}
            entry: dict = {"move_uci": move.uci(), "move_san": board.san(move)}
            score = last_info.get("score")
            entry.update(_score_to_cp(score))
            depth = last_info.get("depth")
            if depth is not None:
                entry["depth"] = depth
            pv = _pv_to_uci(board, last_info.get("pv"))
            if pv:
                entry["pv"] = pv
            entry["_sort_key"] = _stm_pov_sort_key(score, board.turn)
            entries.append(entry)
            if cancelled:
                cancelled_any = True
                break

        # Scoreless candidates (no engine score) sort to the bottom in
        # both directions; only the scored ones go through the POV sort.
        scored = [c for c in entries if c["_sort_key"] is not None]
        scoreless = [c for c in entries if c["_sort_key"] is None]
        scored.sort(key=lambda c: c["_sort_key"], reverse=True)
        entries = scored + scoreless
        for c in entries:
            c.pop("_sort_key", None)

        out: dict = {
            "side_to_move": SIDE_WHITE if stm_is_white else SIDE_BLACK,
            "limits_used": limits_used,
            "candidates": entries,
        }
        if errors:
            out["errors"] = errors
        if truncated:
            out["truncated"] = True
        if cancelled_any:
            out["cancelled"] = True
        return out

    return top_moves


def make_piece_at_tool(board_provider: BoardProvider) -> AnalyzeTool:
    """Build the `piece_at` async tool. Reports what occupies a square --
    the model's self-check against hallucinated piece placements. Reads the
    live board (via board_provider) by default, or a supplied `fen` so the
    model can ground a square in any position it is reasoning about."""
    async def piece_at(input_: dict, *, cancel_token: CancelToken) -> dict:
        fen = input_.get("fen")
        if fen is not None:
            board, err = _parse_fen_arg(input_)
            if err is not None:
                return err
        else:
            board = board_provider()
            if board is None:
                return {"error": "no_live_position"}
        raw = input_.get("square")
        if not isinstance(raw, str) or not raw:
            return {"error": "invalid_square", "detail": "square must be a non-empty string"}
        try:
            sq = chess.parse_square(raw.lower())
        except ValueError as exc:
            return {"error": "invalid_square", "detail": str(exc)}
        piece = board.piece_at(sq)
        out: dict = {"square": chess.square_name(sq)}
        if piece is None:
            out["piece"] = None
        else:
            out["piece"] = {
                "type": chess.PIECE_NAMES[piece.piece_type],
                "color": "white" if piece.color == chess.WHITE else "black",
                "symbol": piece.symbol(),
            }
        return out

    return piece_at


def make_material_tool() -> AnalyzeTool:
    """Build the `material` async tool. Pure function of the supplied FEN
    (no live-board fallback, no engine): parses the position and reports
    per-color piece counts keyed by name. Kings are omitted."""
    async def material(input_: dict, *, cancel_token: CancelToken) -> dict:
        board, err = _parse_fen_arg(input_)
        if err is not None:
            return err

        def counts(color: chess.Color) -> dict:
            return {
                chess.PIECE_NAMES[pt]: len(board.pieces(pt, color))
                for pt in _MATERIAL_PIECE_TYPES
            }

        return {"white": counts(chess.WHITE), "black": counts(chess.BLACK)}

    return material


def make_validate_move_tool(board_provider: BoardProvider) -> AnalyzeTool:
    """Build the `validate_move` async tool. Tries UCI first, falls back
    to SAN. Illegal or malformed input returns a structured error so the
    model knows not to use the move it asked about."""
    async def validate_move(input_: dict, *, cancel_token: CancelToken) -> dict:
        board = board_provider()
        if board is None:
            return {"error": "no_live_position"}
        raw = input_.get("move")
        if not isinstance(raw, str) or not raw.strip():
            return {"error": "invalid_input", "detail": "move must be a non-empty string"}
        move, kind, detail = _parse_move_or_error(board, _strip_move_prefix(raw))
        if move is None:
            return {"error": kind, "detail": detail}
        return {"legal": True, "uci": move.uci(), "san": board.san(move)}

    return validate_move


_REPORT_LINE_CARD = (
    "Report a continuation here before you narrate it in prose -- the "
    "moves go through this tool, not invented in text. The line is "
    "replayed and confirmed legal in sequence; once accepted you may name "
    "its moves and the pieces they touch freely. Starts from the live "
    "position unless you pass `from_fen`."
)


REPORT_LINE_TOOL_SPEC = ToolSpec(
    name=REPORT_LINE_TOOL_NAME,
    description=(
        "Confirm a continuation (sequence of moves) is legal in order, so "
        "you can discuss it in prose. Replays the moves from the live "
        "position (or `from_fen`) and returns the rendered SAN and "
        "resulting FEN, or the ply where it breaks. Report a line before "
        "naming its moves in prose."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "moves": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "The line's moves in UCI or SAN, in order. Capped at "
                    f"{REPORT_LINE_MAX_PLIES} plies."
                    + _MOVE_NOTATION_CONSTRAINT
                ),
            },
            "from_fen": {
                "type": "string",
                "description": (
                    "Position the line starts from, or 'startpos'. Omit to "
                    "start from the live position."
                ),
            },
        },
        "required": ["moves"],
    },
    card=_REPORT_LINE_CARD,
)


def make_report_line_tool(board_provider: BoardProvider) -> AnalyzeTool:
    """Build the `report_line` async tool. Pure legality replay (no engine)
    of a model-supplied move sequence from the live board (or `from_fen`),
    validating each move in order. Returns the rendered SAN, per-ply FENs
    (`fens`, including the start), and the final FEN."""
    async def report_line(input_: dict, *, cancel_token: CancelToken) -> dict:
        from_fen = input_.get("from_fen")
        if from_fen is not None:
            board, err = _parse_fen_arg({"fen": from_fen})
            if err is not None:
                return err
        else:
            board = board_provider()
            if board is None:
                return {"error": "no_live_position"}
        board = board.copy(stack=False)

        raw_moves = input_.get("moves")
        if not isinstance(raw_moves, list) or not raw_moves:
            return {"error": "invalid_input", "detail": "moves must be a non-empty list of strings"}
        truncated = len(raw_moves) > REPORT_LINE_MAX_PLIES
        raw_moves = raw_moves[:REPORT_LINE_MAX_PLIES]

        san_line: list[str] = []
        fens: list[str] = [board.fen()]
        for ply, raw in enumerate(raw_moves):
            if not isinstance(raw, str):
                return {"error": "invalid_input", "detail": "move must be a string", "ply": ply}
            move, kind, detail = _parse_move_or_error(board, _strip_move_prefix(raw))
            if move is None:
                return {"error": kind, "detail": detail, "ply": ply, "move_input": raw}
            san_line.append(board.san(move))
            board.push(move)
            fens.append(board.fen())

        out: dict = {
            "ok": True,
            "san": " ".join(san_line),
            "fens": fens,
            "end_fen": board.fen(),
        }
        if truncated:
            out["truncated"] = True
        return out

    return report_line


# Declarative (see _DELEGATE_TOOL_CARD): the move goes via the tool, not
# prose, and a one-to-two sentence conclusion follows the accepted call.
_RECOMMEND_MOVE_CARD = (
    "The move goes through this tool, not in prose. It is checked against "
    "the strongest move at the requested depth; if a meaningfully stronger "
    "move exists, the result names it -- submit that move next, do not hunt "
    "for others. An illegal move comes back with `legal_moves` for that "
    "piece -- pick one of those, never guess another. Once the call is "
    "accepted, a one-to-two sentence conclusion follows it, naming the plan "
    "the move reflects."
)


RECOMMEND_MOVE_TOOL_SPEC = ToolSpec(
    name=RECOMMEND_MOVE_TOOL_NAME,
    description=(
        "Submit your final move at end-of-turn. Validates legality and "
        "checks it against the strongest move at the requested depth. "
        "Returns post-move FEN on acceptance, or "
        "error=recommendation_rejected with a `reason` naming the stronger "
        "move when one is meaningfully better."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "move": {
                "type": "string",
                "description": (
                    "Move in UCI (e.g. 'g1f3', 'e7e8q') or SAN (e.g. "
                    "'Nf3', 'O-O', 'exd5')."
                    + _MOVE_NOTATION_CONSTRAINT
                ),
            },
            "depth": {
                "type": "integer",
                "description": (
                    "Dominance-check depth. Go deeper when the position is "
                    "sharp or the move was close, so the check doesn't "
                    "confirm a shallow mistake."
                ),
            },
        },
        "required": ["move"],
    },
    card=_RECOMMEND_MOVE_CARD,
)


def _better_for_stm(
    candidate: chess.engine.PovScore | None,
    rival: chess.engine.PovScore | None,
    turn: chess.Color,
) -> bool:
    """True iff `rival` is strictly better than `candidate` from `turn`'s
    perspective by more than RECOMMEND_MARGIN_CP. Mate always trumps cp
    (margin doesn't apply); None falls back to conservative behavior."""
    if rival is None:
        return False
    if candidate is None:
        return True
    rival_score = rival.pov(turn)
    cand_score = candidate.pov(turn)
    # Mate trumps cp (and vice versa) regardless of margin.
    if rival_score.is_mate() or cand_score.is_mate():
        return rival_score > cand_score
    rival_cp = rival_score.score()
    cand_cp = cand_score.score()
    if rival_cp is None or cand_cp is None:
        return rival_score > cand_score
    return (rival_cp - cand_cp) > RECOMMEND_MARGIN_CP


def make_recommend_move_tool(
    engine_launcher: EngineLauncher,
    bus: EventBus,
    board_provider: BoardProvider,
    game_id_provider: GameIdProvider | None = None,
    settings_provider: SettingsProvider | None = None,
    search_cache: SearchCache | None = None,
) -> AnalyzeTool:
    """Build the `recommend_move` async tool. Parses UCI/SAN, then runs
    two engine searches (candidate-restricted + free) at the requested
    depth on the live position; if the engine's bestmove scores better
    for the side to move, returns a structured error so the model can
    pivot. On acceptance returns `{ok, uci, san, post_move_fen,
    candidate_score, engine_best_move, engine_best_score, depth}`.
    `search_cache`: see make_analyze_tool."""
    cache = search_cache or SearchCache()

    async def recommend_move(input_: dict, *, cancel_token: CancelToken) -> dict:
        board = board_provider()
        if board is None:
            return {"error": "no_live_position"}
        raw = input_.get("move")
        if not isinstance(raw, str) or not raw.strip():
            return {"error": "invalid_input", "detail": "move must be a non-empty string"}
        parsed, kind, detail = _parse_move_or_error(board, _strip_move_prefix(raw))
        if parsed is None:
            err: dict = {"error": kind, "detail": detail}
            # On an illegal move, hand back the legal moves for the piece the
            # model meant -- so it picks a real one instead of guessing again.
            if kind == "illegal_move":
                piece_type = _intended_piece_type(board, _strip_move_prefix(raw))
                if piece_type is not None:
                    err["legal_moves"] = _legal_moves_for_piece(board, piece_type)
            return err
        san = board.san(parsed)
        uci = parsed.uci()
        scratch = board.copy(stack=False)
        scratch.push(parsed)

        # Floor recommend_move's two searches at the verification depth so
        # the dominance check can't confirm a move at a shallow depth the
        # model picked; it may go deeper, never below the floor.
        max_depth = _max_depth(settings_provider)
        floor = min(_verification_depth(settings_provider), max_depth)
        raw_depth = input_.get("depth")
        try:
            depth = min(int(raw_depth), max_depth) if raw_depth is not None else _DEFAULT_DEPTH
        except (TypeError, ValueError):
            depth = _DEFAULT_DEPTH
        depth = max(depth, floor)
        limit = chess.engine.Limit(depth=depth)
        game_id = (game_id_provider() if game_id_provider else None) or _ANALYZE_GAME_ID_FALLBACK

        scratch_live = board.copy(stack=False)

        # Search A: engine's free best move on the live position.
        try:
            best_info, _ = await cache.get_or_search(
                engine_launcher, scratch_live, limit,
                bus=bus, game_id=game_id, cancel_token=cancel_token,
                settings_provider=settings_provider,
            )
        except _SearchError as err:
            return {"error": err.kind, "detail": err.detail}
        if cancel_token.cancelled:
            # Accept as-is; we couldn't finish verification.
            return {
                "ok": True, "uci": uci, "san": san,
                "post_move_fen": scratch.fen(), "cancelled": True,
            }

        # Search B: same board, restricted to the candidate.
        try:
            cand_info, _ = await cache.get_or_search(
                engine_launcher, board.copy(stack=False), limit,
                bus=bus, game_id=game_id, cancel_token=cancel_token,
                root_moves=[parsed],
                settings_provider=settings_provider,
            )
        except _SearchError as err:
            return {"error": err.kind, "detail": err.detail}
        if cancel_token.cancelled:
            return {
                "ok": True, "uci": uci, "san": san,
                "post_move_fen": scratch.fen(), "cancelled": True,
            }

        best_score = best_info.get("score")
        cand_score = cand_info.get("score")
        best_move = best_info.get("pv", [None])[0] if best_info.get("pv") else None

        result_common: dict = {
            "uci": uci,
            "san": san,
            "depth": depth,
            "candidate_score": _score_to_cp(cand_score),
            "engine_best_score": _score_to_cp(best_score),
        }
        if best_move is not None:
            result_common["engine_best_move"] = best_move.uci()
            result_common["engine_best_san"] = scratch_live.san(best_move)

        # Exact-move match short-circuits: search scores are mildly
        # non-deterministic, so don't reject a move the engine itself
        # just picked as best.
        if best_move is not None and best_move == parsed:
            return {"ok": True, "post_move_fen": scratch.fen(), **result_common}

        # A move that forces mate for the side to move is a won game; a
        # faster engine mate doesn't make it a mistake. Accept it (mate
        # against STM isn't winning, so it falls through to the check below).
        if cand_score is not None:
            cand_mate = cand_score.pov(board.turn).mate()
            if cand_mate is not None and cand_mate > 0:
                return {"ok": True, "post_move_fen": scratch.fen(), **result_common}

        if _better_for_stm(cand_score, best_score, board.turn):
            best_san = result_common.get("engine_best_san") or "a stronger move"
            return {
                "error": "recommendation_rejected",
                "reason": (
                    f"A stronger move is available: {best_san}. Submit "
                    f"{best_san} unless you can show it is worse."
                ),
                **result_common,
            }

        return {"ok": True, "post_move_fen": scratch.fen(), **result_common}

    return recommend_move


def make_recommend_verifier(
    engine_launcher: EngineLauncher,
    bus: EventBus,
    board_provider: BoardProvider,
    game_id_provider: GameIdProvider | None = None,
    settings_provider: SettingsProvider | None = None,
    search_cache: SearchCache | None = None,
):
    """Build the end-of-turn recommend-verifier. Returns a callable that
    runs a searchmoves-restricted deep search on the recommended move
    and returns a payload dict (or None on failure). The coordinator
    emits the `ai_recommendation` event through its own _emit so the
    payload gets a seq stamp and lands in the replay buffer.
    `search_cache`: see make_analyze_tool."""
    cache = search_cache or SearchCache()

    async def verify(
        move: chess.Move, depth: int | None, cancel_token: CancelToken,
    ) -> dict | None:
        board = board_provider()
        if board is None or move not in board.legal_moves:
            return None
        game_id = (game_id_provider() if game_id_provider else None) or _ANALYZE_GAME_ID_FALLBACK
        # Verify at least the verification floor deep, going deeper if the
        # model asked for more -- never shallower. Clamped to the cap.
        requested = int(depth) if depth else 0
        floor = _verification_depth(settings_provider)
        d = min(max(requested, floor), _max_depth(settings_provider))
        limit = chess.engine.Limit(depth=d)
        try:
            last_info, _cancelled = await cache.get_or_search(
                engine_launcher, board.copy(stack=False), limit,
                bus=bus, game_id=game_id, cancel_token=cancel_token,
                root_moves=[move],
                settings_provider=settings_provider,
            )
        except _SearchError:
            return None
        payload: dict = {
            "uci": move.uci(),
            "san": board.san(move),
        }
        payload.update(_score_to_cp(last_info.get("score")))
        depth = last_info.get("depth")
        if depth is not None:
            payload["depth"] = depth
        pv = _pv_to_uci(board, last_info.get("pv"))
        if pv:
            payload["pv"] = pv
            payload["pv_uci"] = pv
        return payload

    return verify
