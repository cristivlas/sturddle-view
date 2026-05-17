"""Perf baseline for pgn_tail._parse_delta before P4 (R6a) refactor."""
from __future__ import annotations

import pathlib

import pytest

from sturddle_view.tournament.pgn_tail import PgnTailer
from tests.perf._bench import timeit_best_of

FIXTURE = pathlib.Path(__file__).parent.parent / "fixtures" / "perf_200_games.pgn"
ITERATIONS = 3
TOLERANCE = 0.05
_FILE_SIZE = FIXTURE.stat().st_size


def _bench_fn():
    PgnTailer(FIXTURE, lambda _: None)._parse_delta(0, _FILE_SIZE)


@pytest.mark.perf
def test_bench_parse_delta_200_games(bench_compare):
    elapsed = timeit_best_of(_bench_fn, ITERATIONS)
    bench_compare("parse_delta_200_games", elapsed, tolerance=TOLERANCE)
