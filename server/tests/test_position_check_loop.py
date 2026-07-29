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
    _PositionCheck,
    _POSITION_CHECK_PREFIX,
    _POSITION_CHECK_REPEAT_LEAD,
)


# These tests lock pure regex-loop behavior (note emission, corrective
# injection, round counts). The semantic judge is a separate layer with its
# own suite (test_position_judge); disable it here so its extra stream() call
# doesn't perturb the scripted-provider round budget or the assertions.
@pytest.fixture(autouse=True)
def _no_semantic_check(monkeypatch):
    monkeypatch.setattr(
        "sturddle_view.play.ai_analysis.SEMANTIC_CHECK_ENABLED", False,
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


# White to move; both bishops (d3, e6) are light-squared, so a "dark-squared
# bishop" reference matches nothing -- the reported hallucination.
_BISHOP_FEN = "2n1rk2/p1R2p2/2NRb1p1/1P5p/4P2P/3B1PP1/5K2/2r5 w - - 3 41"


def test_board_labels_exclude_invariant_bishop_flags():
    # A square-bound bishop-color flag is a geometric invariant (d6 is dark
    # in every position), so the semantic judge must never see -- and thus
    # never clear -- it. Bare bishop labels stay judgeable.
    pc = _PositionCheck(
        chess.Board(), [], [], [],
        bishop_triples=[
            ("the light-squared bishop", "light-squared bishop on d6",
             "d6 is dark-squared"),
            ("the dark-squared bishop", "dark-squared bishop",
             "no dark-squared bishop on the board"),
        ],
    )
    assert pc.board_labels == ["dark-squared bishop"]


@pytest.mark.asyncio
async def test_false_bishop_color_emits_note_and_injects_corrective():
    board = chess.Board(_BISHOP_FEN)
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(kind="text", text="Rd4 hits the dark-squared bishop.")],
        [ProviderChunk(kind="text", text="Corrected: the bishop on e6 is light-squared.")],
    ])
    coord, bus = _coord(provider, board)
    queue = await bus.subscribe()

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    notes = [e for e in events if e.kind == EVT_AI_POSITION_NOTE]
    assert len(notes) == 1
    # Surface includes the article (like piece claims) so the client strikes
    # "the dark-squared bishop" whole, not a dangling "the".
    assert notes[0].payload["surfaces"] == ["the dark-squared bishop"]
    assert provider.stream_calls == 2
    injected = _last_user_texts(provider)
    assert any(t.startswith(_POSITION_CHECK_PREFIX) for t in injected)
    # Fact-anchored: bare ref (no side named) lists the real bishops, both
    # light-squared, so the model sees there is no dark-squared bishop at all.
    assert any(
        "no dark-squared bishop on the board" in t
        and "d3 is light-squared" in t and "e6 is light-squared" in t
        for t in injected
    )


def test_tool_mention_after_future_line_still_caught():
    # A future move number truncates the board view, but a tool mention past it
    # is a style violation everywhere -- scanned on the full prose.
    board = chess.Board()  # move 1, so "2.Nf3" is a future line
    coord, _bus = _coord(None, board)
    pc = coord._position_check([
        ProviderChunk(kind="text", text="Then 2.Nf3 develops. The tool agrees."),
    ])
    assert pc.tool_mentions == ["the tool"]
    assert pc.hit


def test_tool_mention_produces_corrective_clause():
    # A caught tool reference asks the model to remove it and rewrite -- no
    # strike, no "isn't legal" wording.
    pc = _PositionCheck(
        chess.Board(_FEN),
        move_pairs=[],
        claim_triples=[],
        line_pairs=[],
        tool_mentions=["the tool"],
    )
    msg = AIAnalysisCoordinator._position_check_message(pc, repeat=False)
    assert '"the tool"' in msg
    assert "never name the tools or engine" in msg
    assert "isn't legal" not in msg


def test_move_named_as_line_and_token_appears_once_in_corrective():
    # A move flagged both as a broken line and standalone in prose must not
    # repeat its fact in the corrective sent to the model.
    pc = _PositionCheck(
        chess.Board(_FEN),
        move_pairs=[("Bb5", "Bb5")],
        claim_triples=[],
        line_pairs=[("Bb5", "Bb5")],
    )
    msg = AIAnalysisCoordinator._position_check_message(pc, repeat=False)
    assert msg.count("Bb5 isn't legal") == 1


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
