"""Perf gate for cancel-think grace period (R3 / P6).

Pins the 500ms timeout on engine 'stop' acknowledgement. After the
refactor, EngineSupervisor.cancel must preserve the same grace period
(no accidental bumps to 2s, no premature transport close).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine

TOLERANCE = 0.20  # wider: timeout is wall-clock 0.5s plus loop overhead
ROUNDS = 5


class _WedgedEngine:
    """Stub UCI engine that never responds to stop. Mirrors the surface
    HVE._cancel_think touches: send_line, transport.close."""

    def __init__(self) -> None:
        self.transport = _Transport()
        self.stop_calls = 0

    def send_line(self, line: str) -> None:
        if line == "stop":
            self.stop_calls += 1


class _Transport:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


async def _hung_think() -> None:
    # Simulates a think task that doesn't honor stop -- waits past the
    # grace period so the supervisor must fall back to transport.close.
    await asyncio.sleep(5.0)


async def _one_cancel() -> float:
    bus = EventBus()
    hve = HumanVsEngine(engine_path="/nonexistent", bus=bus)
    hve._engine = _WedgedEngine()
    think_task = asyncio.create_task(_hung_think())
    hve._think_task = think_task
    t0 = time.perf_counter_ns()
    await hve._cancel_think()
    elapsed = (time.perf_counter_ns() - t0) / 1e9
    if not think_task.done():
        think_task.cancel()
        try:
            await think_task
        except (asyncio.CancelledError, Exception):
            pass
    return elapsed


def _bench_one() -> float:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_one_cancel())
    finally:
        loop.close()


@pytest.mark.perf
def test_bench_cancel_think_grace_period(bench_compare):
    samples = sorted(_bench_one() for _ in range(ROUNDS))
    median = samples[len(samples) // 2]
    bench_compare("cancel_think_grace_period", median, tolerance=TOLERANCE)
