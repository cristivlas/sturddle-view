"""Walking-skeleton e2e for AI analysis coordinator.

Verifies the provider -> coordinator -> bus contract with the
CannedProvider as the test double (the spec-mandated mock boundary).
No sleeps, no HTTP -- the bus delivers events synchronously via
asyncio.Queue.put_nowait, so a queue drain is the natural sync.
"""
from __future__ import annotations

import asyncio

import pytest

from sturddle_view.events import EventBus
from sturddle_view.llm import CannedProvider, ProviderChunk
from sturddle_view.llm.canned import SKELETON_CHUNKS
from sturddle_view.play.ai_analysis import AIAnalysisCoordinator


async def _drain(bus: EventBus, queue: asyncio.Queue, *, until_done: bool) -> list:
    """Pull events off the bus until we see the done marker.

    The coordinator always emits a terminal `ai_info` event with
    payload.done=True; once observed, we stop. No timer needed -- the
    coordinator owns the cadence and the queue is in-process.
    """
    events = []
    while True:
        evt = await queue.get()
        events.append(evt)
        if until_done and evt.payload.get("done"):
            return events


@pytest.mark.asyncio
async def test_canned_provider_streams_ai_info_chunks():
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, CannedProvider())

    await coord.run(game_id="g1")
    events = await _drain(bus, queue, until_done=True)

    assert all(e.kind == "ai_info" for e in events)
    assert all(e.game_id == "g1" for e in events)
    deltas = [e.payload["delta"] for e in events if "delta" in e.payload]
    assert deltas == list(SKELETON_CHUNKS)
    terminal = events[-1].payload
    # Subset match -- payloads carry an auto-injected `seq` for the
    # client replay-dedupe protocol.
    assert terminal.get("done") is True


@pytest.mark.asyncio
async def test_cancel_during_stream_emits_cancelled_done():
    # A custom provider that yields one chunk and then suspends forever,
    # so the cancel path is exercised deterministically without any timer.
    class _Hanging(CannedProvider):
        async def stream(self, system, messages, tools=None, *, transcript=None, round_index=0):
            yield_chunk = next(iter(SKELETON_CHUNKS))
            yield ProviderChunk(kind="text", text=yield_chunk)
            # Wait on a future that never resolves; cancellation will
            # propagate from the consumer side.
            await asyncio.Future()

    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, _Hanging())

    run_task = asyncio.create_task(coord.run(game_id="g2"))
    # Wait until the first chunk lands on the bus -- that proves the
    # provider is in its second iteration (inside the never-resolving
    # await), at which point cancel is guaranteed to interrupt it.
    first = await queue.get()
    assert first.kind == "ai_info"
    assert first.payload.get("delta") is not None

    await coord.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    terminal = await queue.get()
    assert terminal.kind == "ai_info"
    assert terminal.game_id == "g2"
    assert terminal.payload.get("done") is True
    assert terminal.payload.get("cancelled") is True


@pytest.mark.asyncio
async def test_concurrent_run_serializes_on_lock():
    # Two run() calls overlapping must not interleave events for the
    # same coordinator -- the lock guarantees turn-by-turn ordering.
    chunks_a = ("A1\n", "A2\n")
    chunks_b = ("B1\n", "B2\n")

    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, CannedProvider())

    t1 = asyncio.create_task(
        coord.run(game_id="g", provider=CannedProvider(chunks_a))
    )
    t2 = asyncio.create_task(
        coord.run(game_id="g", provider=CannedProvider(chunks_b))
    )
    await asyncio.gather(t1, t2)

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    deltas = [e.payload["delta"] for e in events if "delta" in e.payload]
    # The first turn's deltas must appear contiguously before the second
    # turn's (no interleaving), in whichever order the tasks acquired
    # the lock. Both possibilities are valid; interleaving is not.
    valid_orderings = [
        list(chunks_a) + list(chunks_b),
        list(chunks_b) + list(chunks_a),
    ]
    assert deltas in valid_orderings
