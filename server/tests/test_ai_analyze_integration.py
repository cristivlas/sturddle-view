"""Integration test for the coordinator -> registry -> analyze chain.

Scripted provider asks for the `analyze` tool; the registered tool
spawns a real fake-engine subprocess via spawn_throwaway, returns a
structured eval, runner appends the tool_result, second round finishes.
End-to-end through every Slice A/B/C piece with no production-only
hacks.
"""
from __future__ import annotations


import pytest

from sturddle_view.events import EventBus
from sturddle_view.llm import (
    ProviderChunk,
    ScriptedProvider,
    ToolRegistry,
    ToolSpec,
)
from sturddle_view.play.ai_analysis import AIAnalysisCoordinator
from sturddle_view.play.engine_supervisor import EngineSupervisor
from sturddle_view.play.tools_engine import make_analyze_tool

from .conftest import make_searching_fake_uci


@pytest.mark.asyncio
async def test_coordinator_dispatches_analyze_to_real_subprocess(tmp_path):
    engine_path = make_searching_fake_uci(
        tmp_path, "integ_fake",
        score_cp=66, depth=4, bestmove="d2d4", pv="d2d4 d7d5",
    )
    bus = EventBus()

    def _launcher() -> EngineSupervisor:
        return EngineSupervisor(engine_path=engine_path, bus=bus)

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="analyze",
            description="search a position",
            input_schema={"type": "object"},
        ),
        make_analyze_tool(_launcher, bus=bus),
    )

    provider = ScriptedProvider(rounds=[
        # Round 1: agent asks for analyze.
        [ProviderChunk(
            kind="tool_use",
            tool_use_id="tu_1",
            tool_name="analyze",
            tool_input={"fen": "startpos", "depth": 4},
        )],
        # Round 2: agent narrates the eval and stops.
        [ProviderChunk(kind="text", text="d4 looks fine (+0.66).")],
    ])
    coord = AIAnalysisCoordinator(bus, provider, registry=registry)
    queue = await bus.subscribe()

    await coord.run(game_id="g")

    # Drain bus -- delta + terminal done.
    events = []
    while True:
        evt = await queue.get()
        events.append(evt)
        if evt.payload.get("done"):
            break
    deltas = [e.payload["delta"] for e in events if "delta" in e.payload]
    assert deltas == ["d4 looks fine (+0.66)."]
    assert events[-1].payload.get("done") is True

    # Second round must carry the real tool_result the analyze tool
    # returned -- score_cp=66 from the fake engine.
    snap = provider.last_call
    tr = snap["messages"][-1]["content"][0]
    assert tr["type"] == "tool_result"
    assert tr["tool_use_id"] == "tu_1"
    assert '"score_cp": 66' in tr["content"]
    assert '"depth": 4' in tr["content"]
    assert '"bestmove": "d2d4"' in tr["content"]
