"""report_line grounds the narrator's prose against the validators.

The reliability win of the structured-line channel: when the model reports
a continuation via `report_line` before narrating it, the coordinator
registers every position the line traverses as examined, so the prose
validators treat the line's moves as legitimate instead of flagging them as
illegal on the live board.

Driven with ScriptedProvider so the loop runs deterministically -- no LLM,
no engine. The narrator registry's report_line tool is no-engine (pure
legality replay), so the whole flow is in-process.
"""
from __future__ import annotations

import asyncio

import chess
import pytest

from sturddle_view.events import EventBus
from sturddle_view.llm import ProviderChunk, ScriptedProvider, ToolRegistry
from sturddle_view.play.ai_analysis import AIAnalysisCoordinator
from sturddle_view.play.tools_engine import (
    REPORT_LINE_TOOL_NAME,
    REPORT_LINE_TOOL_SPEC,
    make_report_line_tool,
)


async def _drain_until_done(queue: asyncio.Queue) -> list:
    events = []
    while True:
        evt = await queue.get()
        events.append(evt)
        if evt.payload.get("done"):
            return events


def _coord_with_report_line(provider, board):
    reg = ToolRegistry()
    reg.register(REPORT_LINE_TOOL_SPEC, make_report_line_tool(lambda: board))
    bus = EventBus()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg, board_provider=lambda: board,
    )
    return coord, bus


# After 1.e4 e5 2.Nf3 Nc6: Nf6 and O-O are NOT legal on the live board, so
# prose naming them is flagged -- unless a reported line vouches for them.
def _live_board():
    b = chess.Board()
    for m in ["e4", "e5", "Nf3", "Nc6"]:
        b.push_san(m)
    return b


_PROSE = "White can try Bb5 Nf6 O-O building pressure."


@pytest.mark.asyncio
async def test_reported_line_suppresses_illegal_flag():
    board = _live_board()
    # Round 0: report the line. Round 1: narrate it. Round 2 wouldn't run --
    # round 1 is a clean exit if no corrective fires.
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(
            kind="tool_use", tool_use_id="tu_1", tool_name=REPORT_LINE_TOOL_NAME,
            tool_input={"moves": ["Bb5", "Nf6", "O-O"]},
        )],
        [ProviderChunk(kind="text", text=_PROSE)],
    ])
    coord, bus = _coord_with_report_line(provider, board)
    queue = await bus.subscribe()
    await coord.run(game_id="g")
    events = await _drain_until_done(queue)
    # No corrective fired: the reported line's positions grounded Nf6/O-O.
    correctives = [e for e in events if e.kind == "ai_corrective"]
    assert correctives == [], [e.payload for e in correctives]


@pytest.mark.asyncio
async def test_unreported_line_still_flagged():
    # Same prose WITHOUT reporting the line first -> Nf6/O-O flag as illegal
    # on the live board, so a corrective fires. This is the control: the
    # suppression above is the report_line effect, not a no-op validator.
    board = _live_board()
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(kind="text", text=_PROSE)],
        [ProviderChunk(kind="text", text="Rewritten cleanly.")],
    ])
    coord, bus = _coord_with_report_line(provider, board)
    queue = await bus.subscribe()
    await coord.run(game_id="g")
    events = await _drain_until_done(queue)
    correctives = [e for e in events if e.kind == "ai_corrective"]
    assert len(correctives) >= 1
    flagged = correctives[0].payload["illegal_moves"]
    assert "Nf6" in flagged and "O-O" in flagged
