"""Tool progress rows: a tool's internal steps shown under its panel row.

recommend_move (and the refute check) run two searches: the engine's own best
as a baseline, then the candidate. The board arrow and Search Lines follow
each search live, so the panel must name the one running -- otherwise the
row says "Considering Qf8" while the arrow shows the baseline's move.

Pinned:
- the dominance searches report "engine best" then "search <move>", each
  before its search starts, and finish each with its result (move, eval,
  depth) once it lands,
- the coordinator surfaces a report as an ai_tool_call nested under the
  dispatching tool's row (parent_tool_use_id), after that row, and its
  finish as that step's ai_tool_call_complete,
- reports outside a dispatch go nowhere.
"""
from __future__ import annotations

import chess
import pytest

from sturddle_view.events import EVT_AI_TOOL_CALL, EVT_AI_TOOL_CALL_COMPLETE, EventBus
from sturddle_view.llm import ProviderChunk, ScriptedProvider, ToolRegistry, ToolSpec
from sturddle_view.llm.cancel import CancelToken
from sturddle_view.llm.tool_progress import report_progress, reporting_progress
from sturddle_view.play import tools_engine
from sturddle_view.play.ai_analysis import AIAnalysisCoordinator
from sturddle_view.play.tools_engine import SearchCache

_SEARCH_DEPTH = 6
_FREE_BEST = chess.Move.from_uci("d2d4")
_FREE_CP = 30
_CAND_CP = 20


class _FakeSearch:
    """Stand-in for `_run_one_search`; records which search ran (free or
    restricted) into the shared `log`, alongside progress reports. The free
    search's best is d4; a restricted one plays its only root move."""

    def __init__(self, log: list) -> None:
        self._log = log

    async def __call__(
        self, engine_launcher, board, limit, *,
        bus, game_id, cancel_token, root_moves=None, settings_provider=None,
    ):
        self._log.append(("searched", [m.uci() for m in root_moves or []]))
        best, cp = (root_moves[0], _CAND_CP) if root_moves else (_FREE_BEST, _FREE_CP)
        score = chess.engine.PovScore(chess.engine.Cp(cp), chess.WHITE)
        return {"depth": limit.depth, "score": score, "pv": [best]}, False


@pytest.mark.asyncio
async def test_dominance_searches_report_each_search_before_it_runs(monkeypatch):
    log: list = []
    monkeypatch.setattr(tools_engine, "_run_one_search", _FakeSearch(log))

    async def progress(name, input_):
        log.append((name, input_))

        async def finish(output):
            log.append(("done", name, output))

        return finish

    board = chess.Board()
    with reporting_progress(progress):
        await tools_engine._dominance_searches(
            SearchCache(), lambda: None, board, chess.Move.from_uci("e2e4"),
            chess.engine.Limit(depth=_SEARCH_DEPTH),
            bus=EventBus(), game_id="g", cancel_token=CancelToken(),
            settings_provider=None,
        )

    best, move = tools_engine.PROGRESS_ENGINE_BEST, tools_engine.PROGRESS_SEARCH_MOVE
    assert log == [
        (best, {"depth": _SEARCH_DEPTH}),
        ("searched", []),
        ("done", best, {"san": "d4", "score_text": "+0.30", "depth": _SEARCH_DEPTH}),
        (move, {"move": "e4", "depth": _SEARCH_DEPTH}),
        ("searched", ["e2e4"]),
        ("done", move, {"san": "e4", "score_text": "+0.20", "depth": _SEARCH_DEPTH}),
    ]


@pytest.mark.asyncio
async def test_candidate_that_is_the_engine_best_skips_its_search(monkeypatch):
    # The baseline already searched the candidate's own line: a second,
    # restricted search (and its row) would repeat it. The baseline result
    # stands in for the candidate's.
    log: list = []
    monkeypatch.setattr(tools_engine, "_run_one_search", _FakeSearch(log))

    async def progress(name, input_):
        log.append((name, input_))

        async def finish(output):
            log.append(("done", name, output))

        return finish

    with reporting_progress(progress):
        best_info, cand_info = await tools_engine._dominance_searches(
            SearchCache(), lambda: None, chess.Board(), _FREE_BEST,
            chess.engine.Limit(depth=_SEARCH_DEPTH),
            bus=EventBus(), game_id="g", cancel_token=CancelToken(),
            settings_provider=None,
        )

    assert [entry[0] for entry in log] == [tools_engine.PROGRESS_ENGINE_BEST, "searched", "done"]
    assert cand_info is best_info


@pytest.mark.asyncio
async def test_report_outside_a_dispatch_is_a_no_op():
    finish = await report_progress("anything", {})  # must not raise
    await finish({"san": "e4"})


@pytest.mark.asyncio
async def test_coordinator_nests_reports_under_the_dispatching_row():
    async def stepping_tool(_input, *, cancel_token):
        finish = await report_progress("step_one", {"move": "Qf8"})
        await finish({"san": "Qf8", "score_text": "+0.10"})
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
    # The finished step's result lands on its own row (the OUT detail); the
    # unfinished one gets none.
    completes = {
        e.payload["tool_use_id"]: e.payload["output"]
        for e in events if e.kind == EVT_AI_TOOL_CALL_COMPLETE
    }
    assert completes.get(calls[1]["tool_use_id"]) == {"san": "Qf8", "score_text": "+0.10"}
    assert calls[2]["tool_use_id"] not in completes
