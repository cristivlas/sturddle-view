"""Perf baseline for pgn_tail._parse_delta before P4 (R6a) refactor."""
from __future__ import annotations

import pathlib

import pytest

pytestmark = pytest.mark.perf

from sturddle_view.tournament.pgn_tail import PgnTailer

FIXTURE = pathlib.Path(__file__).parent.parent / "fixtures" / "perf_200_games.pgn"
TOLERANCE = 0.10
_FILE_SIZE = FIXTURE.stat().st_size


def _bench_fn():
    PgnTailer(FIXTURE, lambda _: None)._parse_delta(0, _FILE_SIZE)


@pytest.mark.perf
@pytest.mark.benchmark(min_rounds=30)
def test_bench_parse_delta_200_games(benchmark, bench_compare):
    benchmark(_bench_fn)
    bench_compare("parse_delta_200_games", benchmark.stats.stats.min, tolerance=TOLERANCE)
