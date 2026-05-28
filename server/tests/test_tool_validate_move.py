"""`validate_move` tool tests.

Accepts UCI or SAN; returns legal/uci/san on success, structured error
on illegal/malformed/no-board.
"""
from __future__ import annotations

import chess
import pytest

from sturddle_view.llm.cancel import CancelToken
from sturddle_view.play.tools_engine import make_validate_move_tool


@pytest.mark.asyncio
async def test_validate_move_accepts_legal_uci():
    board = chess.Board()
    tool = make_validate_move_tool(board_provider=lambda: board)

    out = await tool({"move": "g1f3"}, cancel_token=CancelToken())

    assert out == {"legal": True, "uci": "g1f3", "san": "Nf3"}


@pytest.mark.asyncio
async def test_validate_move_accepts_legal_san():
    board = chess.Board()
    tool = make_validate_move_tool(board_provider=lambda: board)

    out = await tool({"move": "Nf3"}, cancel_token=CancelToken())

    assert out == {"legal": True, "uci": "g1f3", "san": "Nf3"}


@pytest.mark.asyncio
async def test_validate_move_accepts_castling_san():
    board = chess.Board("r3k2r/pppppppp/8/8/8/8/PPPPPPPP/R3K2R w KQkq - 0 1")
    tool = make_validate_move_tool(board_provider=lambda: board)

    out = await tool({"move": "O-O"}, cancel_token=CancelToken())

    assert out["legal"] is True
    assert out["uci"] == "e1g1"
    assert out["san"] == "O-O"


@pytest.mark.asyncio
async def test_validate_move_accepts_promotion_uci():
    board = chess.Board("8/P7/8/8/8/8/8/k6K w - - 0 1")
    tool = make_validate_move_tool(board_provider=lambda: board)

    out = await tool({"move": "a7a8q"}, cancel_token=CancelToken())

    assert out["legal"] is True
    assert out["uci"] == "a7a8q"
    assert out["san"] == "a8=Q+"


@pytest.mark.asyncio
async def test_validate_move_rejects_illegal_san():
    board = chess.Board()
    tool = make_validate_move_tool(board_provider=lambda: board)

    out = await tool({"move": "Nf6"}, cancel_token=CancelToken())

    assert out["error"] == "illegal_move"
    assert "detail" in out


@pytest.mark.asyncio
async def test_validate_move_rejects_illegal_uci():
    # Well-formed UCI but not legal from startpos -- pawn cannot jump
    # three squares. Earlier impl mis-classified this as invalid_move
    # by falling through to SAN parsing.
    board = chess.Board()
    tool = make_validate_move_tool(board_provider=lambda: board)

    out = await tool({"move": "e2e5"}, cancel_token=CancelToken())

    assert out["error"] == "illegal_move"
    assert "detail" in out


@pytest.mark.asyncio
async def test_validate_move_rejects_garbage_string():
    board = chess.Board()
    tool = make_validate_move_tool(board_provider=lambda: board)

    out = await tool({"move": "zzz"}, cancel_token=CancelToken())

    assert out["error"] == "invalid_move"
    assert "detail" in out


@pytest.mark.asyncio
async def test_validate_move_errors_when_no_live_board():
    tool = make_validate_move_tool(board_provider=lambda: None)

    out = await tool({"move": "e2e4"}, cancel_token=CancelToken())

    assert out == {"error": "no_live_position"}


@pytest.mark.asyncio
async def test_validate_move_errors_on_missing_input():
    board = chess.Board()
    tool = make_validate_move_tool(board_provider=lambda: board)

    out = await tool({}, cancel_token=CancelToken())

    assert out["error"] == "invalid_input"


@pytest.mark.asyncio
async def test_validate_move_errors_on_empty_string():
    board = chess.Board()
    tool = make_validate_move_tool(board_provider=lambda: board)

    out = await tool({"move": "   "}, cancel_token=CancelToken())

    assert out["error"] == "invalid_input"
