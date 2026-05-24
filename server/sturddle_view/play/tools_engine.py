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

from ..llm.cancel import CancelToken
from .engine_supervisor import EngineSupervisor


log = logging.getLogger(__name__)


_DEFAULT_MAX_TIME_MS = 5_000
_DEFAULT_MAX_DEPTH = 30
# Hard caps -- the agent can request anything, but we clamp to these.
# UI exposure (Phase 4 Advanced collapsible) lets the user raise them.
MAX_TIME_MS = int(os.environ.get("SV_AI_ANALYZE_MAX_TIME_MS", _DEFAULT_MAX_TIME_MS))
MAX_DEPTH = int(os.environ.get("SV_AI_ANALYZE_MAX_DEPTH", _DEFAULT_MAX_DEPTH))

# Fallback when the caller passes neither time_ms nor depth -- a short
# search keeps the agent responsive without runaway cost.
_DEFAULT_TIME_MS = 1_000


EngineLauncher = Callable[[], EngineSupervisor]
AnalyzeTool = Callable[..., Awaitable[dict[str, Any]]]


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
        kwargs["time"] = _DEFAULT_TIME_MS / 1000.0
        used["time_ms"] = _DEFAULT_TIME_MS
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
    """Normalize a python-chess PovScore into wire fields. Mate is
    surfaced as `mate` (signed plies); regular evals as `score_cp` (white
    POV centipawns). Both can appear in the same record (cp from the
    last sub-mate info, mate flag set by the final info).

    POV: white-relative (positive = white better) regardless of who is
    to move. Matches view-mode convention and ignores `play_eval_pov`
    -- the AI agent shows the same numbers the engine panel does in
    view mode; user-relative flipping is a play-side concern."""
    if score is None:
        return {}
    s = score.white()
    out: dict = {}
    cp = s.score(mate_score=None)
    if cp is not None:
        out["score_cp"] = cp
    mate = s.mate()
    if mate is not None:
        out["mate"] = mate
    return out


def _pv_to_uci(board: chess.Board, pv: list[chess.Move] | None) -> list[str]:
    if not pv:
        return []
    return [m.uci() for m in pv]


def make_analyze_tool(engine_launcher: EngineLauncher) -> AnalyzeTool:
    """Build the `analyze` async tool.

    `engine_launcher()` returns a fresh `EngineSupervisor` per call --
    decouples the tool from how the production engine is resolved
    (registry + settings happen in `app.py`). Tests pass a launcher
    closed over a fake engine path; production passes one closed over
    `resolve_selected(app.state.engines, app.state.settings)`.
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

        sup = engine_launcher()
        try:
            engine, cleanup = await sup.spawn_throwaway()
        except Exception as exc:
            log.exception("analyze: engine spawn failed")
            return {"error": "engine_spawn_failed", "detail": str(exc)}

        last_info: chess.engine.InfoDict = {}
        cancelled = False
        try:
            with await engine.analysis(board, limit=limit) as analysis:
                async for info in analysis:
                    if info:
                        last_info = info
                    if cancel_token.cancelled:
                        cancelled = True
                        try:
                            analysis.stop()
                        except Exception:
                            pass
                        # Drain remaining items so the engine sees
                        # bestmove and the context manager exits cleanly.
                        async for _ in analysis:
                            pass
                        break
        except chess.engine.EngineTerminatedError as exc:
            log.error("analyze: engine terminated mid-search")
            return {"error": "engine_terminated", "detail": str(exc)}
        except asyncio.CancelledError:
            raise
        finally:
            try:
                await cleanup()
            except Exception:
                log.exception("analyze: engine cleanup failed")

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
