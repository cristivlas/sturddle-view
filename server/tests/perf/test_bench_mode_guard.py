"""Perf gate for mode guard (R5 / P8).

Baseline captured against the existing 4-bool check in HVE before mode.py
lands. After the refactor the Mode enum assert_allowed() call must stay
within 10% of this baseline.

1M inner loops so each timed round is far above perf_counter resolution.
"""
from __future__ import annotations

import pytest

from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine, TimeControl

INNER_LOOPS = 1_000_000


def _four_bool_guard(editing: bool, viewing: bool, paused: bool, analysis: bool) -> bool:
    """Inline equivalent of the HVE submit_move mode check (4-bool path)."""
    if editing:
        return False
    if viewing:
        return False
    if paused:
        return False
    if analysis:
        return False
    return True


def _bench_guard() -> None:
    for _ in range(INNER_LOOPS):
        _four_bool_guard(False, False, False, False)


@pytest.mark.perf
def test_bench_mode_guard(benchmark, bench_compare):
    benchmark(_bench_guard)
    bench_compare("mode_guard", benchmark.stats.stats.median)
