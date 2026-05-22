"""Perf baseline for import_position.parse_pgn before P4 (R6a) refactor."""
from __future__ import annotations

import pathlib

import chess.pgn
import pytest

pytestmark = pytest.mark.perf

from sturddle_view.play.import_position import parse_pgn

FIXTURE = pathlib.Path(__file__).parent.parent / "fixtures" / "perf_200_games.pgn"
TOLERANCE = 0.10

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
            if game.next() is not None:  # skip 0-move games
                _GAME_TEXTS.append(str(game))


def _bench_fn():
    for text in _GAME_TEXTS:
        parse_pgn(text)


@pytest.mark.perf
def test_bench_parse_pgn_200_games(benchmark, bench_compare):
    _load_games()
    benchmark(_bench_fn)
    bench_compare("parse_pgn_200_games", benchmark.stats.stats.median, tolerance=TOLERANCE)
