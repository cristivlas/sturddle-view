"""Perf gate for mode guard (R5 / P8).

Baseline captured against the 4-bool inline check equivalent to the old
submit_move preamble. The refactor replaces those checks with an inline
bitmask check (mode & op._mask) at each guard site -- no function call.
assert_allowed() is kept for the parametrized matrix test only.

1M inner loops so each timed round is far above perf_counter resolution.
"""
from __future__ import annotations

import pytest

from sturddle_view.play.mode import Mode, ModeConflictError, Op

INNER_LOOPS = 1_000_000
_SUBMIT_MASK = Op.SUBMIT_MOVE._mask


def _bench_inline_guard() -> None:
    mode = Mode.PLAY
    for _ in range(INNER_LOOPS):
        if not (mode & _SUBMIT_MASK):
            raise ModeConflictError(mode, Op.SUBMIT_MOVE)


@pytest.mark.perf
def test_bench_mode_guard(benchmark, bench_compare):
    """Inline bitmask guard (mode & op._mask) must stay within 10% of
    the 4-bool baseline captured before the P8 refactor."""
    benchmark(_bench_inline_guard)
    bench_compare("mode_guard", benchmark.stats.stats.median)
