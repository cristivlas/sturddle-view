"""Position-check wiring in the AIAnalysisCoordinator loop.

Every round's prose is checked against the live board. A mismatch emits an
ai_position_note event (UI self-correction) and injects a fact-anchored
[position check] user message so the model corrects itself next round. A
re-flagged item escalates the corrective wording.
"""
from __future__ import annotations

import chess
import pytest

from sturddle_view.events import EVT_AI_POSITION_NOTE, EventBus
from sturddle_view.llm import ProviderChunk, ScriptedProvider, ToolRegistry
from sturddle_view.play.ai_analysis import (
    AIAnalysisCoordinator,
    _POSITION_CHECK_PREFIX,
    _POSITION_CHECK_REPEAT_LEAD,
)


# Black to move; g6 and c1 are empty (false-claim targets).
_FEN = "r2qr1k1/5ppp/p4n2/1pbP1bB1/8/2Nn1B2/PP1Q1PPP/1N1R1RK1 b - - 3 17"


async def _drain_until_done(queue) -> list:
    events = []
    while True:
        evt = await queue.get()
        events.append(evt)
        if evt.payload.get("done"):
            return events


def _coord(provider, board):
    bus = EventBus()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=ToolRegistry(), board_provider=lambda: board,
    )
    return coord, bus


def _last_user_texts(provider: ScriptedProvider) -> list[str]:
    """User-role message contents from the most recent round's input."""
    msgs = provider.last_call["messages"] if provider.last_call else []
    out = []
    for m in msgs:
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            out.append(m["content"])
    return out


@pytest.mark.asyncio
async def test_clean_prose_emits_no_note_and_no_injection():
    board = chess.Board(_FEN)
    provider = ScriptedProvider(rounds=[[
        ProviderChunk(kind="text", text="Black is slightly better here."),
    ]])
    coord, bus = _coord(provider, board)
    queue = await bus.subscribe()

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    assert not any(e.kind == EVT_AI_POSITION_NOTE for e in events)
    assert provider.stream_calls == 1  # natural exit, no extra round


@pytest.mark.asyncio
async def test_false_claim_emits_note_and_injects_corrective():
    board = chess.Board(_FEN)
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(kind="text", text="The bishop on h6 dominates.")],
        [ProviderChunk(kind="text", text="Corrected: the bishop is on f5.")],
    ])
    coord, bus = _coord(provider, board)
    queue = await bus.subscribe()

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    notes = [e for e in events if e.kind == EVT_AI_POSITION_NOTE]
    assert len(notes) == 1
    # The note carries the exact prose span (with article) for the client to
    # strike -- prose was "The bishop on h6 dominates."
    assert notes[0].payload["surfaces"] == ["The bishop on h6"]
    # The corrective is injected, forcing a second round.
    assert provider.stream_calls == 2
    injected = _last_user_texts(provider)
    assert any(t.startswith(_POSITION_CHECK_PREFIX) for t in injected)
    # Fact-anchored: the corrective states the square is empty.
    assert any("h6 is empty" in t for t in injected)


@pytest.mark.asyncio
async def test_repeat_of_reworded_claim_escalates():
    # Round 1: "white bishop on c6"; round 2 re-asserts it as "bishop on c6".
    # The repeat key is the square, so the reworded repeat still escalates.
    # c6 is empty and unreachable by either bishop, so both rounds flag.
    board = chess.Board(_FEN)
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(kind="text", text="White bishop on c6 is strong.")],
        [ProviderChunk(kind="text", text="The bishop on c6 stays.")],
        [ProviderChunk(kind="text", text="Fine, the bishop is on f5.")],
    ])
    coord, bus = _coord(provider, board)
    queue = await bus.subscribe()

    await coord.run(game_id="g")
    await _drain_until_done(queue)

    # The third round's input carries the escalated (repeat) corrective.
    injected = _last_user_texts(provider)
    assert any(_POSITION_CHECK_REPEAT_LEAD in t for t in injected)


@pytest.mark.asyncio
async def test_prose_move_to_unreachable_square_flagged():
    # "bishop to a1" is an impossible move; the note carries the exact prose
    # span (including the verb) for the client to strike.
    board = chess.Board(_FEN)
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(kind="text", text="The bishop goes to a1 winning.")],
        [ProviderChunk(kind="text", text="Corrected.")],
    ])
    coord, bus = _coord(provider, board)
    queue = await bus.subscribe()

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    notes = [e for e in events if e.kind == EVT_AI_POSITION_NOTE]
    assert len(notes) == 1
    assert notes[0].payload["surfaces"] == ["The bishop goes to a1"]
    assert provider.stream_calls == 2


@pytest.mark.asyncio
async def test_no_board_provider_is_a_noop():
    provider = ScriptedProvider(rounds=[[
        ProviderChunk(kind="text", text="The bishop on g6 dominates."),
    ]])
    bus = EventBus()
    coord = AIAnalysisCoordinator(bus, provider, registry=ToolRegistry())
    queue = await bus.subscribe()

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    assert not any(e.kind == EVT_AI_POSITION_NOTE for e in events)
    assert provider.stream_calls == 1
