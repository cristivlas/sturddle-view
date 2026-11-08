"""Turn elapsed time: the Analysis panel's timer source.

The coordinator's clock is swapped for a fake one so every figure is exact.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from sturddle_view.events import EventBus
from sturddle_view.llm import CannedProvider, ProviderChunk
from sturddle_view.play import ai_analysis
from sturddle_view.play.ai_analysis import AIAnalysisCoordinator

T0 = 100.0
MID_TURN = 102.5
MID_TURN_MS = 2500


class _Hanging(CannedProvider):
    """One text chunk, then suspends until cancelled."""

    async def stream(self, system, messages, tools=None, *, transcript=None, round_index=0, thinking=None, force_tool_call=False):
        yield ProviderChunk(kind="text", text="thinking about it")
        await asyncio.Future()


@pytest.mark.asyncio
async def test_elapsed_tracks_turn_and_rides_done(monkeypatch):
    clock = [T0]
    monkeypatch.setattr(ai_analysis, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, _Hanging())
    assert coord.turn_elapsed_ms() is None

    run_task = asyncio.create_task(coord.run(game_id="g1"))
    await queue.get()  # first chunk: the turn is in flight
    clock[0] = MID_TURN
    assert coord.turn_elapsed_ms() == MID_TURN_MS

    await coord.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    done = await queue.get()
    assert done.payload.get("done") is True
    assert done.payload["elapsed_ms"] == MID_TURN_MS
    assert coord.turn_elapsed_ms() is None
