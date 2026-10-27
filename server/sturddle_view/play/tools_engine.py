"""Engine-backed tools for the AI analysis agent.

`analyze` is the first real tool: spawns a throwaway UCI engine,
configures a depth-only search limit, drains info events until the
engine signals bestmove (or the cancel token flips), and returns a
structured eval record. Searches are depth-only by design -- a time
limit makes the bestmove non-deterministic on near-equal candidates.

Per spec sec. Architecture: one throwaway engine per analyze call (pool
later if perf demands). Hard caps live as named consts + SV_ env vars
(no magic numbers); out-of-range requests are clamped, not rejected,
so the agent never stalls on a guardrail.
"""
from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Any, Awaitable, Callable

import chess
import chess.engine

from ..chess.engine_info import serialize_info
from ..chess.results import SIDE_BLACK, SIDE_WHITE
from ..chess.score import SCORE_MATE
from ..config import (
    AI_ANALYZE_MAX_DEPTH_KEY,
    AI_VERIFICATION_DEPTH_KEY,
    ANALYSIS_MULTIPV_KEY,
    _DEFAULT_AI_ANALYZE_MAX_DEPTH,
    _DEFAULT_AI_VERIFICATION_DEPTH,
    _DEFAULT_ANALYSIS_MULTIPV,
)
from ..engines import UCI_OPT_MULTIPV
from ..env_utils import env_int
from ..error_detail import REASON_KEY
from ..events import EVT_ENGINE_SEARCH_START, Event, EventBus
from ..llm import ToolSpec
from ..llm.cancel import CancelToken
from ..llm.tool_progress import report_progress
from ..llm.tools import (
    INVALID_INPUT_ERROR,
    NO_LIVE_POSITION_ERROR,
    integer_prop,
    object_schema,
    string_list_prop,
    string_prop,
    tool_error,
)
from .engine_analysis import (
    log_spawn_failure,
    resolve_eval_pov_white_or_stm,
    spawn_analysis_engine,
)
from .engine_info_pump import is_interesting_info, publish_engine_info, pump_engine_info
from .engine_supervisor import EngineSupervisor
from .playbook import AHEAD_MARGINS, BEHIND_MARGINS, Situation
from .tactics import all_forks, all_pins, piece_label


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

# recommend_move dominance margin: the engine's best must beat the
# candidate by strictly more than this many cp (STM POV) to reject. Plan-
# aware: tight when the side to move is ahead (convert cleanly), wide when
# behind (a practical try may score below the top line), the midpoint when
# even or unclassified. Mate scores ignore it.
_DEFAULT_RECOMMEND_MARGIN_MIN_CP = 20
_DEFAULT_RECOMMEND_MARGIN_MAX_CP = 80
RECOMMEND_MARGIN_MIN_CP = env_int("SV_AI_RECOMMEND_MARGIN_MIN", _DEFAULT_RECOMMEND_MARGIN_MIN_CP)
RECOMMEND_MARGIN_MAX_CP = env_int("SV_AI_RECOMMEND_MARGIN_MAX", _DEFAULT_RECOMMEND_MARGIN_MAX_CP)

# recommend_move rejection reason when the side ahead offers a repetition.
_REPEAT_AHEAD_REASON = (
    "{san} repeats the position; ahead, a repetition only offers the "
    "draw. Submit a move that makes progress."
)

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

# MultiPV: analyze's optional `lines` arg and top_moves' one-search batch
# only make sense from two lines up; fewer is a plain best-line search.
_MIN_LINES = 2

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
# The analysis engine's probed MultiPV max (Engine.multipv_max semantics).
MultipvMaxProvider = Callable[[], int | None]
# The current AI turn's book move (UCI), or None when the turn has none.
BookMoveProvider = Callable[[], str | None]
# Live-position Situation (play/playbook.py) for the plan-aware gate.
SituationProvider = Callable[[], Situation | None]
AnalyzeTool = Callable[..., Awaitable[dict[str, Any]]]
# Delegate's refutation check: True when the engine confirms the move is
# dominated (recommend_move would reject it), False when it isn't, None
# when the search failed or was cancelled.
RefuteCheck = Callable[[chess.Move, CancelToken], Awaitable[bool | None]]


def _settings(settings_provider: SettingsProvider | None):
    return settings_provider() if settings_provider else None


def _search_pov(settings_provider: SettingsProvider | None, board: chess.Board) -> chess.Color:
    """Eval POV a tool search's engine_info is serialized in (live or reused)."""
    return resolve_eval_pov_white_or_stm(_settings(settings_provider), board.turn)


def _settings_int(
    settings_provider: SettingsProvider | None, attr: str, fallback: int,
) -> int:
    """Read an int setting via the provider, falling back to the module
    const when no provider/setting is wired. One source for the
    settings-or-default depth-cap lookup the tools share."""
    settings = _settings(settings_provider)
    value = getattr(settings, attr, None) if settings is not None else None
    return int(value) if isinstance(value, int) else fallback


def _max_depth(settings_provider: SettingsProvider | None) -> int:
    return _settings_int(settings_provider, AI_ANALYZE_MAX_DEPTH_KEY, MAX_DEPTH)


def _verification_depth(settings_provider: SettingsProvider | None) -> int:
    return _settings_int(settings_provider, AI_VERIFICATION_DEPTH_KEY, VERIFICATION_DEPTH)


def _multipv_max(multipv_max_provider: MultipvMaxProvider | None) -> int:
    return (multipv_max_provider() if multipv_max_provider else None) or 0


def analyze_lines_cap(
    settings_provider: SettingsProvider | None,
    multipv_max_provider: MultipvMaxProvider | None,
) -> int:
    """Most lines `analyze` may return: the Lines setting capped by the
    analysis engine's MultiPV max. Below _MIN_LINES the arg isn't offered."""
    lines = _settings_int(settings_provider, ANALYSIS_MULTIPV_KEY, _DEFAULT_ANALYSIS_MULTIPV)
    return min(lines, _multipv_max(multipv_max_provider))

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
_MOVE_ARG_DESCRIPTION = (
    "Move in UCI (e.g. 'g1f3', 'e7e8q') or SAN (e.g. "
    "'Nf3', 'O-O', 'exd5')."
    + _MOVE_NOTATION_CONSTRAINT
)

# Tool names -- single source of truth (the ToolSpecs below use them, and
# the coordinator imports them rather than hardcoding string literals).
ANALYZE_TOOL_NAME = "analyze"
MATERIAL_TOOL_NAME = "material"
TACTICS_TOOL_NAME = "tactics"
TOP_MOVES_TOOL_NAME = "top_moves"
PIECE_AT_TOOL_NAME = "piece_at"
VALIDATE_MOVE_TOOL_NAME = "validate_move"
RECOMMEND_MOVE_TOOL_NAME = "recommend_move"
REPORT_LINE_TOOL_NAME = "report_line"

# Progress steps of the dominance check (see tool_progress): the baseline
# search of the engine's own best, then the candidate's. Panel labels key on
# these names.
PROGRESS_ENGINE_BEST = "engine_best"
PROGRESS_SEARCH_MOVE = "search_move"

# Shared description for the `fen` arg across every FEN-taking tool spec
# (analyze, material). One source so the startpos affordance stays in sync.
_FEN_ARG_DESCRIPTION = "FEN string, or 'startpos' for the initial position."

# Tool argument and result keys. depth / pv / nodes / score double as
# python-chess InfoDict keys; the results mirror the engine's names.
# fen / move are public: `delegate` (ai_analysis) shares them.
FEN_KEY = "fen"
_FROM_FEN_KEY = "from_fen"
MOVE_KEY = "move"
_MOVES_KEY = "moves"
_SQUARE_KEY = "square"
_DEPTH_KEY = "depth"
_PV_KEY = "pv"
_LINES_KEY = "lines"
_NODES_KEY = "nodes"
_SCORE_KEY = "score"
_UCI_KEY = "uci"
_SAN_KEY = "san"
_OK_KEY = "ok"
_POST_MOVE_FEN_KEY = "post_move_fen"
_CANCELLED_KEY = "cancelled"
_TRUNCATED_KEY = "truncated"
_LIMITS_USED_KEY = "limits_used"
_MOVE_INPUT_KEY = "move_input"
_PIECE_KEY = "piece"
_PLY_KEY = "ply"
_BY_KEY = "by"
_SCORE_TEXT_KEY = "score_text"
_ENGINE_BEST_SAN_KEY = "engine_best_san"
# top_moves ranks on this, then pops it before returning.
_SORT_KEY = "_sort_key"
# top_moves result keys; public so the verifier gate can read the ranking.
CANDIDATES_KEY = "candidates"
MOVE_UCI_KEY = "move_uci"

# Error codes and details repeated across the tools. The move-parse codes
# and the empty-move detail are public: `delegate` (ai_analysis) shares them.
_INVALID_SQUARE = "invalid_square"
_ILLEGAL_MOVE = "illegal_move"
INVALID_MOVE_ERROR = "invalid_move"
_RECOMMENDATION_REJECTED = "recommendation_rejected"
_MOVE_NOT_STRING = "move must be a string"
MOVE_EMPTY_DETAIL = "move must be a non-empty string"
_MOVES_EMPTY = "moves must be a non-empty list of strings"


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
    input_schema=object_schema(
        {
            _MOVES_KEY: string_list_prop(
                "Candidate moves in UCI or SAN. Capped at "
                f"{TOP_MOVES_MAX_N}; extras dropped."
                + _MOVE_NOTATION_CONSTRAINT
            ),
            _DEPTH_KEY: integer_prop(
                "Per-candidate depth. Go deeper when candidates score "
                "close -- shallow ranking is unreliable."
            ),
        },
        required=[_MOVES_KEY],
    ),
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
    input_schema=object_schema(
        {
            _SQUARE_KEY: string_prop(
                "Algebraic square name, e.g. 'e4', 'a1', 'h8'. "
                "Case-insensitive."
            ),
            FEN_KEY: string_prop(
                "Position to read, or 'startpos'. Omit to use the live "
                "position."
            ),
        },
        required=[_SQUARE_KEY],
    ),
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
    input_schema=object_schema(
        {MOVE_KEY: string_prop(_MOVE_ARG_DESCRIPTION)},
        required=[MOVE_KEY],
    ),
    card=_VALIDATE_MOVE_CARD,
)


ANALYZE_TOOL_SPEC = ToolSpec(
    name=ANALYZE_TOOL_NAME,
    description=(
        "Deep search of a position. Returns white-POV eval: score_cp, "
        "score_text, mate (signed plies when forced), depth, pv, bestmove. "
        "Use the numbers internally; eval discipline still applies."
    ),
    input_schema=object_schema(
        {
            FEN_KEY: string_prop(_FEN_ARG_DESCRIPTION),
            _DEPTH_KEY: integer_prop(
                "Search depth. Go deeper on sharp or close positions -- "
                "a shallow search misjudges tactics."
            ),
        },
        required=[FEN_KEY],
    ),
)


def analyze_tool_spec(
    settings_provider: SettingsProvider | None,
    multipv_max_provider: MultipvMaxProvider | None,
) -> ToolSpec:
    """ANALYZE_TOOL_SPEC plus the `lines` arg, offered only while the Lines
    setting and the analysis engine's MultiPV allow two or more."""
    def extra() -> dict[str, Any]:
        cap = analyze_lines_cap(settings_provider, multipv_max_provider)
        if cap < _MIN_LINES:
            return {}
        return {_LINES_KEY: integer_prop(
            f"Return the best {_MIN_LINES}-{cap} lines, ranked, under `lines`. "
            "Ask when comparing alternatives or at a critical moment (only "
            "move vs several good options); omit for just the best line."
        )}
    return replace(ANALYZE_TOOL_SPEC, extra_properties=extra)


# material and tactics take just a FEN.
_FEN_ONLY_SCHEMA = object_schema(
    {FEN_KEY: string_prop(_FEN_ARG_DESCRIPTION)}, required=[FEN_KEY],
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
    input_schema=_FEN_ONLY_SCHEMA,
)


TACTICS_TOOL_SPEC = ToolSpec(
    name=TACTICS_TOOL_NAME,
    description=(
        "Pins and forks actually on the board, both colors. Pins are to "
        "the king or queen only. Call before writing 'pin', 'pinned', "
        "'fork' or 'forks' about a position; an empty list means there is "
        "none to name. Pass a FEN."
    ),
    input_schema=_FEN_ONLY_SCHEMA,
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
    fen = input_.get(FEN_KEY)
    if not isinstance(fen, str) or not fen.strip():
        return None, tool_error("missing_fen")
    try:
        return _parse_fen(fen.strip()), None
    except ValueError as exc:
        return None, tool_error("invalid_fen", str(exc))


def _depth_limit(
    input_: dict, default_depth: int, max_depth: int,
) -> tuple[chess.engine.Limit, dict]:
    """Build a depth-only chess.engine.Limit from the agent's requested
    depth (clamped to `max_depth`), falling back to `default_depth`. Returns
    the Limit and a dict echoing the effective depth (tool output + test
    assertions). Time limits are intentionally not supported: a timer that
    fires before the depth is reached makes the bestmove non-deterministic,
    which flips the pick on near-equal candidates."""
    raw_depth = input_.get(_DEPTH_KEY)
    if raw_depth is not None:
        try:
            depth = max(min(MIN_DEPTH, max_depth), min(int(raw_depth), max_depth))
        except (TypeError, ValueError):
            depth = default_depth
    else:
        depth = default_depth
    return chess.engine.Limit(depth=depth), {_DEPTH_KEY: depth}


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
        out[_SCORE_TEXT_KEY] = f"{cp / 100.0:+.2f}"
    mate = s.mate()
    if mate is not None:
        out[SCORE_MATE] = mate
        # Mate beats cp for the human-readable string. python-chess uses
        # signed plies, but coaching prose works better in moves: "mate
        # in 3" reads cleaner than "mate in 6 plies".
        sign = "+" if mate > 0 else "-"
        moves = (abs(mate) + 1) // 2
        out[_SCORE_TEXT_KEY] = f"{sign}M{moves}"
    return out


def _requested_lines(input_: dict, cap: int) -> int:
    """analyze's `lines` arg clamped to [_MIN_LINES, cap]; 0 = best line
    only (absent, unusable, or lines unavailable)."""
    raw = input_.get(_LINES_KEY)
    if raw is None or cap < _MIN_LINES:
        return 0
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 0
    return min(n, cap) if n >= _MIN_LINES else 0


def _eval_fields(board: chess.Board, info: chess.engine.InfoDict) -> dict:
    """A searched line's eval, depth and pv (shared by analyze and top_moves)."""
    out = _score_to_cp(info.get(_SCORE_KEY))
    if info.get(_DEPTH_KEY) is not None:
        out[_DEPTH_KEY] = info[_DEPTH_KEY]
    pv = _pv_to_uci(board, info.get(_PV_KEY))
    if pv:
        out[_PV_KEY] = pv
    return out


def _line_fields(board: chess.Board, info: chess.engine.InfoDict) -> dict:
    """analyze's per-line result: _eval_fields plus nodes and the pv's first move."""
    out = _eval_fields(board, info)
    if info.get(_NODES_KEY) is not None:
        out[_NODES_KEY] = info[_NODES_KEY]
    if _PV_KEY in out:
        out["bestmove"] = out[_PV_KEY][0]
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
        # MultiPV searches: the search key plus the line count -> ranked lines.
        self._lines: dict[tuple[_SearchKey, int], tuple[int, list[chess.engine.InfoDict]]] = {}

    def clear(self) -> None:
        self._entries = {}
        self._lines = {}

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
        return await self._get_or_run(
            self._entries, self._key(board, root_moves), limit,
            lambda: _run_one_search(
                engine_launcher, board, limit,
                bus=bus, game_id=game_id, cancel_token=cancel_token,
                root_moves=root_moves, settings_provider=settings_provider,
            ),
            lambda info: [info],
            board=board, bus=bus, game_id=game_id, settings_provider=settings_provider,
        )

    async def get_or_search_lines(
        self,
        engine_launcher: EngineLauncher,
        board: chess.Board,
        limit: chess.engine.Limit,
        multipv: int,
        *,
        bus: EventBus,
        game_id: str,
        cancel_token: CancelToken,
        root_moves: list[chess.Move] | None = None,
        settings_provider: SettingsProvider | None = None,
    ) -> tuple[list[chess.engine.InfoDict], bool]:
        """get_or_search for a `multipv`-line search; returns the ranked
        lines, best first (see _run_lines_search)."""
        return await self._get_or_run(
            self._lines, (self._key(board, root_moves), multipv), limit,
            lambda: _run_lines_search(
                engine_launcher, board, limit, multipv,
                bus=bus, game_id=game_id, cancel_token=cancel_token,
                root_moves=root_moves, settings_provider=settings_provider,
            ),
            lambda lines: lines,
            board=board, bus=bus, game_id=game_id, settings_provider=settings_provider,
        )

    @staticmethod
    async def _get_or_run(
        store: dict, key, limit: chess.engine.Limit,
        run: Callable[[], Awaitable[tuple[Any, bool]]],
        infos: Callable[[Any], list[chess.engine.InfoDict]],
        *, board: chess.Board, bus: EventBus, game_id: str,
        settings_provider: SettingsProvider | None,
    ) -> tuple[Any, bool]:
        """Shared hit/miss: `run()` searches; `infos(result)` lists its
        InfoDicts, best line first, whose depth is the result's."""
        requested = limit.depth or 0
        cached = store.get(key)
        if cached is not None and cached[0] >= requested:
            log.info("search cache hit: depth %d >= %d, key=%s", cached[0], requested, key)
            # Show the reused result like a live search's final chunk, so the
            # arrow and Search Lines follow the move now under consideration.
            pov = _search_pov(settings_provider, board)
            for info in infos(cached[1]):
                if is_interesting_info(info):
                    payload = serialize_info(info, board=board, pov=pov)
                    await publish_engine_info(payload, bus=bus, game_id=game_id)
            return cached[1], False
        result, cancelled = await run()
        if not cancelled:
            reached = infos(result)[0].get(_DEPTH_KEY) or 0
            if cached is None or reached > cached[0]:
                store[key] = (reached, result)
        return result, cancelled


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
    """One best-line search: (last_info, cancelled). See _run_search."""
    last_info, _lines, cancelled = await _run_search(
        engine_launcher, board, limit,
        bus=bus, game_id=game_id, cancel_token=cancel_token,
        root_moves=root_moves, settings_provider=settings_provider,
    )
    return last_info, cancelled


async def _run_lines_search(
    engine_launcher: EngineLauncher,
    board: chess.Board,
    limit: chess.engine.Limit,
    multipv: int,
    *,
    bus: EventBus,
    game_id: str,
    cancel_token: CancelToken,
    root_moves: list[chess.Move] | None = None,
    settings_provider: SettingsProvider | None = None,
) -> tuple[list[chess.engine.InfoDict], bool]:
    """One `multipv`-line search: (ranked lines, cancelled). Each line is
    python-chess's per-rank aggregate, best first. See _run_search."""
    _last_info, lines, cancelled = await _run_search(
        engine_launcher, board, limit,
        bus=bus, game_id=game_id, cancel_token=cancel_token,
        root_moves=root_moves, multipv=multipv, settings_provider=settings_provider,
    )
    return lines, cancelled


async def _run_search(
    engine_launcher: EngineLauncher,
    board: chess.Board,
    limit: chess.engine.Limit,
    *,
    bus: EventBus,
    game_id: str,
    cancel_token: CancelToken,
    root_moves: list[chess.Move] | None = None,
    multipv: int | None = None,
    settings_provider: SettingsProvider | None = None,
) -> tuple[chess.engine.InfoDict, list[chess.engine.InfoDict], bool]:
    """Spawn a throwaway engine, run one search, return (last_info, lines,
    cancelled). Raises _SearchError on spawn/mid-search engine death ->
    error envelope.

    `root_moves`: restricts the engine to these moves at the root (UCI
    `searchmoves`); top_moves uses it to score one candidate without push/pop.
    `multipv`: lines to search; `lines` holds them ranked (one when None).
    `settings_provider`: live settings so the engine inherits Threads/Hash/
    SyzygyPath; None falls back to no overrides (test doubles).

    Emits engine_info but NOT engine_search_start -- the caller clears the
    panel (analyze once; top_moves once for the whole batch)."""
    settings = _settings(settings_provider)
    try:
        # Build inside the try: the launcher (make_analysis_supervisor) can
        # raise NoAnalysisEngine before spawn -- it must come out as a clean
        # _SearchError envelope, not escape the tool uncaught.
        sup = engine_launcher()
        engine, cleanup = await spawn_analysis_engine(sup, settings)
    except Exception as exc:
        log_spawn_failure(exc, "search")
        raise _SearchError("engine_spawn_failed", str(exc)) from exc
    try:
        analysis_kwargs: dict = {"limit": limit}
        if root_moves:
            analysis_kwargs["root_moves"] = root_moves
        # The registry's MultiPV support can be stale (binary swapped since
        # the probe): without the option, search the best line only.
        if multipv and UCI_OPT_MULTIPV in engine.options:
            analysis_kwargs["multipv"] = multipv
        with await engine.analysis(board, **analysis_kwargs) as analysis:
            last_info, cancelled = await pump_engine_info(
                analysis,
                bus=bus,
                game_id=game_id,
                board=board,
                pov=_search_pov(settings_provider, board),
                cancel_token=cancel_token,
            )
            lines = [dict(info) for info in analysis.multipv]
        return last_info, lines, cancelled
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
    multipv_max_provider: MultipvMaxProvider | None = None,
) -> AnalyzeTool:
    """Build the `analyze` async tool.

    `search_cache`: cross-tool search cache; defaults to a fresh always-miss
    one. `engine_launcher()`: fresh EngineSupervisor per call (resolution
    lives in app.py). `bus`: HVE's engine_info bus -- publishing there lights
    up the PV window and board arrow during a tool-call search.
    `game_id_provider()`: live game id (fallback id when None, so events still
    mux). `settings_provider()`: settings for Threads/Hash/SyzygyPath; None ok
    (tests). `multipv_max_provider()`: with the Lines setting, caps the
    optional `lines` arg (see analyze_tool_spec)."""
    cache = search_cache or SearchCache()

    async def analyze(input_: dict, *, cancel_token: CancelToken) -> dict:
        board, err = _parse_fen_arg(input_)
        if err is not None:
            return err

        limit, limits_used = _depth_limit(
            input_, _DEFAULT_DEPTH, _max_depth(settings_provider)
        )

        game_id = (game_id_provider() if game_id_provider else None) or _ANALYZE_GAME_ID_FALLBACK
        await _publish_search_start(bus, game_id)

        lines_wanted = _requested_lines(
            input_, analyze_lines_cap(settings_provider, multipv_max_provider),
        )
        search_kw = dict(
            bus=bus, game_id=game_id, cancel_token=cancel_token,
            settings_provider=settings_provider,
        )
        lines: list[chess.engine.InfoDict] = []
        try:
            if lines_wanted:
                lines, cancelled = await cache.get_or_search_lines(
                    engine_launcher, board, limit, lines_wanted, **search_kw,
                )
                best_info = lines[0]
            else:
                best_info, cancelled = await cache.get_or_search(
                    engine_launcher, board, limit, **search_kw,
                )
        except _SearchError as err:
            return tool_error(err.kind, err.detail)

        out: dict = {_LIMITS_USED_KEY: limits_used}
        if cancelled:
            out[_CANCELLED_KEY] = True
        out.update(_line_fields(board, best_info))
        if lines:
            out[_LINES_KEY] = [_line_fields(board, info) for info in lines]
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
        return None, INVALID_INPUT_ERROR, "empty move string"
    for parse in (board.parse_uci, board.parse_san):
        try:
            return parse(candidate), None, None
        except chess.IllegalMoveError as exc:
            return None, _ILLEGAL_MOVE, str(exc)
        except (chess.InvalidMoveError, chess.AmbiguousMoveError):
            continue
    return None, INVALID_MOVE_ERROR, f"could not parse {candidate!r} as UCI or SAN"


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
    return None, {_MOVE_INPUT_KEY: raw, **tool_error(kind, detail)}


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


async def _publish_search_start(bus: EventBus, game_id: str) -> None:
    """Fresh search: the client clears the engine panel and its arrows."""
    await bus.publish(Event(kind=EVT_ENGINE_SEARCH_START, game_id=game_id, payload={}))


def _candidate_entry(board: chess.Board, move: chess.Move, info: chess.engine.InfoDict) -> dict:
    """top_moves' result for one candidate, plus the private sort key."""
    return {
        MOVE_UCI_KEY: move.uci(), "move_san": board.san(move),
        **_eval_fields(board, info),
        _SORT_KEY: _stm_pov_sort_key(info.get(_SCORE_KEY), board.turn),
    }


def make_top_moves_tool(
    engine_launcher: EngineLauncher,
    bus: EventBus,
    board_provider: BoardProvider,
    game_id_provider: GameIdProvider | None = None,
    settings_provider: SettingsProvider | None = None,
    search_cache: SearchCache | None = None,
    multipv_max_provider: MultipvMaxProvider | None = None,
) -> AnalyzeTool:
    """Deep-evaluate a model-supplied list of candidate moves. A MultiPV
    engine scores them in one root-restricted search; any candidate it
    leaves out (or every one, without MultiPV) gets its own. Illegal or
    unparseable moves come back as per-entry errors. Publishes one
    `engine_search_start` for the batch, then engine_info events.
    `search_cache`: see make_analyze_tool."""
    cache = search_cache or SearchCache()

    async def top_moves(input_: dict, *, cancel_token: CancelToken) -> dict:
        board = board_provider()
        if board is None:
            return tool_error(NO_LIVE_POSITION_ERROR)
        board = board.copy(stack=False)

        raw_moves = input_.get(_MOVES_KEY)
        if not isinstance(raw_moves, list) or not raw_moves:
            return tool_error(INVALID_INPUT_ERROR, _MOVES_EMPTY)
        truncated = len(raw_moves) > TOP_MOVES_MAX_N
        raw_moves = raw_moves[:TOP_MOVES_MAX_N]

        parsed: list[chess.Move] = []
        errors: list[dict] = []
        for raw in raw_moves:
            if not isinstance(raw, str):
                errors.append({
                    _MOVE_INPUT_KEY: raw, **tool_error(INVALID_INPUT_ERROR, _MOVE_NOT_STRING),
                })
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
        await _publish_search_start(bus, game_id)

        stm_is_white = board.turn == chess.WHITE
        search_kw = dict(
            bus=bus, game_id=game_id, cancel_token=cancel_token,
            settings_provider=settings_provider,
        )
        cancelled_any = False
        batched: dict[chess.Move, chess.engine.InfoDict] = {}
        n_lines = min(len(parsed), _multipv_max(multipv_max_provider))
        if n_lines >= _MIN_LINES:
            try:
                lines, cancelled_any = await cache.get_or_search_lines(
                    engine_launcher, board, limit, n_lines, root_moves=parsed, **search_kw,
                )
            except _SearchError as err:
                return tool_error(err.kind, err.detail)
            batched = {info[_PV_KEY][0]: info for info in lines if info.get(_PV_KEY)}
        # Kept even when the batch was cancelled, like per-move results.
        entries = [_candidate_entry(board, m, batched[m]) for m in parsed if m in batched]
        fresh_panel = bool(batched)
        for move in [m for m in parsed if m not in batched]:
            if cancelled_any or cancel_token.cancelled:
                cancelled_any = True
                break
            if fresh_panel:
                # Per-move searches after a batch start a fresh panel, so a
                # candidate it left out never shows as its numbered line 1.
                await _publish_search_start(bus, game_id)
                fresh_panel = False
            try:
                info, cancelled = await cache.get_or_search(
                    engine_launcher, board, limit, root_moves=[move], **search_kw,
                )
            except _SearchError as err:
                return tool_error(err.kind, err.detail)
            entries.append(_candidate_entry(board, move, info))
            if cancelled:
                cancelled_any = True
                break

        # Scoreless candidates (no engine score) sort to the bottom in
        # both directions; only the scored ones go through the POV sort.
        scored = [c for c in entries if c[_SORT_KEY] is not None]
        scoreless = [c for c in entries if c[_SORT_KEY] is None]
        scored.sort(key=lambda c: c[_SORT_KEY], reverse=True)
        entries = scored + scoreless
        for c in entries:
            c.pop(_SORT_KEY, None)

        out: dict = {
            "side_to_move": SIDE_WHITE if stm_is_white else SIDE_BLACK,
            _LIMITS_USED_KEY: limits_used,
            CANDIDATES_KEY: entries,
        }
        if errors:
            out["errors"] = errors
        if truncated:
            out[_TRUNCATED_KEY] = True
        if cancelled_any:
            out[_CANCELLED_KEY] = True
        return out

    return top_moves


def make_piece_at_tool(board_provider: BoardProvider) -> AnalyzeTool:
    """Build the `piece_at` async tool. Reports what occupies a square --
    the model's self-check against hallucinated piece placements. Reads the
    live board (via board_provider) by default, or a supplied `fen` so the
    model can ground a square in any position it is reasoning about."""
    async def piece_at(input_: dict, *, cancel_token: CancelToken) -> dict:
        fen = input_.get(FEN_KEY)
        if fen is not None:
            board, err = _parse_fen_arg(input_)
            if err is not None:
                return err
        else:
            board = board_provider()
            if board is None:
                return tool_error(NO_LIVE_POSITION_ERROR)
        raw = input_.get(_SQUARE_KEY)
        if not isinstance(raw, str) or not raw:
            return tool_error(_INVALID_SQUARE, "square must be a non-empty string")
        try:
            sq = chess.parse_square(raw.lower())
        except ValueError as exc:
            return tool_error(_INVALID_SQUARE, str(exc))
        piece = board.piece_at(sq)
        out: dict = {_SQUARE_KEY: chess.square_name(sq)}
        if piece is None:
            out[_PIECE_KEY] = None
        else:
            out[_PIECE_KEY] = {
                "type": chess.PIECE_NAMES[piece.piece_type],
                "color": SIDE_WHITE if piece.color == chess.WHITE else SIDE_BLACK,
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

        return {SIDE_WHITE: counts(chess.WHITE), SIDE_BLACK: counts(chess.BLACK)}

    return material


def make_tactics_tool() -> AnalyzeTool:
    """Build the `tactics` async tool. Pure function of the supplied FEN
    (no engine): lists every pin (king/queen shield) and fork on the
    board, each entry naming its pieces by color and square."""
    async def tactics(input_: dict, *, cancel_token: CancelToken) -> dict:
        board, err = _parse_fen_arg(input_)
        if err is not None:
            return err
        return {
            "pins": [
                {
                    "pinned": piece_label(board, pin.pinned),
                    _BY_KEY: piece_label(board, pin.attacker),
                    "to": piece_label(board, pin.shield),
                }
                for pin in all_pins(board)
            ],
            "forks": [
                {
                    _BY_KEY: piece_label(board, fork.attacker),
                    "targets": [piece_label(board, sq) for sq in fork.targets],
                }
                for fork in all_forks(board)
            ],
        }

    return tactics


def make_validate_move_tool(board_provider: BoardProvider) -> AnalyzeTool:
    """Build the `validate_move` async tool. Tries UCI first, falls back
    to SAN. Illegal or malformed input returns a structured error so the
    model knows not to use the move it asked about."""
    async def validate_move(input_: dict, *, cancel_token: CancelToken) -> dict:
        board = board_provider()
        if board is None:
            return tool_error(NO_LIVE_POSITION_ERROR)
        raw = input_.get(MOVE_KEY)
        if not isinstance(raw, str) or not raw.strip():
            return tool_error(INVALID_INPUT_ERROR, MOVE_EMPTY_DETAIL)
        move, kind, detail = _parse_move_or_error(board, _strip_move_prefix(raw))
        if move is None:
            return tool_error(kind, detail)
        return {"legal": True, _UCI_KEY: move.uci(), _SAN_KEY: board.san(move)}

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
    input_schema=object_schema(
        {
            _MOVES_KEY: string_list_prop(
                "The line's moves in UCI or SAN, in order. Capped at "
                f"{REPORT_LINE_MAX_PLIES} plies."
                + _MOVE_NOTATION_CONSTRAINT
            ),
            _FROM_FEN_KEY: string_prop(
                "Position the line starts from, or 'startpos'. Omit to "
                "start from the live position."
            ),
        },
        required=[_MOVES_KEY],
    ),
    card=_REPORT_LINE_CARD,
)


def make_report_line_tool(board_provider: BoardProvider) -> AnalyzeTool:
    """Build the `report_line` async tool. Pure legality replay (no engine)
    of a model-supplied move sequence from the live board (or `from_fen`),
    validating each move in order. Returns the rendered SAN, per-ply FENs
    (`fens`, including the start), and the final FEN."""
    async def report_line(input_: dict, *, cancel_token: CancelToken) -> dict:
        from_fen = input_.get(_FROM_FEN_KEY)
        if from_fen is not None:
            board, err = _parse_fen_arg({FEN_KEY: from_fen})
            if err is not None:
                return err
        else:
            board = board_provider()
            if board is None:
                return tool_error(NO_LIVE_POSITION_ERROR)
        board = board.copy(stack=False)

        raw_moves = input_.get(_MOVES_KEY)
        if not isinstance(raw_moves, list) or not raw_moves:
            return tool_error(INVALID_INPUT_ERROR, _MOVES_EMPTY)
        truncated = len(raw_moves) > REPORT_LINE_MAX_PLIES
        raw_moves = raw_moves[:REPORT_LINE_MAX_PLIES]

        san_line: list[str] = []
        fens: list[str] = [board.fen()]
        for ply, raw in enumerate(raw_moves):
            if not isinstance(raw, str):
                return {**tool_error(INVALID_INPUT_ERROR, _MOVE_NOT_STRING), _PLY_KEY: ply}
            move, kind, detail = _parse_move_or_error(board, _strip_move_prefix(raw))
            if move is None:
                return {**tool_error(kind, detail), _PLY_KEY: ply, _MOVE_INPUT_KEY: raw}
            san_line.append(board.san(move))
            board.push(move)
            fens.append(board.fen())

        out: dict = {
            _OK_KEY: True,
            _SAN_KEY: " ".join(san_line),
            "fens": fens,
            "end_fen": board.fen(),
        }
        if truncated:
            out[_TRUNCATED_KEY] = True
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
    input_schema=object_schema(
        {
            MOVE_KEY: string_prop(_MOVE_ARG_DESCRIPTION),
            _DEPTH_KEY: integer_prop(
                "Dominance-check depth. Go deeper when the position is "
                "sharp or the move was close, so the check doesn't "
                "confirm a shallow mistake."
            ),
        },
        required=[MOVE_KEY],
    ),
    card=_RECOMMEND_MOVE_CARD,
)


def recommend_margin_cp(situation: Situation | None) -> int:
    """Dominance margin for the side to move: MIN ahead, MAX behind, the
    midpoint when even or unclassified."""
    if situation is not None:
        if situation.margin in AHEAD_MARGINS:
            return RECOMMEND_MARGIN_MIN_CP
        if situation.margin in BEHIND_MARGINS:
            return RECOMMEND_MARGIN_MAX_CP
    return (RECOMMEND_MARGIN_MIN_CP + RECOMMEND_MARGIN_MAX_CP) // 2


def _better_for_stm(
    candidate: chess.engine.PovScore | None,
    rival: chess.engine.PovScore | None,
    turn: chess.Color,
    margin_cp: int,
) -> bool:
    """True iff `rival` is strictly better than `candidate` from `turn`'s
    perspective by more than `margin_cp`. Mate always trumps cp (margin
    doesn't apply); None falls back to conservative behavior."""
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
    return (rival_cp - cand_cp) > margin_cp


async def _dominance_searches(
    cache: SearchCache,
    engine_launcher: EngineLauncher,
    board: chess.Board,
    move: chess.Move,
    limit: chess.engine.Limit,
    *,
    bus: EventBus,
    game_id: str,
    cancel_token: CancelToken,
    settings_provider: SettingsProvider | None,
) -> tuple[dict, dict] | None:
    """(best_info, cand_info): the engine's free best on `board`, then the
    search restricted to `move` (skipped when `move` IS the free best -- the
    baseline then stands in for it). None when cancelled before both finish.
    Raises _SearchError. Each search is reported as a progress step first,
    so the panel names the search the board is showing, and finished with
    its result once it lands."""
    finish = await report_progress(PROGRESS_ENGINE_BEST, {_DEPTH_KEY: limit.depth})
    best_info, _ = await cache.get_or_search(
        engine_launcher, board.copy(stack=False), limit,
        bus=bus, game_id=game_id, cancel_token=cancel_token,
        settings_provider=settings_provider,
    )
    if cancel_token.cancelled:
        return None
    await finish(_step_result(board, best_info))
    # The engine's own pick: the baseline already searched this line, and
    # _is_dominated never rejects it -- a restricted repeat adds nothing.
    if _best_move(best_info) == move:
        return best_info, best_info
    finish = await report_progress(
        PROGRESS_SEARCH_MOVE, {MOVE_KEY: board.san(move), _DEPTH_KEY: limit.depth},
    )
    cand_info, _ = await cache.get_or_search(
        engine_launcher, board.copy(stack=False), limit,
        bus=bus, game_id=game_id, cancel_token=cancel_token,
        root_moves=[move],
        settings_provider=settings_provider,
    )
    if cancel_token.cancelled:
        return None
    await finish(_step_result(board, cand_info))
    return best_info, cand_info


def _step_result(board: chess.Board, info: dict) -> dict:
    """A dominance-check step's panel result: the move the search settled
    on, its eval, and the depth reached."""
    out: dict = {}
    best = _best_move(info)
    if best is not None:
        out[_SAN_KEY] = board.san(best)
    text = _score_to_cp(info.get(_SCORE_KEY)).get(_SCORE_TEXT_KEY)
    if text is not None:
        out[_SCORE_TEXT_KEY] = text
    depth = info.get(_DEPTH_KEY)
    if depth is not None:
        out[_DEPTH_KEY] = depth
    return out


def _best_move(best_info: dict) -> chess.Move | None:
    pv = best_info.get(_PV_KEY)
    return pv[0] if pv else None


def _is_dominated(
    move: chess.Move,
    best_info: dict,
    cand_info: dict,
    turn: chess.Color,
    margin_cp: int,
) -> bool:
    """True when the engine's free best beats `move` by more than
    `margin_cp` for `turn`. Never for the engine's own pick (search scores
    are mildly non-deterministic) or a move that forces mate for `turn` (a
    faster engine mate doesn't make it a mistake)."""
    if _best_move(best_info) == move:
        return False
    cand_score = cand_info.get(_SCORE_KEY)
    if cand_score is not None:
        cand_mate = cand_score.pov(turn).mate()
        if cand_mate is not None and cand_mate > 0:
            return False
    return _better_for_stm(cand_score, best_info.get(_SCORE_KEY), turn, margin_cp)


def make_recommend_move_tool(
    engine_launcher: EngineLauncher,
    bus: EventBus,
    board_provider: BoardProvider,
    game_id_provider: GameIdProvider | None = None,
    settings_provider: SettingsProvider | None = None,
    search_cache: SearchCache | None = None,
    book_move_provider: BookMoveProvider | None = None,
    situation_provider: SituationProvider | None = None,
) -> AnalyzeTool:
    """Build the `recommend_move` async tool. Parses UCI/SAN, then runs
    two engine searches (candidate-restricted + free) at the requested
    depth on the live position; if the engine's bestmove scores better
    for the side to move by more than the plan-aware margin
    (`recommend_margin_cp` of the `situation_provider`'s Situation),
    returns a structured error so the model can pivot. A side ahead
    that submits a repeating move is rejected before any search. On
    acceptance returns `{ok, uci, san, post_move_fen, candidate_score,
    engine_best_move, engine_best_score, depth}`.
    The turn's book move (`book_move_provider`) is accepted as-is, no
    search: `{ok, uci, san, post_move_fen, book: True}`.
    `search_cache`: see make_analyze_tool."""
    cache = search_cache or SearchCache()

    async def recommend_move(input_: dict, *, cancel_token: CancelToken) -> dict:
        board = board_provider()
        if board is None:
            return tool_error(NO_LIVE_POSITION_ERROR)
        raw = input_.get(MOVE_KEY)
        if not isinstance(raw, str) or not raw.strip():
            return tool_error(INVALID_INPUT_ERROR, MOVE_EMPTY_DETAIL)
        parsed, kind, detail = _parse_move_or_error(board, _strip_move_prefix(raw))
        if parsed is None:
            err = tool_error(kind, detail)
            # On an illegal move, hand back the legal moves for the piece the
            # model meant -- so it picks a real one instead of guessing again.
            if kind == _ILLEGAL_MOVE:
                piece_type = _intended_piece_type(board, _strip_move_prefix(raw))
                if piece_type is not None:
                    err["legal_moves"] = _legal_moves_for_piece(board, piece_type)
            return err
        san = board.san(parsed)
        uci = parsed.uci()
        scratch = board.copy(stack=False)
        scratch.push(parsed)
        # Theory needs no dominance check: the book move ships unsearched.
        if book_move_provider is not None and uci == book_move_provider():
            return {
                _OK_KEY: True, _UCI_KEY: uci, _SAN_KEY: san,
                _POST_MOVE_FEN_KEY: scratch.fen(), "book": True,
            }

        situation = situation_provider() if situation_provider is not None else None
        if (
            situation is not None
            and situation.margin in AHEAD_MARGINS
            and san in situation.repeats
        ):
            return {
                **tool_error(_RECOMMENDATION_REJECTED),
                REASON_KEY: _REPEAT_AHEAD_REASON.format(san=san),
                _UCI_KEY: uci, _SAN_KEY: san,
            }
        margin_cp = recommend_margin_cp(situation)

        # Floor recommend_move's two searches at the verification depth so
        # the dominance check can't confirm a move at a shallow depth the
        # model picked; it may go deeper, never below the floor.
        max_depth = _max_depth(settings_provider)
        floor = min(_verification_depth(settings_provider), max_depth)
        raw_depth = input_.get(_DEPTH_KEY)
        try:
            depth = min(int(raw_depth), max_depth) if raw_depth is not None else _DEFAULT_DEPTH
        except (TypeError, ValueError):
            depth = _DEFAULT_DEPTH
        depth = max(depth, floor)
        limit = chess.engine.Limit(depth=depth)
        game_id = (game_id_provider() if game_id_provider else None) or _ANALYZE_GAME_ID_FALLBACK

        try:
            searched = await _dominance_searches(
                cache, engine_launcher, board, parsed, limit,
                bus=bus, game_id=game_id, cancel_token=cancel_token,
                settings_provider=settings_provider,
            )
        except _SearchError as err:
            return tool_error(err.kind, err.detail)
        if searched is None:
            # Accept as-is; we couldn't finish verification.
            return {
                _OK_KEY: True, _UCI_KEY: uci, _SAN_KEY: san,
                _POST_MOVE_FEN_KEY: scratch.fen(), _CANCELLED_KEY: True,
            }
        best_info, cand_info = searched
        best_score = best_info.get(_SCORE_KEY)
        cand_score = cand_info.get(_SCORE_KEY)
        best_move = _best_move(best_info)

        result_common: dict = {
            _UCI_KEY: uci,
            _SAN_KEY: san,
            _DEPTH_KEY: depth,
            "candidate_score": _score_to_cp(cand_score),
            "engine_best_score": _score_to_cp(best_score),
        }
        if best_move is not None:
            result_common["engine_best_move"] = best_move.uci()
            result_common[_ENGINE_BEST_SAN_KEY] = board.san(best_move)

        if _is_dominated(parsed, best_info, cand_info, board.turn, margin_cp):
            best_san = result_common.get(_ENGINE_BEST_SAN_KEY) or "a stronger move"
            return {
                **tool_error(_RECOMMENDATION_REJECTED),
                REASON_KEY: (
                    f"A stronger move is available: {best_san}. Submit "
                    f"{best_san} unless you can show it is worse."
                ),
                **result_common,
            }

        return {_OK_KEY: True, _POST_MOVE_FEN_KEY: scratch.fen(), **result_common}

    return recommend_move


def make_refute_check(
    engine_launcher: EngineLauncher,
    bus: EventBus,
    board_provider: BoardProvider,
    game_id_provider: GameIdProvider | None = None,
    settings_provider: SettingsProvider | None = None,
    search_cache: SearchCache | None = None,
    situation_provider: SituationProvider | None = None,
) -> RefuteCheck:
    """Build the delegate's refutation check: the same dominance test
    recommend_move applies (plan-aware margin, verification-depth search),
    so a "refuted" verdict stands only when the final gate would reject the
    move too. `search_cache`: see make_analyze_tool."""
    cache = search_cache or SearchCache()

    async def refute_check(move: chess.Move, cancel_token: CancelToken) -> bool | None:
        board = board_provider()
        if board is None or move not in board.legal_moves:
            return None
        situation = situation_provider() if situation_provider is not None else None
        depth = min(_verification_depth(settings_provider), _max_depth(settings_provider))
        game_id = (game_id_provider() if game_id_provider else None) or _ANALYZE_GAME_ID_FALLBACK
        try:
            searched = await _dominance_searches(
                cache, engine_launcher, board, move, chess.engine.Limit(depth=depth),
                bus=bus, game_id=game_id, cancel_token=cancel_token,
                settings_provider=settings_provider,
            )
        except _SearchError:
            return None
        if searched is None:
            return None
        best_info, cand_info = searched
        return _is_dominated(
            move, best_info, cand_info, board.turn, recommend_margin_cp(situation),
        )

    return refute_check


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
            _UCI_KEY: move.uci(),
            _SAN_KEY: board.san(move),
        }
        payload.update(_score_to_cp(last_info.get(_SCORE_KEY)))
        depth = last_info.get(_DEPTH_KEY)
        if depth is not None:
            payload[_DEPTH_KEY] = depth
        pv = _pv_to_uci(board, last_info.get(_PV_KEY))
        if pv:
            payload[_PV_KEY] = pv
            payload["pv_uci"] = pv
        return payload

    return verify
