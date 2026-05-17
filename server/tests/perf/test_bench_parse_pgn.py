"""Perf baseline for import_position.parse_pgn before P4 (R6a) refactor."""
from __future__ import annotations

import pathlib

import chess.pgn
import pytest

from sturddle_view.play.import_position import parse_pgn
from tests.perf._bench import timeit_best_of

FIXTURE = pathlib.Path(__file__).parent.parent / "fixtures" / "perf_1k_games.pgn"
ITERATIONS = 20
TOLERANCE = 0.05

_GAME_TEXTS: list[str] = []


def _load_games():
    global _GAME_TEXTS
    if _GAME_TEXTS:
        return
    with FIXTURE.open(encoding="utf-8", errors="replace") as f:
        while True:
            game = chess.pgn.read_game(f)
            if game is None:
                break
            _GAME_TEXTS.append(str(game))


def _bench_fn():
    for text in _GAME_TEXTS:
        parse_pgn(text)


@pytest.mark.perf
def test_bench_parse_pgn_1k_games(bench_compare):
    _load_games()
    elapsed = timeit_best_of(_bench_fn, ITERATIONS)
    bench_compare("parse_pgn_1k_games", elapsed, tolerance=TOLERANCE)
