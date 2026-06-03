"""`report_line` tool tests.

Structured grounding channel: the model submits a continuation, the tool
replays it move-by-move and confirms legality in order. Pins wire shape,
the per-ply error envelope, the from_fen override, notation-agnostic
parsing (UCI or SAN), and the ply cap.
"""
from __future__ import annotations

import chess
import pytest

from sturddle_view.llm.cancel import CancelToken
from sturddle_view.play.tools_engine import (
    REPORT_LINE_MAX_PLIES,
    make_report_line_tool,
)


@pytest.mark.asyncio
async def test_report_line_replays_legal_sequence():
    board = chess.Board()
    tool = make_report_line_tool(board_provider=lambda: board)
    out = await tool({"moves": ["e4", "e5", "Nf3", "Nc6"]}, cancel_token=CancelToken())
    assert out["ok"] is True
    assert out["san"] == "e4 e5 Nf3 Nc6"
    # fens = start + one after each ply.
    assert len(out["fens"]) == 5
    assert out["fens"][0] == chess.Board().fen()
    assert out["end_fen"] == out["fens"][-1]


@pytest.mark.asyncio
async def test_report_line_accepts_uci_and_san_mixed():
    board = chess.Board()
    tool = make_report_line_tool(board_provider=lambda: board)
    out = await tool({"moves": ["e2e4", "e5", "g1f3"]}, cancel_token=CancelToken())
    assert out["ok"] is True
    assert out["san"] == "e4 e5 Nf3"


@pytest.mark.asyncio
async def test_report_line_flags_illegal_move_with_ply():
    board = chess.Board()
    tool = make_report_line_tool(board_provider=lambda: board)
    # e4 e5 then a second e4 is illegal (pawn already there / not legal).
    out = await tool({"moves": ["e4", "e5", "e4"]}, cancel_token=CancelToken())
    assert "error" in out
    assert out["ply"] == 2
    assert out["move_input"] == "e4"


@pytest.mark.asyncio
async def test_report_line_starts_from_supplied_fen():
    # Live board is startpos, but the line is reported from a midgame fen.
    board = chess.Board()
    tool = make_report_line_tool(board_provider=lambda: board)
    after_e4 = chess.Board()
    after_e4.push_uci("e2e4")
    out = await tool(
        {"moves": ["e5", "Nf3"], "from_fen": after_e4.fen()},
        cancel_token=CancelToken(),
    )
    assert out["ok"] is True
    assert out["san"] == "e5 Nf3"
    assert out["fens"][0] == after_e4.fen()


@pytest.mark.asyncio
async def test_report_line_from_startpos_alias():
    tool = make_report_line_tool(board_provider=lambda: None)
    out = await tool(
        {"moves": ["e4"], "from_fen": "startpos"}, cancel_token=CancelToken()
    )
    assert out["ok"] is True
    assert out["san"] == "e4"


@pytest.mark.asyncio
async def test_report_line_errors_without_board_or_fen():
    tool = make_report_line_tool(board_provider=lambda: None)
    out = await tool({"moves": ["e4"]}, cancel_token=CancelToken())
    assert out == {"error": "no_live_position"}


@pytest.mark.asyncio
async def test_report_line_errors_on_empty_moves():
    tool = make_report_line_tool(board_provider=lambda: chess.Board())
    out = await tool({"moves": []}, cancel_token=CancelToken())
    assert out["error"] == "invalid_input"


@pytest.mark.asyncio
async def test_report_line_errors_on_invalid_fen():
    tool = make_report_line_tool(board_provider=lambda: chess.Board())
    out = await tool(
        {"moves": ["e4"], "from_fen": "not-a-fen"}, cancel_token=CancelToken()
    )
    assert out["error"] == "invalid_fen"


@pytest.mark.asyncio
async def test_report_line_caps_ply_count():
    # A line longer than the cap is truncated; the flag is surfaced.
    board = chess.Board()
    tool = make_report_line_tool(board_provider=lambda: board)
    # Shuffle the knights back and forth to build a long legal line.
    long_line = []
    for _ in range((REPORT_LINE_MAX_PLIES // 4) + 2):
        long_line += ["Nf3", "Nf6", "Ng1", "Ng8"]
    out = await tool({"moves": long_line}, cancel_token=CancelToken())
    assert out["ok"] is True
    assert out.get("truncated") is True
    assert len(out["fens"]) == REPORT_LINE_MAX_PLIES + 1
