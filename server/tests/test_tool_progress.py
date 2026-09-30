"""Tool progress rows: a tool's internal steps shown under its panel row.

recommend_move (and the refute check) run two searches: the engine's own best
as a baseline, then the candidate. The board arrow and Search Lines follow
each search live, so the panel must name the one running -- otherwise the
row says "Considering Qf8" while the arrow shows the baseline's move.

Pinned:
- the dominance searches report "engine best" then "search <move>", each
  before its search starts,
- the coordinator surfaces a report as an ai_tool_call nested under the
  dispatching tool's row (parent_tool_use_id), after that row,
- reports outside a dispatch go nowhere.
"""
from __future__ import annotations

import chess
import pytest

from sturddle_view.events import EVT_AI_TOOL_CALL, EventBus
from sturddle_view.llm import ProviderChunk, ScriptedProvider, ToolRegistry, ToolSpec
from sturddle_view.llm.cancel import CancelToken
from sturddle_view.llm.tool_progress import report_progress, reporting_progress
from sturddle_view.play import tools_engine
from sturddle_view.play.ai_analysis import AIAnalysisCoordinator
from sturddle_view.play.tools_engine import SearchCache

_SEARCH_DEPTH = 6


class _FakeSearch:
    """Stand-in for `_run_one_search`; records which search ran (free or
    restricted) into the shared `log`, alongside progress reports."""

    def __init__(self, log: list) -> None:
        self._log = log

    async def __call__(
        self, engine_launcher, board, limit, *,
        bus, game_id, cancel_token, root_moves=None, settings_provider=None,
    ):
        self._log.append(("searched", [m.uci() for m in root_moves or []]))
        return {"depth": limit.depth, "score": None, "pv": []}, False


@pytest.mark.asyncio
async def test_dominance_searches_report_each_search_before_it_runs(monkeypatch):
    log: list = []
    monkeypatch.setattr(tools_engine, "_run_one_search", _FakeSearch(log))

    async def progress(name, input_):
        log.append((name, input_))

    board = chess.Board()
    with reporting_progress(progress):
        await tools_engine._dominance_searches(
            SearchCache(), lambda: None, board, chess.Move.from_uci("e2e4"),
            chess.engine.Limit(depth=_SEARCH_DEPTH),
            bus=EventBus(), game_id="g", cancel_token=CancelToken(),
            settings_provider=None,
        )

    assert log == [
        (tools_engine.PROGRESS_ENGINE_BEST, {"depth": _SEARCH_DEPTH}),
        ("searched", []),
        (tools_engine.PROGRESS_SEARCH_MOVE, {"move": "e4", "depth": _SEARCH_DEPTH}),
        ("searched", ["e2e4"]),
    ]


@pytest.mark.asyncio
async def test_report_outside_a_dispatch_is_a_no_op():
    await report_progress("anything", {})  # must not raise


@pytest.mark.asyncio
async def test_coordinator_nests_reports_under_the_dispatching_row():
    async def stepping_tool(_input, *, cancel_token):
        await report_progress("step_one", {"move": "Qf8"})
        await report_progress("step_two", {})
        return {"ok": True}

    reg = ToolRegistry()
    reg.register(
        ToolSpec(name="stepper", description="d", input_schema={"type": "object"}),
        stepping_tool,
    )
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(kind="tool_use", tool_use_id="t1", tool_name="stepper", tool_input={})],
        [ProviderChunk(kind="text", text="Done.")],
    ])
    bus = EventBus()
    coord = AIAnalysisCoordinator(bus, provider, registry=reg, board_provider=lambda: None)
    queue = await bus.subscribe()

    await coord.run(game_id="g")
    events = []
    while True:
        evt = await queue.get()
        events.append(evt)
        if evt.payload.get("done"):
            break

    calls = [e.payload for e in events if e.kind == EVT_AI_TOOL_CALL]
    assert [(c["name"], c.get("parent_tool_use_id")) for c in calls] == [
        ("stepper", None),
        ("step_one", "t1"),
        ("step_two", "t1"),
    ]
    assert calls[1]["input"] == {"move": "Qf8"}
    # Distinct ids so the client can index each row.
    assert len({c["tool_use_id"] for c in calls}) == 3
