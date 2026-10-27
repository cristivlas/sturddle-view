"""MultiPV in the AI engine tools.

- `analyze` offers a `lines` arg only while the Lines setting and the
  analysis engine's MultiPV both allow two or more; asked, it returns that
  many ranked lines (clamped to the cap), the best line's fields on top.
- `top_moves` scores every candidate in one root-restricted MultiPV search
  when the engine has MultiPV, and searches any candidate the batch left out
  on its own.

The fake engine logs each `go` line so tests can count searches.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import chess
import chess.engine
import pytest

from sturddle_view.events import EVT_ENGINE_SEARCH_START, EventBus
from sturddle_view.llm.cancel import CancelToken
from sturddle_view.llm.tools import ToolRegistry
from sturddle_view.play import tools_engine
from sturddle_view.play.engine_supervisor import EngineSupervisor
from sturddle_view.play.tools_engine import (
    ANALYZE_TOOL_NAME,
    analyze_tool_spec,
    make_analyze_tool,
    make_top_moves_tool,
)

from .conftest import _write_uci_stub, make_searching_fake_uci

# White king on b2, black king on h8: the king's moves are the legal moves.
FEN = "7k/8/8/8/8/8/1K6/8 w - - 0 1"
# Side-to-move cp per root move; the fake ranks lines by these.
SCORES = {"b2a1": 300, "b2a2": 100, "b2a3": 50, "b2b1": 40, "b2c1": 30}
ENGINE_MULTIPV_MAX = 500
LINES_SETTING = 3
DEPTH = 4
LINES_KEY = "lines"


def _multipv_fake(root: Path, name: str) -> tuple[str, Path]:
    """Fake UCI honoring MultiPV and searchmoves: emits one ranked info line
    per root move (best first, up to MultiPV), logging each `go`."""
    log = root / f"{name}.go.log"
    pre_loop = (
        "import re\n"
        f"scores = {json.dumps(SCORES)}\n"
        f"log_path = {str(log)!r}\n"
        "multipv = 1\n"
    )
    extra = (
        "    elif line.startswith('setoption name MultiPV value'):\n"
        "        multipv = int(line.split()[-1])\n"
        "    elif line.startswith('go'):\n"
        "        open(log_path, 'a').write(line + '\\n')\n"
        "        tail = line.split('searchmoves', 1)[1] if 'searchmoves' in line else ''\n"
        "        roots = re.findall(r'\\b[a-h][1-8][a-h][1-8][qrbn]?\\b', tail) or list(scores)\n"
        "        ranked = sorted(roots, key=lambda m: -scores.get(m, 0))[:multipv]\n"
        # Reports the requested depth, so the result counts as complete.
        "        depth = re.search(r'depth (\\d+)', line).group(1)\n"
        "        for k, mv in enumerate(ranked, 1):\n"
        "            cp = scores.get(mv, 0)\n"
        "            sys.stdout.write(f'info depth {depth} multipv {k} score cp {cp} pv {mv}\\n')\n"
        "        sys.stdout.write(f'bestmove {ranked[0]}\\n')\n"
        "        sys.stdout.flush()\n"
    )
    option = f"option name MultiPV type spin default 1 min 1 max {ENGINE_MULTIPV_MAX}\\n"
    return _write_uci_stub(root, name, extra, pre_loop=pre_loop, uci_options=option), log


def _go_lines(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def _launcher(path: str, bus: EventBus):
    return lambda: EngineSupervisor(engine_path=path, bus=bus)


def _settings(lines: int = LINES_SETTING):
    return lambda: SimpleNamespace(analysis_multipv=lines)


@pytest.mark.parametrize(("lines", "multipv_max", "offered"), [
    (LINES_SETTING, ENGINE_MULTIPV_MAX, True),
    (1, ENGINE_MULTIPV_MAX, False),  # Lines setting says one line
    (LINES_SETTING, 0, False),  # engine without MultiPV
    (LINES_SETTING, None, False),  # MultiPV support not probed yet
])
def test_analyze_offers_lines_only_when_available(lines, multipv_max, offered):
    registry = ToolRegistry()
    spec = analyze_tool_spec(_settings(lines), lambda: multipv_max)
    registry.register(spec, make_analyze_tool(None, EventBus()))
    (schema,) = [s for s in registry.schemas() if s["name"] == ANALYZE_TOOL_NAME]
    assert (LINES_KEY in schema["input_schema"]["properties"]) is offered


@pytest.mark.asyncio
async def test_analyze_returns_ranked_lines_clamped_to_cap(tmp_path: Path):
    path, log = _multipv_fake(tmp_path, "mpv")
    bus = EventBus()
    tool = make_analyze_tool(
        _launcher(path, bus), bus, settings_provider=_settings(),
        multipv_max_provider=lambda: ENGINE_MULTIPV_MAX,
    )
    out = await tool({"fen": FEN, "depth": DEPTH, LINES_KEY: 10}, cancel_token=CancelToken())

    assert [line["bestmove"] for line in out[LINES_KEY]] == ["b2a1", "b2a2", "b2a3"]
    assert out["bestmove"] == "b2a1" and out["score_cp"] == SCORES["b2a1"]
    assert len(_go_lines(log)) == 1

    # Same position + line count: served from the cache, no new search.
    again = await tool({"fen": FEN, "depth": DEPTH, LINES_KEY: 10}, cancel_token=CancelToken())
    assert again[LINES_KEY] == out[LINES_KEY] and len(_go_lines(log)) == 1


@pytest.mark.asyncio
async def test_analyze_without_lines_arg_is_single_line(tmp_path: Path):
    path, _log = _multipv_fake(tmp_path, "mpv")
    bus = EventBus()
    tool = make_analyze_tool(
        _launcher(path, bus), bus, settings_provider=_settings(),
        multipv_max_provider=lambda: ENGINE_MULTIPV_MAX,
    )
    out = await tool({"fen": FEN, "depth": DEPTH}, cancel_token=CancelToken())
    assert LINES_KEY not in out and out["bestmove"] == "b2a1"


def _top_moves(path: str, multipv_max):
    bus = EventBus()
    board = chess.Board(FEN)
    return make_top_moves_tool(
        _launcher(path, bus), bus=bus, board_provider=lambda: board,
        multipv_max_provider=lambda: multipv_max,
    )


CANDIDATES = ["Ka3", "Ka1", "Ka2"]
RANKED_UCI = ["b2a1", "b2a2", "b2a3"]


@pytest.mark.asyncio
async def test_top_moves_scores_candidates_in_one_multipv_search(tmp_path: Path):
    path, log = _multipv_fake(tmp_path, "mpv")
    out = await _top_moves(path, ENGINE_MULTIPV_MAX)(
        {"moves": CANDIDATES, "depth": DEPTH}, cancel_token=CancelToken(),
    )
    assert [c["move_uci"] for c in out["candidates"]] == RANKED_UCI
    assert [c["score_cp"] for c in out["candidates"]] == [SCORES[m] for m in RANKED_UCI]
    assert len(_go_lines(log)) == 1


@pytest.mark.asyncio
async def test_top_moves_searches_candidates_the_batch_left_out(tmp_path: Path):
    # Engine max of two lines: the batch covers the best two; the third
    # candidate gets its own root-restricted search.
    path, log = _multipv_fake(tmp_path, "mpv")
    out = await _top_moves(path, 2)(
        {"moves": CANDIDATES, "depth": DEPTH}, cancel_token=CancelToken(),
    )
    assert [c["move_uci"] for c in out["candidates"]] == RANKED_UCI
    assert len(_go_lines(log)) == 2


@pytest.mark.asyncio
async def test_top_moves_without_multipv_searches_each_candidate(tmp_path: Path):
    path, log = _multipv_fake(tmp_path, "mpv")
    out = await _top_moves(path, 0)(
        {"moves": CANDIDATES, "depth": DEPTH}, cancel_token=CancelToken(),
    )
    assert [c["move_uci"] for c in out["candidates"]] == RANKED_UCI
    assert len(_go_lines(log)) == len(CANDIDATES)


@pytest.mark.asyncio
async def test_analyze_lines_with_stale_multipv_support_searches_best_line(tmp_path: Path):
    # The registry still claims MultiPV, but the engine binary lacks it.
    path = make_searching_fake_uci(tmp_path, "single", bestmove="b2a1", pv="b2a1")
    bus = EventBus()
    tool = make_analyze_tool(
        _launcher(path, bus), bus, settings_provider=_settings(),
        multipv_max_provider=lambda: ENGINE_MULTIPV_MAX,
    )
    args = {"fen": FEN, "depth": DEPTH, LINES_KEY: LINES_SETTING}
    out = await tool(args, cancel_token=CancelToken())
    assert "error" not in out and out["bestmove"] == "b2a1"
    assert [line["bestmove"] for line in out[LINES_KEY]] == ["b2a1"]


async def _drain(queue) -> list:
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


@pytest.mark.asyncio
async def test_top_moves_per_move_searches_after_a_batch_start_a_fresh_panel(tmp_path: Path):
    # The client clears numbered arrows on search start; without a fresh one
    # a candidate searched alone would show as the batch's line 1.
    path, _log = _multipv_fake(tmp_path, "mpv")
    bus = EventBus()
    queue = await bus.subscribe()
    board = chess.Board(FEN)
    tool = make_top_moves_tool(
        _launcher(path, bus), bus=bus, board_provider=lambda: board,
        multipv_max_provider=lambda: 2,
    )
    await tool({"moves": CANDIDATES, "depth": DEPTH}, cancel_token=CancelToken())
    kinds = [e.kind for e in await _drain(queue)]
    assert kinds.count(EVT_ENGINE_SEARCH_START) == 2


@pytest.mark.asyncio
async def test_top_moves_keeps_batch_results_when_cancelled(monkeypatch):
    """A batch cancelled after scoring candidates keeps them, as per-move
    results finished before a cancel always were; nothing else searches."""
    per_move: list = []

    async def cancelled_batch(_launcher, board, limit, multipv, **_kw):
        def line(uci):
            score = chess.engine.PovScore(chess.engine.Cp(SCORES[uci]), chess.WHITE)
            return {"depth": DEPTH, "score": score, "pv": [chess.Move.from_uci(uci)]}
        return [line(m) for m in RANKED_UCI[:multipv]], True

    async def one_search(*_a, **_kw):
        per_move.append(1)
        return {}, False

    monkeypatch.setattr(tools_engine, "_run_lines_search", cancelled_batch)
    monkeypatch.setattr(tools_engine, "_run_one_search", one_search)
    tool = _top_moves("unused", 2)
    out = await tool({"moves": CANDIDATES, "depth": DEPTH}, cancel_token=CancelToken())
    assert [c["move_uci"] for c in out["candidates"]] == RANKED_UCI[:2]
    assert out["cancelled"] is True and per_move == []
