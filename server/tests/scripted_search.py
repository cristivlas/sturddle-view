"""Shared stand-in for tools_engine._run_one_search: per-position scripted
results, so play_out / test_aim tests run the real tool logic and the real
SearchCache without spawning an engine."""
from __future__ import annotations

import chess
import chess.engine

# (position EPD, forced root move UCI or None) -> (cp or a chess.engine
# Score for the side to move, pv in UCI). Side-to-move POV, like a UCI
# engine reports it.
SearchScript = dict[tuple[str, str | None], tuple[int | chess.engine.Score, list[str]]]


def epd_after(*sans: str, start: chess.Board | None = None) -> str:
    """EPD of the position after `sans` from `start` (the initial position)."""
    board = (start or chess.Board()).copy()
    for san in sans:
        board.push_san(san)
    return board.epd()


def scripted_search(script: SearchScript, calls: list[tuple[str, str | None]]):
    """A _run_one_search stand-in serving `script`; records each search's
    (EPD, forced root move) in `calls`. depth echoes the request."""
    async def fake_search(engine_launcher, board, limit, *, root_moves=None, **kw):
        root = root_moves[0].uci() if root_moves else None
        key = (board.epd(), root)
        calls.append(key)
        value, pv = script[key]
        score = value if isinstance(value, chess.engine.Score) else chess.engine.Cp(value)
        return {
            "depth": limit.depth,
            "score": chess.engine.PovScore(score, board.turn),
            "pv": [chess.Move.from_uci(uci) for uci in pv],
        }, False
    return fake_search
