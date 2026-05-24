"""Slice B Step 3: Agent runner loop in AIAnalysisCoordinator.

The coordinator now owns the multi-turn loop:
- Calls provider.stream(system, messages, tools) for one round.
- On a tool_use chunk, dispatches via the registry, captures the result,
  appends the assistant + tool_result messages, and loops.
- Stops when the round ends without a tool_use, when the round budget
  is exhausted, or when the user cancels.

Tests use ScriptedProvider as the provider double and a fake registry.
No HTTP, no real engine.
"""
from __future__ import annotations

import asyncio

import pytest

from sturddle_view.events import EventBus
from sturddle_view.llm import (
    ProviderChunk,
    ScriptedProvider,
    ToolRegistry,
    ToolSpec,
)
from sturddle_view.play.ai_analysis import AIAnalysisCoordinator


def _make_registry(handlers: dict) -> ToolRegistry:
    """handlers: name -> async fn(input, *, cancel_token) -> dict."""
    reg = ToolRegistry()
    for name, fn in handlers.items():
        reg.register(
            ToolSpec(name=name, description=name, input_schema={"type": "object"}),
            fn,
        )
    return reg


async def _drain_until_done(queue: asyncio.Queue) -> list:
    events = []
    while True:
        evt = await queue.get()
        events.append(evt)
        if evt.payload.get("done"):
            return events


@pytest.mark.asyncio
async def test_single_round_no_tool_use_completes_normally():
    # Phase 0 behavior must still hold: provider yields text only, no
    # tool_use, runner streams to bus + emits terminal done.
    provider = ScriptedProvider(rounds=[[
        ProviderChunk(kind="text", text="Hello."),
    ]])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider, registry=ToolRegistry())

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    deltas = [e.payload["delta"] for e in events if "delta" in e.payload]
    assert deltas == ["Hello."]
    assert events[-1].payload == {"done": True}
    assert provider.stream_calls == 1


@pytest.mark.asyncio
async def test_tool_use_dispatches_and_feeds_result_into_next_round():
    captured: dict = {}

    async def fake_tool(payload, *, cancel_token):
        captured["input"] = dict(payload)
        captured["token_seen"] = cancel_token is not None
        return {"score_cp": 42}

    reg = _make_registry({"analyze": fake_tool})

    # Round 1: text + tool_use. Round 2: terminal text.
    provider = ScriptedProvider(rounds=[
        [
            ProviderChunk(kind="text", text="Thinking. "),
            ProviderChunk(
                kind="tool_use",
                tool_use_id="tu_1",
                tool_name="analyze",
                tool_input={"fen": "startpos"},
            ),
        ],
        [
            ProviderChunk(kind="text", text="It's +0.42."),
        ],
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider, registry=reg)

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    # Tool got the input + a cancel token.
    assert captured["input"] == {"fen": "startpos"}
    assert captured["token_seen"] is True

    # Two rounds happened.
    assert provider.stream_calls == 2

    # Second round's messages must carry assistant tool_use + user
    # tool_result keyed by the same tool_use_id.
    snap = provider.last_call
    assert snap is not None
    # Expected shape:
    #  [user(initial), assistant([{type:text}, {type:tool_use,...}]), user([{type:tool_result,...}])]
    assert len(snap["messages"]) >= 3
    assistant = snap["messages"][-2]
    tool_result_msg = snap["messages"][-1]
    assert assistant["role"] == "assistant"
    assert any(
        b.get("type") == "tool_use" and b.get("id") == "tu_1"
        for b in assistant["content"]
    )
    assert tool_result_msg["role"] == "user"
    tr_block = tool_result_msg["content"][0]
    assert tr_block["type"] == "tool_result"
    assert tr_block["tool_use_id"] == "tu_1"

    # Prose emitted to the bus spans both rounds.
    deltas = [e.payload["delta"] for e in events if "delta" in e.payload]
    assert deltas == ["Thinking. ", "It's +0.42."]
    assert events[-1].payload == {"done": True}


@pytest.mark.asyncio
async def test_unknown_tool_returns_structured_error_and_loop_continues():
    # Registry has no "missing" tool; runner must feed back a structured
    # error tool_result instead of crashing the turn, so the model can
    # recover or finish gracefully.
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(
            kind="tool_use",
            tool_use_id="tu_x",
            tool_name="missing",
            tool_input={},
        )],
        [ProviderChunk(kind="text", text="ok, giving up.")],
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider, registry=ToolRegistry())

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    assert provider.stream_calls == 2
    snap = provider.last_call
    tr = snap["messages"][-1]["content"][0]
    assert tr["type"] == "tool_result"
    assert tr["tool_use_id"] == "tu_x"
    # The structured error is JSON-serialized into the tool_result content.
    assert "unknown_tool" in tr["content"]
    # Turn still completes with terminal done (no error marker since
    # the runner handled the error gracefully).
    assert events[-1].payload == {"done": True}


@pytest.mark.asyncio
async def test_tool_raising_returns_structured_error_and_loop_continues():
    async def boom(_input, *, cancel_token):
        raise RuntimeError("kaboom")

    reg = _make_registry({"boom": boom})
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(
            kind="tool_use",
            tool_use_id="tu_b",
            tool_name="boom",
            tool_input={},
        )],
        [ProviderChunk(kind="text", text="recovered.")],
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider, registry=reg)

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    assert provider.stream_calls == 2
    tr = provider.last_call["messages"][-1]["content"][0]
    assert "tool_failed" in tr["content"]
    assert "kaboom" in tr["content"]
    assert events[-1].payload == {"done": True}


@pytest.mark.asyncio
async def test_cancel_mid_tool_propagates_and_emits_cancelled_done():
    tool_started = asyncio.Event()

    async def hangs(_input, *, cancel_token):
        tool_started.set()
        # Wait on the token explicitly -- a cooperative tool would
        # short-circuit here. If cancellation never arrives this will
        # block forever, which makes the test fail loudly rather than
        # passing on the wrong code path.
        await cancel_token.wait_cancelled()
        raise asyncio.CancelledError()  # surface as cancellation

    reg = _make_registry({"slow": hangs})
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(
            kind="tool_use",
            tool_use_id="tu_s",
            tool_name="slow",
            tool_input={},
        )],
        # Second round never runs because we cancel mid-tool.
        [ProviderChunk(kind="text", text="unreached")],
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider, registry=reg)

    run_task = asyncio.create_task(coord.run(game_id="g"))
    # Wait until the tool is actually executing before cancelling, so
    # the test exercises the mid-tool path (not the "before any work"
    # path) deterministically without any timer.
    await tool_started.wait()

    await coord.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    # Drain bus: only the terminal cancelled event should be there
    # (the tool_use chunk doesn't produce an ai_info delta).
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert events[-1].kind == "ai_info"
    assert events[-1].payload == {"done": True, "cancelled": True}
    # Second round never happened.
    assert provider.stream_calls == 1


@pytest.mark.asyncio
async def test_round_cap_stops_runaway_loop():
    # Provider keeps asking for a tool forever. The runner must stop
    # at MAX_TOOL_ROUNDS rather than burning the whole script.
    async def echo(_input, *, cancel_token):
        return {"ok": True}

    reg = _make_registry({"loop": echo})

    # Generate enough rounds to exceed the cap. Each round only has a
    # tool_use; no text. The runner should consume exactly the cap and
    # stop.
    from sturddle_view.play.ai_analysis import MAX_TOOL_ROUNDS
    rounds = [
        [ProviderChunk(
            kind="tool_use",
            tool_use_id=f"tu_{i}",
            tool_name="loop",
            tool_input={},
        )]
        for i in range(MAX_TOOL_ROUNDS + 5)
    ]

    provider = ScriptedProvider(rounds=rounds)
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider, registry=reg)

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    assert provider.stream_calls == MAX_TOOL_ROUNDS
    # Terminal event carries `round_cap=True` so the UI can flag that
    # the turn stopped on the guardrail rather than reaching an answer.
    assert events[-1].payload == {"done": True, "round_cap": True}


@pytest.mark.asyncio
async def test_tools_schema_is_passed_to_provider_each_round():
    async def t(p, *, cancel_token):
        return {"ok": True}

    reg = _make_registry({"a": t})

    provider = ScriptedProvider(rounds=[[
        ProviderChunk(kind="text", text="done"),
    ]])
    bus = EventBus()
    await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider, registry=reg)

    await coord.run(game_id="g")

    snap = provider.last_call
    assert snap is not None
    assert snap["tools"] == reg.schemas()
