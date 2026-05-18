"""R9 / P11 -- unit tests for _board_event payload extraction.

Targets the helpers (_view_payload, _view_moves_san, _opening_payload) plus
the post-transition guarantee that play_from_here scrubs view payload from
the subsequent board_update.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import chess
import pytest

from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine, TimeControl, ViewModeParams

TC = TimeControl(300.0, 0.0)
SEED_MOVES = ["e2e4", "e7e5"]
VIEW_MOVES = ["e2e4", "e7e5", "g1f3", "b8c6"]


class _StubEngine:
    def send_line(self, _line: str) -> None:
        pass

    async def quit(self) -> None:
        return None


@pytest.fixture
def hve():
    h = HumanVsEngine(engine_path="/nonexistent", bus=EventBus())

    async def fake_ensure_engine():
        h._engine = _StubEngine()
        return h._engine

    h._ensure_engine = fake_ensure_engine
    h._engine_to_move = AsyncMock()
    return h


async def _enter_view(hve, moves=VIEW_MOVES, **kwargs):
    await hve.enter_view_mode(ViewModeParams(
        start_fen=kwargs.pop("start_fen", None),
        moves_uci=[chess.Move.from_uci(u).uci() for u in moves],
        clock_history=kwargs.pop("clock_history", None),
        **kwargs,
    ))


async def test_play_payload_omits_view_field(hve):
    await hve.new_game(human_white=True, tc=TC, start_moves_uci=SEED_MOVES)
    event = hve._board_event()
    assert event.payload["view"] is None
    assert event.payload["human_white"] is True


async def test_view_payload_includes_cursor_and_total_plies(hve):
    await _enter_view(hve)
    await hve.view_goto(2)
    view = hve._view_payload()
    assert view["cursor"] == 2
    assert view["total_plies"] == len(VIEW_MOVES)


async def test_view_payload_eval_at_cursor(hve):
    """eval_history[cursor-1] surfaces at the cursor; cursor==0 has no eval."""
    await _enter_view(hve, eval_history=[{"cp": 30}, {"cp": -10}, {"mate": 5}, None])
    await hve.view_goto(0)
    assert hve._view_payload()["eval"] is None
    await hve.view_goto(1)
    assert hve._view_payload()["eval"] == {"cp": 30}
    await hve.view_goto(3)
    assert hve._view_payload()["eval"] == {"mate": 5}
    assert hve._view_payload()["has_eval"] is True


async def test_view_payload_comment_at_cursor(hve):
    """Comments[cursor-1] surfaces at cursor>=1; cursor==0 surfaces root comment."""
    await _enter_view(
        hve,
        root_comment="game start",
        comments=["after 1.e4", None, "after 2.Nf3", None],
    )
    await hve.view_goto(0)
    assert hve._view_payload()["comment"] == "game start"
    await hve.view_goto(1)
    assert hve._view_payload()["comment"] == "after 1.e4"
    await hve.view_goto(3)
    assert hve._view_payload()["comment"] == "after 2.Nf3"
    assert hve._view_payload()["has_comment"] is True


async def test_view_payload_game_over_on_pgn_result(hve):
    """When PGN carries a non-* result and cursor is at the last ply,
    game_over=True even without a board-derived outcome."""
    await _enter_view(hve, pgn_result="1-0", pgn_termination="normal")
    await hve.view_goto(len(VIEW_MOVES))  # cursor at last ply
    view = hve._view_payload()
    assert view["game_over"] is True
    assert view["result"] == "1-0"


async def test_view_payload_threefold_termination_from_can_claim(hve):
    """PGN termination='normal' + can_claim_threefold_repetition() ->
    surfaces 'threefold_repetition' as the termination string."""
    repeat_moves = ["g1f3", "g8f6", "f3g1", "f6g8"] * 3
    await _enter_view(hve, moves=repeat_moves, pgn_result="1/2-1/2", pgn_termination="normal")
    await hve.view_goto(len(repeat_moves))
    view = hve._view_payload()
    assert view["termination"] == "threefold_repetition"


async def test_tablebase_field_only_present_when_prober_set(hve):
    """halfmove_clock is always present; probe-derived fields only when
    a tablebase prober is installed (none in this test)."""
    await hve.new_game(human_white=True, tc=TC, start_moves_uci=SEED_MOVES)
    event = hve._board_event()
    tb = event.payload["tablebase"]
    assert "halfmove_clock" in tb
    # Without a prober, no wdl/dtz/best_move_uci.
    assert "wdl" not in tb
    assert "dtz" not in tb


async def test_opening_field_null_for_imported_games(hve):
    """A non-startpos imported game can't be classified by the opening book."""
    from sturddle_view.openings import OpeningBook

    hve._openings = OpeningBook.load()
    sicilian = "rnbqkbnr/pp1ppppp/8/2p5/4P3/5N2/PPPP1PPP/RNBQKBNR b KQkq - 1 2"
    await hve.new_game(human_white=True, tc=TC, start_fen=sicilian)
    assert hve._opening_payload() is None
    assert hve._board_event().payload["opening"] is None


async def test_view_payload_scrubbed_after_play_from_here(hve):
    """play_from_here transitions view -> play. The very next board_update
    must have view=None even though view-mode helpers were just live."""
    await _enter_view(hve)
    await hve.view_goto(2)
    # Confirm we WERE in view mode.
    assert hve._board_event().payload["view"] is not None
    await hve.play_from_here(tc=TC)
    event = hve._board_event()
    assert event.payload["view"] is None
    assert event.payload["human_white"] is not None  # play-mode field returns
