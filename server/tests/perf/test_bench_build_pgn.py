"""Perf gate for build_pgn (R6b / P5).

Baseline captured against _maybe_save_pgn's inner PGN build loop BEFORE the
refactor. After the refactor, tests that build_pgn() stays within tolerance.

Excludes file I/O (atomic_write_text); measures only PGN assembly.
"""
from __future__ import annotations

import chess
import chess.pgn
import pytest

from tests.perf._bench import timeit_best_of

ITERATIONS = 200
TOLERANCE = 0.10

_PGN_PATH = (
    __file__.replace("perf/test_bench_build_pgn.py", "fixtures/perf_100_ply_game.pgn")
)


def _load_game() -> chess.pgn.Game:
    with open(_PGN_PATH, encoding="utf-8") as fh:
        return chess.pgn.read_game(fh)


_GAME = _load_game()
_MOVES = [node.move for node in _GAME.mainline()]
_HEADERS = {
    "Event": "Sturddle View — Human vs Engine",
    "Site": "Sturddle View",
    "White": "Human",
    "Black": "FakeEngine",
    "TimeControl": "300+2",
}


def _build_via_inline():
    """Replicate _maybe_save_pgn's PGN assembly without file I/O."""
    board = chess.Board()
    for m in _MOVES:
        board.push(m)
    game = chess.pgn.Game.from_board(board)
    for k, v in _HEADERS.items():
        game.headers[k] = v
    game.headers["Result"] = "0-1"
    game.headers["Termination"] = "resignation"
    nodes = list(game.mainline())
    replay = chess.Board()
    for i, node in enumerate(nodes):
        mover_white = replay.turn == chess.WHITE
        replay.push(_MOVES[i])
        clock_val = 300.0 - i * 0.5
        node.set_clock(clock_val)
    return str(game)


@pytest.mark.perf
def test_bench_build_pgn_100_ply_game(bench_compare):
    elapsed = timeit_best_of(_build_via_inline, ITERATIONS)
    bench_compare("build_pgn_100_ply_inline", elapsed, tolerance=TOLERANCE)
