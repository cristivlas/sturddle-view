"""Shared timing helpers for perf benches."""
from __future__ import annotations

import timeit as _timeit
from typing import Callable


def timeit(fn: Callable, iterations: int) -> float:
    """Return total wall-clock seconds for *iterations* calls of *fn*."""
    return _timeit.timeit(fn, number=iterations)


def timeit_best_of(fn: Callable, iterations: int, rounds: int = 3) -> float:
    """Return the minimum total wall-clock seconds across *rounds* runs."""
    return min(_timeit.repeat(fn, number=iterations, repeat=rounds))
