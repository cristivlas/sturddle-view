"""`top_moves` tool tests.

Deep-evaluates a model-supplied list of candidate moves (one search per
move, sequential). Tests pin:
- wire shape (move_uci/move_san + score fields per candidate)
- moves-list cap + truncation flag
- side-to-move-relative sort (white = high white-POV first; black flips)
- no-fen-input: tool reads live board via board_provider
- error envelopes (no_live_position, invalid_input)
- per-move errors for illegal/unparseable inputs (other moves still run)
- engine_info publication per candidate; engine_search_start emitted once
"""
from __future__ import annotations

from pathlib import Path

import chess
import pytest

from sturddle_view.events import EventBus
from sturddle_view.llm.cancel import CancelToken
from sturddle_view.play.engine_supervisor import EngineSupervisor
from sturddle_view.play.tools_engine import (
    TOP_MOVES_MAX_N,
    make_top_moves_tool,
)

from .conftest import make_position_aware_fake_uci, make_searching_fake_uci


def _launcher_from_path(path: str, bus: EventBus):
    def _make() -> EngineSupervisor:
        return EngineSupervisor(engine_path=path, bus=bus)
    return _make


@pytest.mark.asyncio
async def test_top_moves_returns_candidates_sorted_for_white(tmp_path: Path):
    # Fake reports STM-POV cp (White to move). Higher cp = better for White.
    engine_path = make_position_aware_fake_uci(
        tmp_path, "tm_white",
        score_by_substring={
            "searchmoves b2a1": 300,
            "searchmoves b2a2": 100,
            "searchmoves b2a3": 50,
        },
        default_score_cp=0,
    )
    bus = EventBus()
    board = chess.Board("7k/8/8/8/8/8/1K6/8 w - - 0 1")
    tool = make_top_moves_tool(
        _launcher_from_path(engine_path, bus),
        bus=bus,
        board_provider=lambda: board,
    )

    out = await tool(
        {"moves": ["Ka1", "Ka2", "Ka3"], "depth": 4}, cancel_token=CancelToken(),
    )

    assert "error" not in out, out
    assert out["side_to_move"] == "white"
    assert len(out["candidates"]) == 3
    cps = [c["score_cp"] for c in out["candidates"]]
    assert cps == sorted(cps, reverse=True)
    assert cps == [300, 100, 50]
    sans = [c["move_san"] for c in out["candidates"]]
    assert sans == ["Ka1", "Ka2", "Ka3"]


@pytest.mark.asyncio
async def test_top_moves_returns_candidates_sorted_for_black(tmp_path: Path):
    # Black to move; engine reports STM-POV (Black's). _score_to_cp
    # flips to white-POV (values negated). Best-for-Black = LOWEST.
    engine_path = make_position_aware_fake_uci(
        tmp_path, "tm_black",
        score_by_substring={
            "searchmoves a7a6": 300,
            "searchmoves a7a5": 100,
            "searchmoves b7b6": 50,
        },
        default_score_cp=0,
    )
    bus = EventBus()
    board = chess.Board()
    board.push_san("e4")
    tool = make_top_moves_tool(
        _launcher_from_path(engine_path, bus),
        bus=bus,
        board_provider=lambda: board,
    )

    out = await tool(
        {"moves": ["a6", "a5", "b6"], "depth": 4}, cancel_token=CancelToken(),
    )

    assert "error" not in out, out
    assert out["side_to_move"] == "black"
    cps = [c["score_cp"] for c in out["candidates"]]
    assert cps == sorted(cps)
    sans = [c["move_san"] for c in out["candidates"]]
    assert sans == ["a6", "a5", "b6"]


@pytest.mark.asyncio
async def test_top_moves_truncates_list_above_cap(tmp_path: Path):
    engine_path = make_searching_fake_uci(
        tmp_path, "tm_trunc", score_cp=0, depth=2, bestmove="0000", pv="",
    )
    bus = EventBus()
    board = chess.Board()
    tool = make_top_moves_tool(
        _launcher_from_path(engine_path, bus), bus=bus, board_provider=lambda: board,
    )
    too_many = ["a3", "b3", "c3", "d3", "e3", "f3", "g3", "h3"]
    assert len(too_many) > TOP_MOVES_MAX_N
    out = await tool({"moves": too_many, "depth": 2}, cancel_token=CancelToken())
    assert "error" not in out, out
    assert out.get("truncated") is True
    assert len(out["candidates"]) == TOP_MOVES_MAX_N


@pytest.mark.asyncio
async def test_top_moves_no_live_position_when_provider_returns_none():
    def _no_launcher() -> EngineSupervisor:
        raise AssertionError("engine should never spawn when no board")

    tool = make_top_moves_tool(
        _no_launcher, bus=EventBus(), board_provider=lambda: None,
    )
    out = await tool({"moves": ["e4"]}, cancel_token=CancelToken())
    assert out.get("error") == "no_live_position"


@pytest.mark.asyncio
async def test_top_moves_missing_moves_returns_invalid_input():
    def _no_launcher() -> EngineSupervisor:
        raise AssertionError("engine should never spawn without moves")

    board = chess.Board()
    tool = make_top_moves_tool(
        _no_launcher, bus=EventBus(), board_provider=lambda: board,
    )
    out = await tool({}, cancel_token=CancelToken())
    assert out.get("error") == "invalid_input"


@pytest.mark.asyncio
async def test_top_moves_empty_list_returns_invalid_input():
    def _no_launcher() -> EngineSupervisor:
        raise AssertionError("engine should never spawn for empty list")

    board = chess.Board()
    tool = make_top_moves_tool(
        _no_launcher, bus=EventBus(), board_provider=lambda: board,
    )
    out = await tool({"moves": []}, cancel_token=CancelToken())
    assert out.get("error") == "invalid_input"


@pytest.mark.asyncio
async def test_top_moves_per_move_errors_dont_block_legal_ones(tmp_path: Path):
    engine_path = make_searching_fake_uci(
        tmp_path, "tm_mixed", score_cp=10, depth=2, bestmove="0000", pv="",
    )
    bus = EventBus()
    board = chess.Board()
    tool = make_top_moves_tool(
        _launcher_from_path(engine_path, bus), bus=bus, board_provider=lambda: board,
    )
    # e4 legal, z9 invalid, e5 illegal (black's move, white to move).
    out = await tool(
        {"moves": ["e4", "z9", "e5"], "depth": 2}, cancel_token=CancelToken(),
    )
    assert "error" not in out, out
    assert len(out["candidates"]) == 1
    assert out["candidates"][0]["move_san"] == "e4"
    assert "errors" in out
    error_inputs = [e["move_input"] for e in out["errors"]]
    assert "z9" in error_inputs
    assert "e5" in error_inputs


@pytest.mark.asyncio
async def test_top_moves_publishes_engine_search_start_once(tmp_path: Path):
    engine_path = make_searching_fake_uci(
        tmp_path, "tm_evts", score_cp=0, depth=2, bestmove="0000", pv="",
    )
    bus = EventBus()
    queue = await bus.subscribe()
    board = chess.Board()
    tool = make_top_moves_tool(
        _launcher_from_path(engine_path, bus), bus=bus, board_provider=lambda: board,
        game_id_provider=lambda: "g-live",
    )
    out = await tool({"moves": ["e4", "d4"], "depth": 2}, cancel_token=CancelToken())
    assert "error" not in out, out

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    starts = [e for e in events if e.kind == "engine_search_start"]
    assert len(starts) == 1, f"expected 1 engine_search_start, got {len(starts)}"
    infos = [e for e in events if e.kind == "engine_info"]
    assert infos, "tool published no engine_info events"
    for e in starts + infos:
        assert e.game_id == "g-live"


@pytest.mark.asyncio
async def test_top_moves_cancel_returns_cancelled_marker(tmp_path: Path):
    engine_path = make_searching_fake_uci(
        tmp_path, "tm_cancel", score_cp=10, depth=3, bestmove="0000", pv="",
    )
    bus = EventBus()
    board = chess.Board()
    tool = make_top_moves_tool(
        _launcher_from_path(engine_path, bus), bus=bus, board_provider=lambda: board,
    )
    token = CancelToken()
    token.cancel()
    out = await tool({"moves": ["e4", "d4", "Nf3"], "depth": 3}, cancel_token=token)
    assert out.get("cancelled") is True
    assert "candidates" in out


@pytest.mark.asyncio
async def test_top_moves_returns_san_and_uci(tmp_path: Path):
    engine_path = make_searching_fake_uci(
        tmp_path, "tm_shape", score_cp=20, depth=4, bestmove="0000", pv="",
    )
    bus = EventBus()
    board = chess.Board()
    tool = make_top_moves_tool(
        _launcher_from_path(engine_path, bus), bus=bus, board_provider=lambda: board,
    )
    out = await tool({"moves": ["e4"], "depth": 4}, cancel_token=CancelToken())
    assert "error" not in out, out
    c = out["candidates"][0]
    assert "move_uci" in c
    assert "move_san" in c
    assert c["move_san"] != c["move_uci"]
