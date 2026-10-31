"""play_out steps of different ideas share a round (skin-in-the-game item 2).

Any other call still ends its round; a call that can't join stays unsent,
as when every round carried one call. A second step of one idea in the same
round was chosen before its reply, so it is refused undispatched. All of a
round's results go back in one message, tool_results first.
"""
from __future__ import annotations

import json

import chess
import pytest

from sturddle_view.events import EventBus
from sturddle_view.llm import ProviderChunk, ScriptedProvider, ToolRegistry, ToolSpec
from sturddle_view.play.ai_analysis import (
    AIAnalysisCoordinator,
    _ONE_STEP_PER_IDEA_ERROR,
    _tool_results_message,
)


_TURN_CONTEXT = f"Current position (FEN): {chess.STARTING_FEN}\nSide to move: white\n"
_DONE = [ProviderChunk(kind="text", text="1.e4 e5 and 1.d4 d5 both hold.")]


def _call(tool_use_id: str, name: str, **tool_input) -> ProviderChunk:
    return ProviderChunk(
        kind="tool_use", tool_use_id=tool_use_id, tool_name=name, tool_input=tool_input,
    )


def _step(tool_use_id: str, idea: str, move: str) -> ProviderChunk:
    return _call(tool_use_id, "play_out", idea=idea, move=move)


async def _run(rounds) -> tuple[ScriptedProvider, list[str], list]:
    """Run a turn over `rounds`; returns the provider, the tool names
    dispatched in order, and the emitted events."""
    dispatched: list[str] = []

    def tool(name: str):
        async def fn(input_, *, cancel_token):
            dispatched.append(f"{name}:{input_.get('move')}")
            return {"move": input_.get("move")}
        return fn

    reg = ToolRegistry()
    for name in ("play_out", "piece_at", "recommend_move"):
        spec = ToolSpec(name=name, description=name, input_schema={"type": "object"})
        reg.register(spec, tool(name))
    provider = ScriptedProvider(rounds=rounds)
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider, registry=reg, board_provider=lambda: chess.Board())
    await coord.run(
        game_id="g", mode="coach", user_message=_TURN_CONTEXT, max_tool_rounds=len(rounds),
    )
    events = []
    while True:
        evt = await queue.get()
        events.append(evt)
        if evt.payload.get("done"):
            return provider, dispatched, events


def _results_message(provider: ScriptedProvider) -> dict:
    return provider.last_call["messages"][2]


@pytest.mark.asyncio
async def test_steps_of_different_ideas_share_a_round():
    provider, dispatched, events = await _run([
        [_step("a", "e4 idea", "e4"), _step("b", "d4 idea", "d4")],
        _DONE,
    ])
    assert dispatched == ["play_out:e4", "play_out:d4"]
    assert provider.stream_calls == 2
    assistant = provider.last_call["messages"][1]
    assert [b["id"] for b in assistant["content"] if b["type"] == "tool_use"] == ["a", "b"]
    results = _results_message(provider)["content"]
    assert [b["tool_use_id"] for b in results if b["type"] == "tool_result"] == ["a", "b"]
    # Both rows reach the panel, each with its own complete event.
    completes = [e.payload["tool_use_id"] for e in events if e.kind == "ai_tool_call_complete"]
    assert completes == ["a", "b"]


@pytest.mark.asyncio
async def test_second_step_of_one_idea_in_a_round_is_refused():
    provider, dispatched, events = await _run([
        [_step("a", "e4 idea", "e4"), _step("b", "E4  idea", "Nf3")],
        _DONE,
    ])
    assert dispatched == ["play_out:e4"]
    results = {
        b["tool_use_id"]: json.loads(b["content"])
        for b in _results_message(provider)["content"] if b["type"] == "tool_result"
    }
    assert results["b"]["error"] == _ONE_STEP_PER_IDEA_ERROR
    failed = [e.payload["tool_use_id"] for e in events if e.kind == "ai_tool_call_failed"]
    assert failed == ["b"]


@pytest.mark.asyncio
async def test_other_call_after_steps_stays_unsent():
    provider, dispatched, _events = await _run([
        [_step("a", "e4 idea", "e4"), _call("r", "recommend_move", move="e4")],
        _DONE,
    ])
    assert dispatched == ["play_out:e4"]
    assistant = provider.last_call["messages"][1]
    assert [b["id"] for b in assistant["content"] if b["type"] == "tool_use"] == ["a"]


@pytest.mark.asyncio
async def test_other_call_still_ends_its_round():
    provider, dispatched, _events = await _run([
        [_call("p", "piece_at", square="e2"), _step("a", "e4 idea", "e4")],
        _DONE,
    ])
    assert dispatched == ["piece_at:None"]
    assistant = provider.last_call["messages"][1]
    assert [b["id"] for b in assistant["content"] if b["type"] == "tool_use"] == ["p"]


def test_results_message_puts_every_tool_result_before_cards():
    msg = _tool_results_message([
        ("a", {"x": 1}, "card A"), ("b", "raw", None), ("c", {}, "card C"),
    ])
    assert msg["role"] == "user"
    assert [b["type"] for b in msg["content"]] == [
        "tool_result", "tool_result", "tool_result", "text", "text",
    ]
    assert msg["content"][1]["content"] == "raw"
    assert [b["text"] for b in msg["content"][3:]] == ["card A", "card C"]


def test_narrator_prose_check_exempts_moves_of_played_out_lines():
    # Bc4 is illegal on the live board, but the prose narrates a line the
    # server replayed: its moves pass the board check (the prompt promises
    # that), while a move outside every replayed line is still flagged.
    coord = AIAnalysisCoordinator(
        EventBus(), ScriptedProvider([]), board_provider=lambda: chess.Board(),
    )
    prose = [ProviderChunk(
        kind="text", text="After 1.e4 e5 the knight comes to f3 and Bc4 hits f7.",
    )]
    line = ["1.e4 e5 2.Nf3 Nc6 3.Bc4"]
    assert coord._position_check(prose).hit
    assert not coord._position_check(prose, line).hit
    stray = [ProviderChunk(kind="text", text="Bb5 pins the knight.")]
    assert coord._position_check(stray, line).hit


def test_exemption_keeps_the_cut_at_a_future_line():
    # Prose past "2." is about the line's position, not the live board:
    # blanking the line's moves must not lift that cut ("the bishop on c4"
    # is false on the live board, true after 3.Bc4).
    coord = AIAnalysisCoordinator(
        EventBus(), ScriptedProvider([]), board_provider=lambda: chess.Board(),
    )
    prose = [ProviderChunk(
        kind="text", text="1.e4 e5 2.Nf3 Nc6 3.Bc4, and the bishop on c4 eyes f7.",
    )]
    assert not coord._position_check(prose, ["1.e4 e5 2.Nf3 Nc6 3.Bc4"]).hit
