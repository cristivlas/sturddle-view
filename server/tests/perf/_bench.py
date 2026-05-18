"""Shared timing helpers for perf-harness smoke tests only.

Real benches use the `benchmark` fixture from pytest-benchmark, which
handles auto-calibration, warmup, GC disable, and outlier rejection.
The bare `timeit` shim here is kept solely to exercise the harness
plumbing (env-var gating, --update-perf-baselines path) in test_smoke.py.
"""
from __future__ import annotations

import timeit as _timeit
from typing import Callable


def timeit(fn: Callable, iterations: int) -> float:
    """Return total wall-clock seconds for *iterations* calls of *fn*."""
    return _timeit.timeit(fn, number=iterations)
