"""Perf gate for engine info serialization (R7 / P9).

Baseline captured against HVE._serialize_info BEFORE the refactor. After the
refactor, tests that the extracted chess/engine_info.serialize_info stays
within tolerance.

Typical info dict (depth + score + multi-move pv) on a startpos board.
"""
from __future__ import annotations

import chess
import chess.engine
import pytest

from sturddle_view.chess.engine_info import serialize_info

INNER_LOOPS = 1000


def _make_info() -> chess.engine.InfoDict:
    board = chess.Board()
    pv = [
        chess.Move.from_uci("e2e4"),
        chess.Move.from_uci("e7e5"),
        chess.Move.from_uci("g1f3"),
        chess.Move.from_uci("b8c6"),
        chess.Move.from_uci("f1b5"),
    ]
    return {
        "depth": 18,
        "seldepth": 24,
        "time": 1234,
        "nodes": 1_500_000,
        "nps": 1_200_000,
        "hashfull": 450,
        "tbhits": 0,
        "score": chess.engine.PovScore(chess.engine.Cp(42), chess.WHITE),
        "pv": pv,
    }


def _bench_serialize(info, board) -> None:
    for _ in range(INNER_LOOPS):
        serialize_info(info, board=board, pov=chess.WHITE)


@pytest.mark.perf
def test_bench_serialize_info(benchmark, bench_compare):
    info = _make_info()
    board = chess.Board()
    benchmark(_bench_serialize, info, board)
    bench_compare("serialize_info_typical", benchmark.stats.stats.median)
