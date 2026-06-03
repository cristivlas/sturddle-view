"""`piece_at` tool tests.

Anti-hallucination probe: model passes a square, gets back what is
actually there. Pins wire shape, empty-square null, error envelopes,
and case-insensitive square parsing.
"""
from __future__ import annotations

import chess
import pytest

from sturddle_view.llm.cancel import CancelToken
from sturddle_view.play.tools_engine import make_piece_at_tool


@pytest.mark.asyncio
async def test_piece_at_returns_piece_on_occupied_square():
    board = chess.Board()
    tool = make_piece_at_tool(board_provider=lambda: board)

    out = await tool({"square": "e1"}, cancel_token=CancelToken())

    assert out == {
        "square": "e1",
        "piece": {"type": "king", "color": "white", "symbol": "K"},
    }


@pytest.mark.asyncio
async def test_piece_at_returns_null_piece_on_empty_square():
    board = chess.Board()
    tool = make_piece_at_tool(board_provider=lambda: board)

    out = await tool({"square": "e4"}, cancel_token=CancelToken())

    assert out == {"square": "e4", "piece": None}


@pytest.mark.asyncio
async def test_piece_at_is_case_insensitive():
    board = chess.Board()
    tool = make_piece_at_tool(board_provider=lambda: board)

    out = await tool({"square": "E1"}, cancel_token=CancelToken())

    assert out["square"] == "e1"
    assert out["piece"]["type"] == "king"


@pytest.mark.asyncio
async def test_piece_at_reports_black_piece_color_and_symbol():
    board = chess.Board()
    tool = make_piece_at_tool(board_provider=lambda: board)

    out = await tool({"square": "g8"}, cancel_token=CancelToken())

    assert out["piece"] == {"type": "knight", "color": "black", "symbol": "n"}


@pytest.mark.asyncio
async def test_piece_at_errors_when_no_live_board():
    tool = make_piece_at_tool(board_provider=lambda: None)

    out = await tool({"square": "e4"}, cancel_token=CancelToken())

    assert out == {"error": "no_live_position"}


@pytest.mark.asyncio
async def test_piece_at_errors_on_bad_square_string():
    board = chess.Board()
    tool = make_piece_at_tool(board_provider=lambda: board)

    out = await tool({"square": "z9"}, cancel_token=CancelToken())

    assert out["error"] == "invalid_square"
    assert "detail" in out


@pytest.mark.asyncio
async def test_piece_at_errors_on_missing_square():
    board = chess.Board()
    tool = make_piece_at_tool(board_provider=lambda: board)

    out = await tool({}, cancel_token=CancelToken())

    assert out["error"] == "invalid_square"


@pytest.mark.asyncio
async def test_piece_at_reads_supplied_fen_over_live_board():
    # fen present -> read that position, not the live board. e4 is empty on
    # the live startpos board but occupied after 1.e4 in the supplied fen.
    board = chess.Board()  # live = startpos, e4 empty
    tool = make_piece_at_tool(board_provider=lambda: board)
    after_e4 = chess.Board()
    after_e4.push_uci("e2e4")
    out = await tool(
        {"square": "e4", "fen": after_e4.fen()}, cancel_token=CancelToken()
    )
    assert out["piece"] == {"type": "pawn", "color": "white", "symbol": "P"}


@pytest.mark.asyncio
async def test_piece_at_fen_works_without_live_board():
    # No live board, but a fen is supplied -> still answers.
    tool = make_piece_at_tool(board_provider=lambda: None)
    out = await tool({"square": "e1", "fen": "startpos"}, cancel_token=CancelToken())
    assert out["piece"] == {"type": "king", "color": "white", "symbol": "K"}


@pytest.mark.asyncio
async def test_piece_at_errors_on_invalid_fen():
    tool = make_piece_at_tool(board_provider=lambda: chess.Board())
    out = await tool({"square": "e1", "fen": "not-a-fen"}, cancel_token=CancelToken())
    assert out["error"] == "invalid_fen"
