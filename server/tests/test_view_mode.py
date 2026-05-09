"""View mode behavior on HumanVsEngine: navigation, play-from-here exit,
gating of play-mode operations, autosave suppression."""
from __future__ import annotations

from unittest.mock import AsyncMock

import chess
import pytest

from sturddle_view.config import Settings
from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine, TimeControl


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


async def test_enter_view_mode_lands_at_last_ply(hve):
    h, _ = hve
    await h.enter_view_mode(
        start_fen=None,
        moves_uci=["e2e4", "e7e5", "g1f3"],
        clock_history=None,
    )
    assert h._viewing is True
    assert h._view_cursor == 3
    assert h._board.fen().startswith("rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2")


async def test_view_navigation_back_forward_first_last(hve):
    h, _ = hve
    await h.enter_view_mode(
        start_fen=None,
        moves_uci=["e2e4", "e7e5", "g1f3"],
        clock_history=None,
    )
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
    await h.enter_view_mode(
        start_fen=None, moves_uci=["e2e4", "e7e5"], clock_history=None,
    )
    await h.view_first()
    await h.view_back()  # already at 0
    assert h._view_cursor == 0
    await h.view_last()
    await h.view_forward()  # already at last
    assert h._view_cursor == 2


async def test_play_modes_rejected_in_view(hve):
    h, _ = hve
    await h.enter_view_mode(
        start_fen=None, moves_uci=["e2e4"], clock_history=None,
    )
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
    await h.enter_view_mode(
        start_fen=None,
        moves_uci=["e2e4", "e7e5", "g1f3"],
        clock_history=None,
    )
    # Navigate around — none of this should trigger an autosave write.
    await h.view_back()
    await h.view_first()
    await h.view_last()
    assert list(tmp_path.glob("*.pgn")) == []


async def test_play_from_here_seeds_new_game_at_cursor(hve):
    h, _ = hve
    await h.enter_view_mode(
        start_fen=None,
        moves_uci=["e2e4", "e7e5", "g1f3", "b8c6"],
        clock_history=None,
    )
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
    await h.enter_view_mode(
        start_fen=None,
        moves_uci=["e2e4", "c7c5", "g1f3"],
        clock_history=[(None, None), (4 * 60 + 55, None), (4 * 60 + 55, 4 * 60 + 50)],
        final_white_time=4 * 60 + 48,
        final_black_time=4 * 60 + 50,
    )
    await h.play_from_here(tc=TimeControl(300, 0), inherit_clocks=True)
    assert h._white_time == 4 * 60 + 48
    assert h._black_time == 4 * 60 + 50


async def test_play_from_here_mid_game_derives_clocks_from_history(hve):
    """When cursor is mid-game, post-move-cursor clocks come from
    view_clock_history[cursor] (== pre-move-(cursor+1))."""
    h, _ = hve
    # 3-ply game: e4 (white@4:55) c5 (black@4:50) Nf3 (white@4:48).
    await h.enter_view_mode(
        start_fen=None,
        moves_uci=["e2e4", "c7c5", "g1f3"],
        clock_history=[(None, None), (4 * 60 + 55, None), (4 * 60 + 55, 4 * 60 + 50)],
        final_white_time=4 * 60 + 48,
        final_black_time=4 * 60 + 50,
    )
    # Cursor at ply 2 (after c5 — White to move). Post-c5 clocks = pre-Nf3
    # snapshot = view_clock_history[2] = (4:55, 4:50).
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
    await h.enter_view_mode(
        start_fen=None,
        moves_uci=["e2e4", "e7e5", "d1h5", "b8c6", "f1c4", "g8f6", "h5f7"],
        clock_history=None,
    )
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
    await h.enter_view_mode(
        start_fen=None, moves_uci=["e2e4", "e7e5"], clock_history=None,
    )
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
            await h.enter_view_mode(
                start_fen=None, moves_uci=["d2d4"], clock_history=None,
            )

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

    await h.enter_view_mode(
        start_fen=None, moves_uci=["d2d4", "d7d5"], clock_history=None,
    )
    assert h._viewing is True
    # The prior play autosave is still on disk.
    assert list(tmp_path.glob("*.pgn")) == play_pgn
