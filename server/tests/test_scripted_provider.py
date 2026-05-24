"""Slice A: ScriptedProvider contract tests.

ScriptedProvider is the mock boundary for Phase 1+ -- it lets the agent
runner be tested end-to-end without any network. Each `stream()` call
consumes one pre-scripted round; the provider snapshots its invocation
args so tests can assert the runner is feeding `messages` correctly.

These tests pin the provider contract itself (the runner is Slice B).
"""
from __future__ import annotations

import pytest

from sturddle_view.llm import ProviderChunk, ScriptedProvider


@pytest.mark.asyncio
async def test_single_round_text_only_chunks_in_order():
    chunks = [
        ProviderChunk(kind="text", text="Hello "),
        ProviderChunk(kind="text", text="world."),
    ]
    provider = ScriptedProvider(rounds=[chunks])

    got = []
    async for chunk in provider.stream(
        system="sys",
        messages=[{"role": "user", "content": "hi"}],
    ):
        got.append(chunk)

    assert got == chunks
    assert provider.stream_calls == 1


@pytest.mark.asyncio
async def test_round_terminating_in_tool_use_yields_tool_chunk():
    # Mirrors the wire shape: a few text chunks then a tool_use chunk
    # carrying id/name/input. The runner (Slice B) is what dispatches;
    # the provider's job is just to yield the chunk faithfully.
    tool_use = ProviderChunk(
        kind="tool_use",
        tool_use_id="tu_1",
        tool_name="analyze",
        tool_input={"fen": "startpos", "depth": 12},
    )
    chunks = [
        ProviderChunk(kind="text", text="Let me check. "),
        tool_use,
    ]
    provider = ScriptedProvider(rounds=[chunks])

    got = []
    async for chunk in provider.stream(system="", messages=[]):
        got.append(chunk)

    assert got[-1] is tool_use
    assert got[-1].kind == "tool_use"
    assert got[-1].tool_name == "analyze"
    assert got[-1].tool_input == {"fen": "startpos", "depth": 12}


@pytest.mark.asyncio
async def test_multi_round_consumes_one_round_per_call_and_snapshots_input():
    # Round 1: tool_use. Round 2: terminal text. Runner would dispatch
    # the tool and call stream() a second time with messages extended by
    # the assistant turn + tool_result. We simulate that here inline and
    # assert the provider sees the accumulated messages.
    tool_use = ProviderChunk(kind="tool_use", tool_use_id="tu_a", tool_name="t", tool_input={})
    final_text = ProviderChunk(kind="text", text="done.")
    provider = ScriptedProvider(rounds=[[tool_use], [final_text]])

    messages: list[dict] = [{"role": "user", "content": "go"}]

    # Round 1
    got1 = [c async for c in provider.stream(system="S", messages=messages)]
    assert got1 == [tool_use]
    assert provider.stream_calls == 1
    snap1 = provider.last_call
    assert snap1 == {
        "system": "S",
        "messages": [{"role": "user", "content": "go"}],
        "tools": None,
    }

    # Runner appends assistant turn + tool_result before next call
    messages.append({"role": "assistant", "content": [{"type": "tool_use", "id": "tu_a"}]})
    messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_a", "content": "ok"}]})

    # Round 2
    got2 = [c async for c in provider.stream(system="S", messages=messages, tools=[{"name": "t"}])]
    assert got2 == [final_text]
    assert provider.stream_calls == 2
    snap2 = provider.last_call
    assert snap2["system"] == "S"
    assert snap2["tools"] == [{"name": "t"}]
    # Messages snapshot must reflect the full accumulated transcript.
    assert len(snap2["messages"]) == 3
    assert snap2["messages"][1]["role"] == "assistant"
    assert snap2["messages"][2]["role"] == "user"


@pytest.mark.asyncio
async def test_snapshot_is_immune_to_later_message_mutation():
    # The runner reuses the same `messages` list across rounds (append
    # in place). The provider's `last_call.messages` snapshot must be a
    # deep copy so tests can assert per-round state without race.
    provider = ScriptedProvider(rounds=[[ProviderChunk(kind="text", text="x")]])
    messages = [{"role": "user", "content": "first"}]

    async for _ in provider.stream(system="", messages=messages):
        pass

    # Mutate after the call returns.
    messages.append({"role": "user", "content": "second"})
    messages[0]["content"] = "MUTATED"

    snap = provider.last_call
    assert snap is not None
    assert len(snap["messages"]) == 1
    assert snap["messages"][0]["content"] == "first"


@pytest.mark.asyncio
async def test_exhausted_provider_raises():
    provider = ScriptedProvider(rounds=[[ProviderChunk(kind="text", text="only")]])
    async for _ in provider.stream(system="", messages=[]):
        pass

    with pytest.raises(RuntimeError, match="exhausted"):
        async for _ in provider.stream(system="", messages=[]):
            pass
