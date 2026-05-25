"""`top_moves` tool tests.

Workaround for engines without native MultiPV: iterate legal moves,
search each child position, return sorted candidates. Tests pin:
- wire shape (move_uci/move_san + score fields per candidate)
- N cap clamp + default
- side-to-move-relative sort (white = high white-POV first; black flips)
- no-fen-input: tool reads live board via board_provider
- error envelopes (no_live_position, invalid_n, no_legal_moves)
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
async def test_top_moves_returns_n_candidates_sorted_for_white(tmp_path: Path):
    # Fake reports STM-POV cp; _score_to_cp flips to white-POV.
    # Negative raw on black's STM => positive white-POV.
    engine_path = make_position_aware_fake_uci(
        tmp_path, "tm_white",
        score_by_substring={
            "moves b2a1": -300,
            "moves b2a2": -100,
            "moves b2a3": -50,
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

    out = await tool({"n": 3, "depth": 4}, cancel_token=CancelToken())

    assert "error" not in out, out
    assert out["side_to_move"] == "white"
    assert len(out["candidates"]) == 3
    cps = [c["score_cp"] for c in out["candidates"]]
    assert cps == sorted(cps, reverse=True)
    assert cps == [300, 100, 50]
    sans = [c["move_san"] for c in out["candidates"]]
    assert sans == ["Ka1", "Ka2", "Ka3"]


@pytest.mark.asyncio
async def test_top_moves_default_n_is_three(tmp_path: Path):
    engine_path = make_searching_fake_uci(
        tmp_path, "tm_default_n", score_cp=0, depth=2, bestmove="0000", pv="",
    )
    bus = EventBus()
    board = chess.Board()
    tool = make_top_moves_tool(
        _launcher_from_path(engine_path, bus), bus=bus, board_provider=lambda: board,
    )
    out = await tool({"depth": 2}, cancel_token=CancelToken())
    assert "error" not in out, out
    assert len(out["candidates"]) == 3


@pytest.mark.asyncio
async def test_top_moves_clamps_n_to_max(tmp_path: Path):
    engine_path = make_searching_fake_uci(
        tmp_path, "tm_clamp_n", score_cp=0, depth=2, bestmove="0000", pv="",
    )
    bus = EventBus()
    board = chess.Board()
    tool = make_top_moves_tool(
        _launcher_from_path(engine_path, bus), bus=bus, board_provider=lambda: board,
    )
    out = await tool({"n": 999, "depth": 2}, cancel_token=CancelToken())
    assert "error" not in out, out
    assert len(out["candidates"]) == TOP_MOVES_MAX_N


@pytest.mark.asyncio
async def test_top_moves_no_live_position_when_provider_returns_none():
    def _no_launcher() -> EngineSupervisor:
        raise AssertionError("engine should never spawn when no board")

    tool = make_top_moves_tool(
        _no_launcher, bus=EventBus(), board_provider=lambda: None,
    )
    out = await tool({}, cancel_token=CancelToken())
    assert out.get("error") == "no_live_position"


@pytest.mark.asyncio
async def test_top_moves_invalid_n_returns_structured_error():
    def _no_launcher() -> EngineSupervisor:
        raise AssertionError("engine should never spawn for invalid n")

    board = chess.Board()
    tool = make_top_moves_tool(
        _no_launcher, bus=EventBus(), board_provider=lambda: board,
    )
    out = await tool({"n": "not-a-number"}, cancel_token=CancelToken())
    assert out.get("error") == "invalid_n"


@pytest.mark.asyncio
async def test_top_moves_no_legal_moves_when_terminal_position():
    board = chess.Board("7k/8/5KQ1/8/8/8/8/8 b - - 0 1")
    assert board.is_stalemate()

    def _no_launcher() -> EngineSupervisor:
        raise AssertionError("engine should never spawn with no legal moves")

    tool = make_top_moves_tool(
        _no_launcher, bus=EventBus(), board_provider=lambda: board,
    )
    out = await tool({}, cancel_token=CancelToken())
    assert out.get("error") == "no_legal_moves"


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
    out = await tool({"n": 2, "depth": 2}, cancel_token=CancelToken())
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
    out = await tool({"n": 3, "depth": 3}, cancel_token=token)
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
    out = await tool({"n": 1, "depth": 4}, cancel_token=CancelToken())
    assert "error" not in out, out
    c = out["candidates"][0]
    assert "move_uci" in c
    assert "move_san" in c
    assert c["move_san"] != c["move_uci"]
