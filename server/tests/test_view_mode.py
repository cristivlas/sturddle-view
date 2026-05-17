"""View mode behavior on HumanVsEngine: navigation, play-from-here exit,
gating of play-mode operations, autosave suppression."""
from __future__ import annotations

from unittest.mock import AsyncMock

import chess
import pytest

from sturddle_view.config import Settings
from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine, TimeControl, ViewModeParams


class _StubEngine:
    def send_line(self, _line: str) -> None:
        pass

    async def quit(self) -> None:
        return None


@pytest.fixture
def hve(tmp_path):
    settings = Settings()
    settings.pgn_autosave = True
    settings.pgn_dir = tmp_path
    bus = EventBus()
    h = HumanVsEngine(engine_path="/nonexistent", bus=bus, settings=settings)

    async def fake_ensure_engine():
        h._engine = _StubEngine()
        return h._engine

    h._ensure_engine = fake_ensure_engine
    h._engine_to_move = AsyncMock()
    return h, tmp_path


async def test_enter_view_mode_lands_at_first_ply(hve):
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5", "g1f3"],
        clock_history=None,
    ))
    assert h._viewing is True
    assert h._view_cursor == 0
    assert h._board.fen() == chess.STARTING_FEN


async def test_view_navigation_back_forward_first_last(hve):
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5", "g1f3"],
        clock_history=None,
    ))
    # Land at 0; jump to last to exercise back/forward from a non-boundary.
    await h.view_last()
    await h.view_back()
    assert h._view_cursor == 2
    assert h._board.fen().startswith("rnbqkbnr/pppp1ppp/8/4p3/4P3/8")  # after e5
    await h.view_first()
    assert h._view_cursor == 0
    assert h._board.fen() == chess.STARTING_FEN
    await h.view_forward()
    assert h._view_cursor == 1
    await h.view_last()
    assert h._view_cursor == 3


async def test_view_navigation_clamps_at_boundaries(hve):
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None, moves_uci=["e2e4", "e7e5"], clock_history=None,
    ))
    await h.view_first()
    await h.view_back()  # already at 0
    assert h._view_cursor == 0
    await h.view_last()
    await h.view_forward()  # already at last
    assert h._view_cursor == 2


async def test_play_modes_rejected_in_view(hve):
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None, moves_uci=["e2e4"], clock_history=None,
    ))
    with pytest.raises(RuntimeError, match="view mode"):
        await h.submit_move("e7e5")
    with pytest.raises(RuntimeError, match="view mode"):
        await h.takeback()
    with pytest.raises(RuntimeError, match="view mode"):
        await h.pause()
    with pytest.raises(RuntimeError, match="view mode"):
        await h.switch_sides()
    with pytest.raises(RuntimeError, match="view mode"):
        await h.resign()


async def test_no_autosave_in_view(hve):
    h, tmp_path = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5", "g1f3"],
        clock_history=None,
    ))
    # Navigate around — none of this should trigger an autosave write.
    await h.view_back()
    await h.view_first()
    await h.view_last()
    assert list(tmp_path.glob("*.pgn")) == []


async def test_play_from_here_seeds_new_game_at_cursor(hve):
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5", "g1f3", "b8c6"],
        clock_history=None,
    ))
    await h.view_last()
    await h.view_back()  # cursor at ply 3 (after Nf3 — Black to move)
    assert h._view_cursor == 3
    pre_view_id = h._game_id
    new_id = await h.play_from_here(tc=TimeControl(60, 0))
    assert h._viewing is False
    assert new_id != pre_view_id  # fresh game_id
    assert [m.uci() for m in h._board.move_stack] == ["e2e4", "e7e5", "g1f3"]
    # Side-to-move at the cursor was Black → human plays Black.
    assert h._human_white is False


async def test_play_from_here_at_last_ply_uses_imported_final_clocks(hve):
    """Round-trip with [%clk]: import -- no nav -- play_from_here at last
    ply with inherit_clocks=True must restore live clocks from the PGN."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "c7c5", "g1f3"],
        clock_history=[(None, None), (4 * 60 + 55, None), (4 * 60 + 55, 4 * 60 + 50)],
        final_white_time=4 * 60 + 48,
        final_black_time=4 * 60 + 50,
    ))
    await h.view_last()
    await h.play_from_here(tc=TimeControl(300, 0), inherit_clocks=True)
    assert h._white_time == 4 * 60 + 48
    assert h._black_time == 4 * 60 + 50


async def test_play_from_here_mid_game_derives_clocks_from_history(hve):
    """When cursor is mid-game, post-move-cursor clocks come from
    view_clock_history[cursor] (== pre-move-(cursor+1))."""
    h, _ = hve
    # 3-ply game: e4 (white@4:55) c5 (black@4:50) Nf3 (white@4:48).
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "c7c5", "g1f3"],
        clock_history=[(None, None), (4 * 60 + 55, None), (4 * 60 + 55, 4 * 60 + 50)],
        final_white_time=4 * 60 + 48,
        final_black_time=4 * 60 + 50,
    ))
    # Cursor at ply 2 (after c5 — White to move). Post-c5 clocks = pre-Nf3
    # snapshot = view_clock_history[2] = (4:55, 4:50).
    await h.view_last()
    await h.view_back()
    assert h._view_cursor == 2
    await h.play_from_here(tc=TimeControl(300, 0), inherit_clocks=True)
    assert h._white_time == 4 * 60 + 55
    assert h._black_time == 4 * 60 + 50


async def test_play_from_here_at_finished_position_keeps_view_mode(hve):
    """Bug regression: play-from-here on a checkmate cursor position must
    raise *and* leave the viewer state intact — not strand the user in a
    half-cleared state where neither view nor play is active."""
    h, _ = hve
    # Scholar's mate: 7 plies ending in #.
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5", "d1h5", "b8c6", "f1c4", "g8f6", "h5f7"],
        clock_history=None,
    ))
    await h.view_last()
    assert h._board.is_checkmate()
    with pytest.raises(RuntimeError, match="game is over|already over"):
        await h.play_from_here(tc=TimeControl(60, 0))
    # Critical: still in view mode.
    assert h._viewing is True
    assert len(h._view_full_moves) == 7
    # And view nav still works.
    await h.view_first()
    assert h._view_cursor == 0


async def test_view_nav_rejected_during_analysis(hve):
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None, moves_uci=["e2e4", "e7e5"], clock_history=None,
    ))
    h._analysis_mode = True  # simulate analysis on
    with pytest.raises(RuntimeError, match="analysis"):
        await h.view_back()
    with pytest.raises(RuntimeError, match="analysis"):
        await h.play_from_here(tc=TimeControl(60, 0))


async def test_flag_fall_does_not_trample_game_installed_during_publish(hve):
    """Race regression: _handle_flag_fall releases the lock between its
    two critical sections to publish the game_result event. If a new_game
    or enter_view_mode lands in that gap, the second section MUST NOT
    clear the freshly-installed _board / _game_id."""
    h, _ = hve
    await h.new_game(human_white=True, tc=TimeControl(60, 0))
    # Force flag-fall: zero white's clock; it's white's turn.
    h._white_time = 0.0

    # Substitute a publish that, on the game_result event (between the
    # two _handle_flag_fall lock sections), supersedes the game with a
    # fresh enter_view_mode call.
    real_publish = h._bus.publish
    superseded = {"done": False}

    async def racing_publish(evt):
        await real_publish(evt)
        if evt.kind == "game_result" and not superseded["done"]:
            superseded["done"] = True
            await h.enter_view_mode(ViewModeParams(
                start_fen=None, moves_uci=["d2d4"], clock_history=None,
            ))

    h._bus.publish = racing_publish

    await h._handle_flag_fall()

    # The new view-mode game must still be installed, not torn down by
    # the flag-fall's second section.
    assert superseded["done"] is True
    assert h._viewing is True
    assert h._board is not None
    assert h._game_id is not None
    assert [m.uci() for m in h._view_full_moves] == ["d2d4"]


async def test_enter_view_supersedes_active_play_game(hve):
    """Importing a PGN tears down the current play game (autosave preserves
    the prior PGN; future load-from-history will let the user resume it)."""
    h, tmp_path = hve
    await h.new_game(human_white=True, tc=TimeControl(60, 0))
    await h.submit_move("e2e4")
    play_pgn = list(tmp_path.glob("*.pgn"))
    assert len(play_pgn) == 1  # autosaved before view-mode entry

    await h.enter_view_mode(ViewModeParams(
        start_fen=None, moves_uci=["d2d4", "d7d5"], clock_history=None,
    ))
    assert h._viewing is True
    # The prior play autosave is still on disk.
    assert list(tmp_path.glob("*.pgn")) == play_pgn


# Threefold repetition sequence: Ng1-f3, Ng8-f6 x3 (10 plies).
_THREEFOLD_MOVES = [
    "g1f3", "g8f6", "f3g1", "f6g8",
    "g1f3", "g8f6", "f3g1", "f6g8",
    "g1f3", "g8f6",
]

# Fool's mate (shortest checkmate): 2 moves.
_FOOLS_MATE_MOVES = ["f2f3", "e7e5", "g2g4", "d8h4"]


async def test_view_payload_includes_result_from_pgn_headers_on_threefold(hve):
    """When a PGN with threefold-repetition result is loaded, the board_event
    view payload must carry result/termination from the headers, not board state
    (board.outcome() is None for claimable draws)."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=_THREEFOLD_MOVES,
        clock_history=None,
        pgn_result="1/2-1/2",
        pgn_termination="threefold_repetition",
    ))
    await h.view_last()
    evt = h._board_event()
    view = evt.payload["view"]
    assert view["game_over"] is True
    assert view["result"] == "1/2-1/2"
    assert view["termination"] == "threefold_repetition"


async def test_normal_termination_enriched_to_threefold(hve):
    """Termination 'normal' is replaced with 'threefold_repetition' when the
    final board position has a claimable threefold draw."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=_THREEFOLD_MOVES,
        clock_history=None,
        pgn_result="1/2-1/2",
        pgn_termination="normal",
    ))
    await h.view_last()
    evt = h._board_event()
    view = evt.payload["view"]
    assert view["termination"] == "threefold_repetition"
    assert view["result"] == "1/2-1/2"


async def test_view_payload_result_from_board_on_checkmate(hve):
    """Forced endings (checkmate) still derive result/termination from the
    board even without PGN headers."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=_FOOLS_MATE_MOVES,
        clock_history=None,
    ))
    await h.view_last()
    evt = h._board_event()
    view = evt.payload["view"]
    assert view["game_over"] is True
    assert view["result"] == "0-1"
    assert view["termination"] == "checkmate"


async def test_game_over_false_one_ply_before_checkmate(hve):
    """game_over must be False at the ply just before the mating move."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=_FOOLS_MATE_MOVES,
        clock_history=None,
    ))
    await h.view_goto(len(_FOOLS_MATE_MOVES) - 1)
    evt = h._board_event()
    assert evt.payload["view"]["game_over"] is False


async def test_game_over_false_at_mid_game_ply_despite_pgn_result(hve):
    """PGN result header must not mark game_over=True at non-terminal plies.
    Regression: result header previously disabled play-from-here for the whole game."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=_THREEFOLD_MOVES,
        clock_history=None,
        pgn_result="1/2-1/2",
        pgn_termination="threefold_repetition",
    ))
    # Navigate back to a mid-game ply -- board is not terminal there.
    await h.view_goto(4)
    evt = h._board_event()
    view = evt.payload["view"]
    assert view["game_over"] is False


# -- comment_nav tests -------------------------------------------------------
# 5-ply game; comments at plies 2 and 4 (1-based), root comment at ply 0.
_CN_MOVES = ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4"]
_CN_COMMENTS = [None, "Nice move.", None, "Strong reply.", None]
_CN_ROOT = "Opening remarks."


async def _cn_hve(hve, *, root=None):
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=_CN_MOVES,
        clock_history=None,
        comments=_CN_COMMENTS,
        root_comment=root,
    ))
    return h


async def test_comment_nav_from_start(hve):
    """At ply 0 with no root comment: prev=None, next=first commented ply."""
    h = await _cn_hve(hve)
    result = h._comment_nav(0)
    assert result == {"prev_comment": None, "next_comment": 2}


async def test_comment_nav_from_start_with_root(hve):
    """At ply 0 with root comment: prev=None (already at root), next=2."""
    h = await _cn_hve(hve, root=_CN_ROOT)
    result = h._comment_nav(0)
    assert result == {"prev_comment": None, "next_comment": 2}


async def test_comment_nav_before_first_comment(hve):
    """At ply 1 (no comment): prev=None, next=2."""
    h = await _cn_hve(hve)
    result = h._comment_nav(1)
    assert result == {"prev_comment": None, "next_comment": 2}


async def test_comment_nav_at_first_comment(hve):
    """At ply 2 (has comment): prev=None, next=4."""
    h = await _cn_hve(hve)
    result = h._comment_nav(2)
    assert result == {"prev_comment": None, "next_comment": 4}


async def test_comment_nav_between_comments(hve):
    """At ply 3 (no comment): prev=2, next=4."""
    h = await _cn_hve(hve)
    result = h._comment_nav(3)
    assert result == {"prev_comment": 2, "next_comment": 4}


async def test_comment_nav_at_last_comment(hve):
    """At ply 4 (has comment): prev=2, next=None."""
    h = await _cn_hve(hve)
    result = h._comment_nav(4)
    assert result == {"prev_comment": 2, "next_comment": None}


async def test_comment_nav_at_end(hve):
    """At ply 5 (end, no comment): prev=4, next=None."""
    h = await _cn_hve(hve)
    result = h._comment_nav(5)
    assert result == {"prev_comment": 4, "next_comment": None}


async def test_comment_nav_root_as_prev(hve):
    """Root comment is reachable as prev from ply 1."""
    h = await _cn_hve(hve, root=_CN_ROOT)
    result = h._comment_nav(1)
    assert result["prev_comment"] == 0


async def test_comment_nav_no_comments(hve):
    """Game with no comments at all: both directions None."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=_CN_MOVES,
        clock_history=None,
    ))
    result = h._comment_nav(3)
    assert result == {"prev_comment": None, "next_comment": None}


async def test_comment_nav_returned_by_view_goto(hve):
    """view_goto returns comment nav when include_comment_nav=True."""
    h = await _cn_hve(hve)
    result = await h.view_goto(3, include_comment_nav=True)
    assert result == {"prev_comment": 2, "next_comment": 4}


async def test_comment_nav_not_returned_by_default(hve):
    """view_goto returns empty dict by default (no overhead)."""
    h = await _cn_hve(hve)
    result = await h.view_goto(3)
    assert result == {}


async def test_comment_nav_via_convenience_methods(hve):
    """view_first/back/forward/last all forward include_comment_nav."""
    h = await _cn_hve(hve)
    await h.view_goto(3)  # land at ply 3
    r = await h.view_back(include_comment_nav=True)
    assert r == {"prev_comment": None, "next_comment": 4}  # now at ply 2
    r = await h.view_forward(include_comment_nav=True)
    assert r == {"prev_comment": 2, "next_comment": 4}  # now at ply 3
    r = await h.view_first(include_comment_nav=True)
    assert r == {"prev_comment": None, "next_comment": 2}  # now at ply 0
    r = await h.view_last(include_comment_nav=True)
    assert r == {"prev_comment": 4, "next_comment": None}  # now at ply 5
