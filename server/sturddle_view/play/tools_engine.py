"""Engine-backed tools for the AI analysis agent.

`analyze` is the first real tool: spawns a throwaway UCI engine,
configures a search limit (time_ms and/or depth), drains info events
until the engine signals bestmove (or the cancel token flips), and
returns a structured eval record.

Per spec §Architecture: one throwaway engine per analyze call (pool
later if perf demands). Hard caps live as named consts + SV_ env vars
(no magic numbers); out-of-range requests are clamped, not rejected,
so the agent never stalls on a guardrail.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable

import chess
import chess.engine

from ..events import Event, EventBus
from ..llm import ToolSpec
from ..llm.cancel import CancelToken
from .engine_analysis import spawn_analysis_engine
from .engine_info_pump import pump_engine_info
from .engine_supervisor import EngineSupervisor


log = logging.getLogger(__name__)


_DEFAULT_MAX_TIME_MS = 5_000
_DEFAULT_MAX_DEPTH = 25
# Hard caps -- the agent can request anything, but we clamp to these.
# The env override is for ops; UI exposure is pending.
MAX_TIME_MS = int(os.environ.get("SV_AI_ANALYZE_MAX_TIME_MS", _DEFAULT_MAX_TIME_MS))
MAX_DEPTH = int(os.environ.get("SV_AI_ANALYZE_MAX_DEPTH", _DEFAULT_MAX_DEPTH))

# Fallback when caller passes neither time_ms nor depth. Depth-based
# (not time-based): more consistent quality across positions and engine
# loads. 20 plies is the floor that gives reliable tactical resolution
# for coach-style prose; lower values surface noisy bestmoves that
# embarrass the agent. Server-side enforcement of the floor against
# model-supplied depth is an open mitigation -- see
# docs/ai-analysis-progress.md.
_DEFAULT_DEPTH = 20

# top_moves: hard cap on the model-supplied candidate list length.
# Each candidate runs one sequential search; cost scales linearly.
_DEFAULT_TOP_MOVES_MAX_N = 5
TOP_MOVES_MAX_N = int(os.environ.get("SV_AI_TOP_MOVES_MAX_N", _DEFAULT_TOP_MOVES_MAX_N))

# Per-candidate default depth -- match analyze's default; with the
# model-supplied list capped at TOP_MOVES_MAX_N, worst-case cost is
# bounded and shallower defaults were producing weak candidate ranking.
_DEFAULT_TOP_MOVES_DEPTH = _DEFAULT_DEPTH


EngineLauncher = Callable[[], EngineSupervisor]
GameIdProvider = Callable[[], str | None]
BoardProvider = Callable[[], chess.Board | None]
# Provider that returns the live app settings object (or None). Lets
# the throwaway analysis engine inherit Threads/Hash/Syzygy/etc. from
# the same source HVE's analysis path uses.
SettingsProvider = Callable[[], Any]
AnalyzeTool = Callable[..., Awaitable[dict[str, Any]]]

# Default fallback game_id when the tool runs outside a live HVE
# session (e.g. unit tests, future post-game path). engine_info events
# still need *some* game_id so the client's per-session muxing works.
_ANALYZE_GAME_ID_FALLBACK = "ai-analyze"


# Wire-shape ToolSpec describing this tool to the model. Lives next to
# the implementation so prompt text + schema + behavior move together;
# app.py only wires (spec, callable) into the registry.
TOP_MOVES_TOOL_SPEC = ToolSpec(
    name="top_moves",
    description=(
        "Deep-evaluate a list of candidate moves you are considering in "
        "the live position. YOU supply the candidates -- chess "
        "understanding picks them, the engine ranks them. Each candidate "
        "is searched with a fresh throwaway engine; returns per-move "
        "entries sorted best-first FOR THE SIDE TO MOVE, carrying "
        "move_uci, move_san, and the white-POV eval fields (score_cp / "
        "score_pawns / score_text / mate / depth / pv). Illegal or "
        "unparseable moves come back as per-entry errors; legal ones "
        "are still searched. Operates on the live game position -- no "
        f"FEN input. List capped at {TOP_MOVES_MAX_N}; extras are "
        "dropped. Per-tool caps apply to time_ms and depth."
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
                ),
            },
            "time_ms": {
                "type": "integer",
                "description": "Per-candidate search time in milliseconds (clamped to server cap).",
            },
            "depth": {
                "type": "integer",
                "description": "Per-candidate maximum depth (clamped to server cap).",
            },
        },
        "required": ["moves"],
    },
)


_PIECE_AT_CARD = (
    "Card for `piece_at`. Any claim that a specific piece sits on or "
    "moves from a specific square in the current position -- explicit "
    "(\"the knight on f3\") or implied by verbs like centralize, "
    "advance, push, capture, retreat, reroute, occupy, defend, attack, "
    "pin, fork, develop -- must be confirmed with `piece_at` before "
    "being written. Non-negotiable. Result is the piece symbol (e.g. "
    "'N', 'p') or null when empty; symbol case encodes color (upper = "
    "white, lower = black). Applies only to the live position, not to "
    "squares inside calculated variations."
)


PIECE_AT_TOOL_SPEC = ToolSpec(
    name="piece_at",
    description=(
        "Return the piece on a square in the live position, or null if "
        "empty. Non-negotiable: call before naming any piece on a "
        "specific square in prose."
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
        },
        "required": ["square"],
    },
    card=_PIECE_AT_CARD,
)


_VALIDATE_MOVE_CARD = (
    "Card for `validate_move`. Before naming any move as playable in "
    "the current position, confirm it with `validate_move`. "
    "Non-negotiable. Applies only to the current position, not to "
    "moves inside calculated lines (those are reasoned about, not "
    "claimed as legal in the live position). Result fields: `legal` "
    "(bool), `uci`, `san`. If `legal` is false, do not name the move "
    "in prose."
)


VALIDATE_MOVE_TOOL_SPEC = ToolSpec(
    name="validate_move",
    description=(
        "Check whether a move (UCI or SAN) is legal in the live "
        "position. Non-negotiable: call before naming any move as "
        "playable in the current position (not required for moves "
        "inside calculated lines)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "move": {
                "type": "string",
                "description": (
                    "Move in UCI (e.g. 'g1f3', 'e7e8q') or SAN (e.g. "
                    "'Nf3', 'O-O', 'exd5')."
                ),
            },
        },
        "required": ["move"],
    },
    card=_VALIDATE_MOVE_CARD,
)


ANALYZE_TOOL_SPEC = ToolSpec(
    name="analyze",
    description=(
        "Run an engine search on a position. Returns a structured eval. "
        "Evaluation fields are white-POV: score_cp (centipawns, int), "
        "score_pawns (pawn units, float -- score_cp / 100), score_text "
        "(presentation string, e.g. '+0.02' or '+M3'), mate (signed "
        "plies-to-mate when present). Use score_text for prose; use "
        "score_cp for any arithmetic. Also returns depth, pv, bestmove. "
        "Hard caps apply to time_ms and depth -- requests above the cap "
        "are clamped, not rejected."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "fen": {
                "type": "string",
                "description": "FEN string, or 'startpos' for the initial position.",
            },
            "time_ms": {
                "type": "integer",
                "description": "Search time in milliseconds (clamped to server cap).",
            },
            "depth": {
                "type": "integer",
                "description": "Maximum depth (clamped to server cap).",
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


def _clamp_limits(input_: dict) -> tuple[chess.engine.Limit, dict]:
    """Build a chess.engine.Limit honoring the agent's requested
    time_ms / depth, clamped to MAX_TIME_MS / MAX_DEPTH. Returns the
    Limit and a debug dict echoing the effective values (used in the
    tool output for both observability and test assertions)."""
    raw_time_ms = input_.get("time_ms")
    raw_depth = input_.get("depth")
    used: dict = {}
    kwargs: dict = {}
    if raw_time_ms is None and raw_depth is None:
        kwargs["depth"] = _DEFAULT_DEPTH
        used["depth"] = _DEFAULT_DEPTH
    if raw_time_ms is not None:
        t = max(0, min(int(raw_time_ms), MAX_TIME_MS))
        kwargs["time"] = t / 1000.0
        used["time_ms"] = t
    if raw_depth is not None:
        d = max(1, min(int(raw_depth), MAX_DEPTH))
        kwargs["depth"] = d
        used["depth"] = d
    return chess.engine.Limit(**kwargs), used


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


# Sort key for white-POV PovScore: mate-for-white > +cp > -cp > mate-against.
# Used to rank top_moves candidates without re-scoring each comparison.
_MATE_RANK = 10**9


def _white_pov_sort_key(score: chess.engine.PovScore | None) -> int:
    if score is None:
        return -_MATE_RANK - 1
    s = score.white()
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
    Raises _SearchError on spawn or mid-search engine death so callers can
    map kind -> structured error envelope.

    `root_moves`: when set, the engine is restricted to playing one of
    these moves at the root (UCI `searchmoves`). Used by top_moves to
    score a specific candidate without push/pop tricks.

    `settings_provider`: returns the live app settings so the throwaway
    engine inherits Threads/Hash/SyzygyPath/etc. via the shared
    engine-analysis helper. None falls back to no overrides (matches
    test doubles that don't carry settings).

    Publishes engine_info events via pump_engine_info but does NOT emit
    engine_search_start -- the caller decides when to clear the panel
    (analyze emits once; top_moves emits once for the whole batch)."""
    sup = engine_launcher()
    settings = settings_provider() if settings_provider else None
    try:
        engine, cleanup = await spawn_analysis_engine(sup, settings)
    except Exception as exc:
        log.exception("search: engine spawn failed")
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
                pov=chess.WHITE,
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
) -> AnalyzeTool:
    """Build the `analyze` async tool.

    `engine_launcher()` returns a fresh `EngineSupervisor` per call --
    decouples the tool from how the production engine is resolved
    (registry + settings happen in `app.py`).

    `bus` is the same event bus HVE publishes engine_info events to.
    The tool publishes there too so the PV-table window and the board
    arrow light up while a tool-call search is running.

    `game_id_provider()` returns the current live game's id at call
    time. When None or the provider returns None, events are tagged
    with a fallback id so they still flow through the WS muxing.

    `settings_provider()` returns the app settings so the throwaway
    engine inherits Threads/Hash/SyzygyPath via the shared spawn
    helper. None is acceptable (tests).
    """
    async def analyze(input_: dict, *, cancel_token: CancelToken) -> dict:
        fen = input_.get("fen")
        if not isinstance(fen, str) or not fen:
            return {"error": "missing_fen"}
        try:
            board = _parse_fen(fen)
        except ValueError as exc:
            return {"error": "invalid_fen", "detail": str(exc)}

        limit, limits_used = _clamp_limits(input_)

        game_id = (game_id_provider() if game_id_provider else None) or _ANALYZE_GAME_ID_FALLBACK
        await bus.publish(Event(kind="engine_search_start", game_id=game_id, payload={}))

        try:
            last_info, cancelled = await _run_one_search(
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
        time_used = last_info.get("time")
        if time_used is not None:
            out["time_ms"] = int(time_used * 1000)
        pv = _pv_to_uci(board, last_info.get("pv"))
        if pv:
            out["pv"] = pv
            out["bestmove"] = pv[0]
        return out

    return analyze


def _clamp_top_moves_limits(input_: dict) -> tuple[chess.engine.Limit, dict]:
    """Per-candidate limit for top_moves. Shallower depth default than
    analyze since cost multiplies by N."""
    raw_time_ms = input_.get("time_ms")
    raw_depth = input_.get("depth")
    used: dict = {}
    kwargs: dict = {}
    if raw_time_ms is None and raw_depth is None:
        kwargs["depth"] = _DEFAULT_TOP_MOVES_DEPTH
        used["depth"] = _DEFAULT_TOP_MOVES_DEPTH
    if raw_time_ms is not None:
        t = max(0, min(int(raw_time_ms), MAX_TIME_MS))
        kwargs["time"] = t / 1000.0
        used["time_ms"] = t
    if raw_depth is not None:
        d = max(1, min(int(raw_depth), MAX_DEPTH))
        kwargs["depth"] = d
        used["depth"] = d
    return chess.engine.Limit(**kwargs), used


def _parse_candidate_move(board: chess.Board, raw: str) -> tuple[chess.Move | None, dict | None]:
    """Try UCI then SAN. Returns (move, None) on success, (None, error_entry)
    on failure. Error entry carries `move_input` so the model can match
    it back to the input list."""
    candidate = raw.strip()
    if not candidate:
        return None, {"move_input": raw, "error": "invalid_input", "detail": "empty move string"}
    for parse in (board.parse_uci, board.parse_san):
        try:
            return parse(candidate), None
        except chess.IllegalMoveError as exc:
            return None, {"move_input": raw, "error": "illegal_move", "detail": str(exc)}
        except (chess.InvalidMoveError, chess.AmbiguousMoveError):
            continue
    return None, {"move_input": raw, "error": "invalid_move",
                  "detail": f"could not parse {candidate!r} as UCI or SAN"}


def make_top_moves_tool(
    engine_launcher: EngineLauncher,
    bus: EventBus,
    board_provider: BoardProvider,
    game_id_provider: GameIdProvider | None = None,
    settings_provider: SettingsProvider | None = None,
) -> AnalyzeTool:
    """Deep-evaluate a model-supplied list of candidate moves. Each
    legal move runs a root-restricted search; illegal or unparseable
    moves come back as per-entry errors. Publishes one
    `engine_search_start` for the batch, then per-candidate
    engine_info events."""
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

        limit, limits_used = _clamp_top_moves_limits(input_)
        game_id = (game_id_provider() if game_id_provider else None) or _ANALYZE_GAME_ID_FALLBACK
        await bus.publish(Event(kind="engine_search_start", game_id=game_id, payload={}))

        stm_is_white = board.turn == chess.WHITE
        cancelled_any = False
        entries: list[dict] = []
        for move in parsed:
            if cancel_token.cancelled:
                cancelled_any = True
                break
            try:
                last_info, cancelled = await _run_one_search(
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
            entry["_sort_key"] = _white_pov_sort_key(score)
            entries.append(entry)
            if cancelled:
                cancelled_any = True
                break

        entries.sort(key=lambda c: c["_sort_key"], reverse=stm_is_white)
        for c in entries:
            c.pop("_sort_key", None)

        out: dict = {
            "side_to_move": "white" if stm_is_white else "black",
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
    """Build the `piece_at` async tool. Reads the live board (via
    board_provider) and reports what occupies the requested square --
    the model's self-check against hallucinated piece placements."""
    async def piece_at(input_: dict, *, cancel_token: CancelToken) -> dict:
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
        candidate = raw.strip()
        for parse in (board.parse_uci, board.parse_san):
            try:
                move = parse(candidate)
            except chess.IllegalMoveError as exc:
                return {"error": "illegal_move", "detail": str(exc)}
            except (chess.InvalidMoveError, chess.AmbiguousMoveError):
                continue
            return {"legal": True, "uci": move.uci(), "san": board.san(move)}
        return {"error": "invalid_move", "detail": f"could not parse {candidate!r} as UCI or SAN"}

    return validate_move


_RECOMMEND_MOVE_CARD = (
    "Card for `recommend_move`. Call this tool at the end of your turn "
    "with the move you stand behind. Non-negotiable, same as "
    "`validate_move` and `piece_at`: this is a tool invocation, not a "
    "sentence in your prose. The engine evaluates your candidate and "
    "the engine's own best move at the requested depth; if a different "
    "move scores better for the side to move, the call returns "
    "error=recommendation_rejected and you must submit a different "
    "move -- repeating the same recommendation is not allowed."
)


RECOMMEND_MOVE_TOOL_SPEC = ToolSpec(
    name="recommend_move",
    description=(
        "Call this tool with your final move recommendation. "
        "Non-negotiable: call (do not narrate) at the end of your turn "
        "after you've decided. Validates legality, scores the candidate "
        "and the engine's best move at the requested depth, and returns "
        "error=recommendation_rejected with a `reason` when a better "
        "move exists -- in that case you must submit a different move. "
        "Returns the post-move FEN on acceptance."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "move": {
                "type": "string",
                "description": (
                    "Move in UCI (e.g. 'g1f3', 'e7e8q') or SAN (e.g. "
                    "'Nf3', 'O-O', 'exd5')."
                ),
            },
            "depth": {
                "type": "integer",
                "description": (
                    f"Search depth for the dominance check (clamped to {MAX_DEPTH})."
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
    perspective. Mate always trumps cp; equal cp = not better."""
    if rival is None:
        return False
    if candidate is None:
        return True
    return rival.pov(turn) > candidate.pov(turn)


def make_recommend_move_tool(
    engine_launcher: EngineLauncher,
    bus: EventBus,
    board_provider: BoardProvider,
    game_id_provider: GameIdProvider | None = None,
    settings_provider: SettingsProvider | None = None,
) -> AnalyzeTool:
    """Build the `recommend_move` async tool. Parses UCI/SAN, then runs
    two engine searches (candidate-restricted + free) at the requested
    depth on the live position; if the engine's bestmove scores better
    for the side to move, returns a structured error so the model can
    pivot. On acceptance returns `{ok, uci, san, post_move_fen,
    candidate_score, engine_best_move, engine_best_score, depth}`."""
    async def recommend_move(input_: dict, *, cancel_token: CancelToken) -> dict:
        board = board_provider()
        if board is None:
            return {"error": "no_live_position"}
        raw = input_.get("move")
        if not isinstance(raw, str) or not raw.strip():
            return {"error": "invalid_input", "detail": "move must be a non-empty string"}
        candidate = raw.strip()
        parsed: chess.Move | None = None
        for parse in (board.parse_uci, board.parse_san):
            try:
                parsed = parse(candidate)
                break
            except chess.IllegalMoveError as exc:
                return {"error": "illegal_move", "detail": str(exc)}
            except (chess.InvalidMoveError, chess.AmbiguousMoveError):
                continue
        if parsed is None:
            return {"error": "invalid_move", "detail": f"could not parse {candidate!r} as UCI or SAN"}
        san = board.san(parsed)
        uci = parsed.uci()
        scratch = board.copy(stack=False)
        scratch.push(parsed)

        raw_depth = input_.get("depth")
        try:
            depth = max(1, min(int(raw_depth), MAX_DEPTH)) if raw_depth is not None else _DEFAULT_DEPTH
        except (TypeError, ValueError):
            depth = _DEFAULT_DEPTH
        limit = chess.engine.Limit(depth=depth)
        game_id = (game_id_provider() if game_id_provider else None) or _ANALYZE_GAME_ID_FALLBACK

        scratch_live = board.copy(stack=False)

        # Search A: engine's free best move on the live position.
        try:
            best_info, _ = await _run_one_search(
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
            cand_info, _ = await _run_one_search(
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

        if _better_for_stm(cand_score, best_score, board.turn):
            best_san = result_common.get("engine_best_san") or "a stronger move"
            return {
                "error": "recommendation_rejected",
                "reason": (
                    f"Engine prefers {best_san}. "
                    "Submit a different move."
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
):
    """Build the end-of-turn recommend-verifier. Returns a callable that
    runs a searchmoves-restricted deep search on the recommended move
    and returns a payload dict (or None on failure). The coordinator
    emits the `ai_recommendation` event through its own _emit so the
    payload gets a seq stamp and lands in the replay buffer."""
    async def verify(move: chess.Move, cancel_token: CancelToken) -> dict | None:
        board = board_provider()
        if board is None or move not in board.legal_moves:
            return None
        game_id = (game_id_provider() if game_id_provider else None) or _ANALYZE_GAME_ID_FALLBACK
        limit = chess.engine.Limit(depth=_DEFAULT_DEPTH)
        try:
            last_info, _cancelled = await _run_one_search(
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
