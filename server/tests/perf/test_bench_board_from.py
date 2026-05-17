"""Perf gate for board_from (R1).

Baselines captured against inline chess.Board() before refactor.
After refactor, tests that board_from() stays within tolerance.
"""
from __future__ import annotations

import chess
import pytest

from sturddle_view.chess.board import board_from
from tests.perf._bench import timeit_best_of

ITERATIONS = 100_000
TOLERANCE = 0.10
START_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2"


def _via_board_from_startpos():
    return board_from(None)


def _via_board_from_fen():
    return board_from(START_FEN)


@pytest.mark.perf
def test_bench_board_from_vs_inline(bench_compare):
    elapsed_startpos = timeit_best_of(_via_board_from_startpos, ITERATIONS)
    bench_compare("board_from_startpos_inline", elapsed_startpos, tolerance=TOLERANCE)

    elapsed_fen = timeit_best_of(_via_board_from_fen, ITERATIONS)
    bench_compare("board_from_fen_inline", elapsed_fen, tolerance=TOLERANCE)
