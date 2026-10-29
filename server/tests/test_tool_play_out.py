"""`play_out` tool: the narrator plays an idea out one move at a time.

Pins the contract the skin-in-the-game design rests on
(docs/ai-analysis-skin-in-the-game.md, item 2-3):

- each step answers with the engine's best reply and the step's eval drop,
  never the engine's best for the model's own side,
- one line per idea, continued by the same label; a new idea starts from the
  live position,
- a step that drops the mover's eval past PLAY_OUT_MARGIN_CP fails and is not
  kept, yet its first move still counts as played out,
- a mating move ends the line; the record ranks first moves by eval after.

Searches come from a scripted _run_one_search (side-to-move POV), so the
real SearchCache and step logic run without an engine.
"""
from __future__ import annotations

import chess
import pytest

from sturddle_view.events import EVT_ENGINE_SEARCH_START, EventBus
from sturddle_view.llm.cancel import CancelToken
from sturddle_view.play import tools_engine
from sturddle_view.play.tools_engine import (
    PLAY_OUT_MARGIN_CP,
    PlayOutRecord,
    make_play_out_tool,
)

from .scripted_search import epd_after, scripted_search


# Start: White +30. After 1.e4 the engine answers ...e5 (White +25: a 5cp
# drop); after 1.f3 it answers ...e5 too, but White is at -100 (a 130cp
# drop). The start search's own best (d4) must never reach the model.
_SCRIPT = {
    (epd_after(), None): (30, ["d2d4", "d7d5"]),
    (epd_after("e4"), None): (-25, ["e7e5", "g1f3"]),
    (epd_after("e4", "e5"), None): (28, ["d2d4"]),
    (epd_after("e4", "e5", "Nf3"), None): (-20, ["b8c6"]),
    (epd_after("f3"), None): (100, ["e7e5"]),
    (epd_after("d4"), None): (-30, ["d7d5"]),
}


def _tool(monkeypatch, board: chess.Board, record: PlayOutRecord, script=_SCRIPT, calls=None):
    calls = [] if calls is None else calls
    monkeypatch.setattr(tools_engine, "_run_one_search", scripted_search(script, calls))
    bus = EventBus()
    tool = make_play_out_tool(lambda: None, bus=bus, board_provider=lambda: board, record=record)
    return tool, bus


async def _step(tool, idea: str, move: str) -> dict:
    return await tool({"idea": idea, "move": move}, cancel_token=CancelToken())


@pytest.mark.asyncio
async def test_first_step_returns_the_reply_and_the_drop(monkeypatch):
    record = PlayOutRecord()
    tool, _bus = _tool(monkeypatch, chess.Board(), record)
    out = await _step(tool, "e4, aim d4", "e4")
    after = chess.Board()
    for san in ("e4", "e5"):
        after.push_san(san)
    assert out == {
        "idea": "e4, aim d4", "move": "e4", "reply": "e5", "line": "1.e4 e5",
        "eval_before": {"score_cp": 30, "score_pawns": 0.3, "score_text": "+0.30"},
        "eval_after": {"score_cp": 25, "score_pawns": 0.25, "score_text": "+0.25"},
        "drop_cp": 5, "fen": after.fen(),
    }
    assert record.played_out(chess.Move.from_uci("e2e4"))


@pytest.mark.asyncio
async def test_same_idea_continues_its_line(monkeypatch):
    record = PlayOutRecord()
    tool, _bus = _tool(monkeypatch, chess.Board(), record)
    await _step(tool, "e4, aim d4", "e4")
    # The label matches case- and space-insensitively.
    out = await _step(tool, "E4,  aim d4", "Nf3")
    assert out["line"] == "1.e4 e5 2.Nf3 Nc6"
    assert out["reply"] == "Nc6"
    assert out["drop_cp"] == 28 - 20
    # Only first moves count as played out.
    assert not record.played_out(chess.Move.from_uci("g1f3"))


@pytest.mark.asyncio
async def test_new_idea_starts_from_the_live_position(monkeypatch):
    record = PlayOutRecord()
    tool, _bus = _tool(monkeypatch, chess.Board(), record)
    await _step(tool, "e4, aim d4", "e4")
    out = await _step(tool, "d4 first", "d4")
    assert out["line"] == "1.d4 d5"
    assert record.played_out(chess.Move.from_uci("d2d4"))


@pytest.mark.asyncio
async def test_failed_step_is_not_kept_but_counts_as_played_out(monkeypatch):
    record = PlayOutRecord()
    board = chess.Board()
    tool, _bus = _tool(monkeypatch, board, record)
    out = await _step(tool, "f3 idea", "f3")
    assert out["failed"] is True
    assert out["drop_cp"] == 130 > PLAY_OUT_MARGIN_CP
    assert "Replace f3" in out["reason"]
    # The refutation shows, but the line stays at the live position.
    assert out["line"] == "1.f3 e5"
    assert out["fen"] == board.fen()
    assert record.played_out(chess.Move.from_uci("f2f3"))
    # The next move of that idea is again a first move from the start.
    retry = await _step(tool, "f3 idea", "e4")
    assert retry["line"] == "1.e4 e5"


@pytest.mark.asyncio
async def test_result_never_carries_the_engines_move_for_the_mover(monkeypatch):
    # The start search's pv (d4 d5) is the engine's own choice for the
    # mover: neither its moves nor a bestmove/pv field reach the model.
    tool, _bus = _tool(monkeypatch, chess.Board(), PlayOutRecord())
    out = await _step(tool, "e4, aim d4", "e4")
    assert not {"pv", "bestmove"} & set(out)
    assert "d4" not in out["line"] and "d5" not in out["line"]


@pytest.mark.asyncio
async def test_each_step_starts_a_fresh_search_panel(monkeypatch):
    tool, bus = _tool(monkeypatch, chess.Board(), PlayOutRecord())
    queue = await bus.subscribe()
    await _step(tool, "e4, aim d4", "e4")
    kinds = []
    while not queue.empty():
        kinds.append(queue.get_nowait().kind)
    assert kinds.count(EVT_ENGINE_SEARCH_START) == 1


@pytest.mark.asyncio
async def test_illegal_move_names_the_lines_position(monkeypatch):
    record = PlayOutRecord()
    board = chess.Board()
    tool, _bus = _tool(monkeypatch, board, record)
    await _step(tool, "e4, aim d4", "e4")
    out = await _step(tool, "e4, aim d4", "e5")  # Black's pawn blocks e5
    assert out["error"] == "illegal_move"
    after = chess.Board()
    for san in ("e4", "e5"):
        after.push_san(san)
    assert out["fen"] == after.fen()


@pytest.mark.parametrize("args", [{"move": "e4"}, {"idea": " ", "move": "e4"}, {"idea": "x"}])
@pytest.mark.asyncio
async def test_missing_idea_or_move_is_invalid_input(monkeypatch, args):
    calls: list = []
    tool, _bus = _tool(monkeypatch, chess.Board(), PlayOutRecord(), calls=calls)
    out = await tool(args, cancel_token=CancelToken())
    assert out["error"] == "invalid_input"
    assert calls == []


@pytest.mark.asyncio
async def test_mating_move_ends_the_line(monkeypatch):
    # 1.f3 e5 2.g4: ...Qh4 mates. The mated position needs no search.
    board = chess.Board()
    for san in ("f3", "e5", "g4"):
        board.push_san(san)
    calls: list = []
    script = {(board.epd(), None): (500, ["d8h4"])}
    tool, _bus = _tool(monkeypatch, board, PlayOutRecord(), script=script, calls=calls)
    out = await _step(tool, "Qh4 mate", "Qh4")
    assert out["reply"] is None
    assert out["line"] == "2...Qh4#"
    assert out["eval_after"] == {"score_text": "checkmate"}
    assert "failed" not in out
    assert calls == [(board.epd(), None)]
    over = await _step(tool, "Qh4 mate", "Kd8")
    assert over["error"] == "line_over"


def test_record_ranks_first_moves_by_eval_after():
    record = PlayOutRecord()
    e4, d4, nf3 = (chess.Move.from_uci(u) for u in ("e2e4", "d2d4", "g1f3"))
    assert record.best_first_move() is None
    record.note_first_move(e4, 10)
    record.note_first_move(nf3, None)
    record.note_first_move(d4, 40)
    record.note_first_move(d4, 5)  # a worse retry keeps the best eval
    assert record.best_first_move() == d4
    record.clear()
    assert not record.any_played_out()
