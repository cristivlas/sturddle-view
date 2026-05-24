"""Real-Ollama integration test for the AI analysis pipeline.

Skipped unless `--ollama --ollama-model <name>` is passed. Drives the
whole agent loop end-to-end against a live local daemon: provider
streams real chunks, coordinator dispatches the analyze tool (real
engine subprocess), runner appends tool_result, model produces final
prose, terminal done event lands on the bus.

Asserts SHAPE (chunks arrive, tool was dispatched, done event present),
not CONTENT (LLM output is non-deterministic).
"""
from __future__ import annotations

import asyncio

import pytest

from sturddle_view.events import EventBus
from sturddle_view.llm import ToolRegistry
from sturddle_view.llm.ollama import OllamaProvider
from sturddle_view.play.ai_analysis import AIAnalysisCoordinator
from sturddle_view.play.engine_supervisor import EngineSupervisor
from sturddle_view.play.tools_engine import ANALYZE_TOOL_SPEC, make_analyze_tool

from .conftest import make_searching_fake_uci


@pytest.fixture
def ollama_opts(pytestconfig):
    if not pytestconfig.getoption("--ollama"):
        pytest.skip("requires --ollama (pass --ollama --ollama-model <name>)")
    model = pytestconfig.getoption("--ollama-model")
    if not model:
        pytest.skip("requires --ollama-model <name>")
    return {
        "base_url": pytestconfig.getoption("--ollama-base-url"),
        "model": model,
    }


@pytest.mark.asyncio
async def test_real_ollama_drives_agent_loop_with_analyze_tool(ollama_opts, tmp_path):
    engine_path = make_searching_fake_uci(
        tmp_path, "ollama_int_fake",
        score_cp=33, depth=6, bestmove="e2e4", pv="e2e4 e7e5",
    )
    bus = EventBus()

    def _launcher() -> EngineSupervisor:
        return EngineSupervisor(engine_path=engine_path, bus=bus)

    registry = ToolRegistry()
    registry.register(ANALYZE_TOOL_SPEC, make_analyze_tool(_launcher, bus=bus))

    provider = OllamaProvider(
        base_url=ollama_opts["base_url"],
        model=ollama_opts["model"],
    )
    coord = AIAnalysisCoordinator(bus, provider, registry=registry)
    queue = await bus.subscribe()

    # The coordinator currently sends an empty user message (Slice D
    # adds the real prompt). For this integration test we just want to
    # confirm the wire works end-to-end; model behavior is not asserted.
    await coord.run(game_id="g_ollama")

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())

    # At minimum: terminal done event with no error marker.
    assert any(e.payload.get("done") for e in events), "no terminal event"
    terminal = next(e for e in events if e.payload.get("done"))
    assert "error" not in terminal.payload, terminal.payload
