"""Out-of-rounds fallback in the narrator loop (skin-in-the-game item 3b).

FALLBACK_ROUNDS_LEFT rounds before the cap, a narrator with no accepted
pick but a played-out move is told to submit now; from then on the real
recommend_move (wired to the coordinator's turn_fallback) takes the best
played-out move unconditionally and the red-team hold is skipped. Nothing
played out: no nudge, the turn ends without a recommendation.
"""
from __future__ import annotations

import asyncio

import chess
import pytest

from sturddle_view.events import EventBus
from sturddle_view.llm import ProviderChunk, ScriptedProvider, ToolRegistry, ToolSpec
from sturddle_view.play import tools_engine
from sturddle_view.play.ai_analysis import (
    FALLBACK_ROUNDS_LEFT,
    AIAnalysisCoordinator,
    _FALLBACK_NUDGE,
)
from sturddle_view.play.tools_engine import (
    RECOMMEND_MOVE_TOOL_SPEC,
    PlayOutRecord,
    make_recommend_move_tool,
)

from .scripted_search import epd_after, scripted_search


_TURN_CONTEXT = f"Current position (FEN): {chess.STARTING_FEN}\nSide to move: white\n"
# e4 is played out and dominated (the engine's d4 is 280cp better): only
# the fallback accepts it.
_SCRIPT = {
    (epd_after(), None): (300, ["d2d4"]),
    (epd_after(), "e2e4"): (20, ["e2e4"]),
}


class _RecordingProvider(ScriptedProvider):
    """ScriptedProvider that records each round's messages and forcing."""

    def __init__(self, rounds):
        super().__init__(rounds)
        self.calls: list[dict] = []

    async def stream(self, system, messages, tools=None, *, force_tool_call=False, **kw):
        self.calls.append({"messages": [dict(m) for m in messages], "force": force_tool_call})
        async for chunk in super().stream(system, messages, tools, **kw):
            yield chunk


def _call(tool_use_id: str, name: str, **tool_input) -> list[ProviderChunk]:
    return [ProviderChunk(
        kind="tool_use", tool_use_id=tool_use_id, tool_name=name, tool_input=tool_input,
    )]


async def _no_tool(_input, *, cancel_token):
    return {"ok": True}


def _coordinator(monkeypatch, provider, record: PlayOutRecord, *, played: list[str]):
    """Narrator registry: the real recommend_move (scripted searches), a
    play_out stub noting `played` first moves in `record`, and a delegate
    stub so the red-team hold is armed."""
    monkeypatch.setattr(tools_engine, "_run_one_search", scripted_search(_SCRIPT, []))
    board = chess.Board()
    reg = ToolRegistry()
    bus = EventBus()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg, board_provider=lambda: board, play_out_record=record,
    )
    reg.register(RECOMMEND_MOVE_TOOL_SPEC, make_recommend_move_tool(
        lambda: None, bus=bus, board_provider=lambda: board,
        play_out_record=record, fallback_provider=coord.turn_fallback,
    ))

    async def play_out(input_, *, cancel_token):
        move = board.parse_san(input_["move"])
        record.note_first_move(move, 20)
        played.append(input_["move"])
        return {"idea": input_["idea"], "move": input_["move"], "reply": "e5"}

    for name, fn in (("play_out", play_out), ("delegate", _no_tool)):
        reg.register(ToolSpec(name=name, description=name, input_schema={"type": "object"}), fn)
    return coord, bus


async def _drain(queue: asyncio.Queue) -> list:
    events = []
    while True:
        evt = await queue.get()
        events.append(evt)
        if evt.payload.get("done"):
            return events


@pytest.mark.asyncio
async def test_fallback_takes_the_played_out_move_without_a_hold(monkeypatch):
    rounds = 5
    nudge_round = rounds - FALLBACK_ROUNDS_LEFT
    provider = _RecordingProvider(rounds=[
        _call("p0", "play_out", idea="e4, aim d4", move="e4"),
        _call("r0", "recommend_move", move="e4"),           # dominated -> rejected
        _call("r1", "recommend_move", move="Nf3"),          # not played out -> rejected
        _call("r2", "recommend_move", move="Nf3"),          # fallback: replaced by e4
        [ProviderChunk(kind="text", text="1.e4 e5 holds the center; e4.")],
    ])
    record = PlayOutRecord()
    coord, bus = _coordinator(monkeypatch, provider, record, played=[])
    queue = await bus.subscribe()

    await coord.run(
        game_id="g", mode="coach", user_message=_TURN_CONTEXT, max_tool_rounds=rounds,
    )
    events = await _drain(queue)

    nudged = provider.calls[nudge_round]
    assert nudged["messages"][-1]["content"] == _FALLBACK_NUDGE
    assert nudged["force"] is True
    assert all(_FALLBACK_NUDGE not in str(c["messages"]) for c in provider.calls[:nudge_round])
    outputs = {
        e.payload["tool_use_id"]: e.payload["output"]
        for e in events if e.kind == "ai_tool_call_complete"
    }
    assert outputs["r0"]["error"] == "recommendation_rejected"
    accepted = outputs["r2"]
    assert accepted["ok"] is True and accepted["uci"] == "e2e4"
    assert accepted["weaker_than_best"] is True
    assert "Nf3 was not played out" in accepted["note"]
    # No red-team hold in fallback, though delegate is registered.
    assert not [e for e in events if e.payload.get("error") == "red_team_first"]
    done = events[-1].payload
    assert not done.get("no_recommendation") and not done.get("round_cap")
    assert coord.turn_fallback() is False


@pytest.mark.asyncio
async def test_no_fallback_nudge_when_nothing_was_played_out(monkeypatch):
    rounds = 3
    provider = _RecordingProvider(rounds=[
        _call("r0", "recommend_move", move="e4"),           # not played out
        _call("r1", "recommend_move", move="e4"),
        _call("r2", "recommend_move", move="e4"),
    ])
    coord, bus = _coordinator(monkeypatch, provider, PlayOutRecord(), played=[])
    queue = await bus.subscribe()

    await coord.run(
        game_id="g", mode="coach", user_message=_TURN_CONTEXT, max_tool_rounds=rounds,
    )
    events = await _drain(queue)

    assert all(_FALLBACK_NUDGE not in str(c["messages"]) for c in provider.calls)
    assert events[-1].payload.get("round_cap") is True
    assert not [e for e in events if e.kind == "ai_recommendation"]


@pytest.mark.asyncio
async def test_play_out_record_is_cleared_at_turn_start(monkeypatch):
    record = PlayOutRecord()
    record.note_first_move(chess.Move.from_uci("e2e4"), 20)
    provider = _RecordingProvider(rounds=[[ProviderChunk(kind="text", text="Quiet.")]] * 2)
    coord, bus = _coordinator(monkeypatch, provider, record, played=[])
    queue = await bus.subscribe()

    await coord.run(game_id="g", mode="coach", user_message=_TURN_CONTEXT, max_tool_rounds=2)
    await _drain(queue)

    assert not record.any_played_out()


@pytest.mark.asyncio
async def test_fallback_fires_on_a_later_round_once_something_is_played_out(monkeypatch):
    # Nothing is played out when FALLBACK_ROUNDS_LEFT rounds remain; the
    # first later round with a played-out move still falls back.
    rounds = FALLBACK_ROUNDS_LEFT + 3
    late = rounds - FALLBACK_ROUNDS_LEFT + 1
    # Alternating unplayed moves (no dedup hit) until the play-out round.
    unplayed = [
        _call(f"r{i}", "recommend_move", move=("Nf3", "d4")[i % 2]) for i in range(late - 1)
    ]
    script = unplayed + [
        _call("p0", "play_out", idea="e4, aim d4", move="e4"),
        _call("rf", "recommend_move", move="Nf3"),          # fallback: e4
    ]
    provider = _RecordingProvider(rounds=script)
    coord, bus = _coordinator(monkeypatch, provider, PlayOutRecord(), played=[])
    queue = await bus.subscribe()

    await coord.run(
        game_id="g", mode="coach", user_message=_TURN_CONTEXT, max_tool_rounds=rounds,
    )
    events = await _drain(queue)

    assert provider.calls[late]["messages"][-1]["content"] == _FALLBACK_NUDGE
    assert all(_FALLBACK_NUDGE not in str(c["messages"]) for c in provider.calls[:late])
    accepted = next(
        e.payload["output"] for e in events
        if e.kind == "ai_tool_call_complete" and e.payload["tool_use_id"] == "rf"
    )
    assert accepted["ok"] is True and accepted["uci"] == "e2e4"
