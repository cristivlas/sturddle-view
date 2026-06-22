"""View mode behavior on HumanVsEngine: navigation, play-from-here exit,
gating of play-mode operations, autosave suppression."""
from __future__ import annotations

from unittest.mock import AsyncMock

import chess
import pytest

from sturddle_view.config import Settings
from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine, TimeControl, ViewModeParams
from sturddle_view.play.mode import Mode, ModeConflictError


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


_UUID_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


async def test_new_game_assigns_full_uuid_game_id(hve):
    import re
    h, _ = hve
    gid = await h.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    assert re.match(_UUID_RE, gid)
    assert h._game_id == gid


async def test_enter_view_mode_assigns_full_uuid_game_id(hve):
    import re
    h, _ = hve
    gid = await h.enter_view_mode(ViewModeParams(
        start_fen=None, moves_uci=["e2e4"], clock_history=None,
    ))
    assert re.match(_UUID_RE, gid)


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
    with pytest.raises(ModeConflictError):
        await h.submit_move("e7e5")
    with pytest.raises(ModeConflictError):
        await h.takeback()
    with pytest.raises(ModeConflictError):
        await h.pause()
    with pytest.raises(ModeConflictError):
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


async def test_play_from_here_preserves_player_name(hve):
    """Regression: forking via play_from_here must carry self._player_name
    into the new game. The internal new_game call previously omitted
    player_name, silently resetting it to the default."""
    h, _ = hve
    await h.new_game(human_white=True, tc=TimeControl(60, 0), player_name="Alice")
    # 2-ply history: White to move at last ply, so human plays White after fork.
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5"],
        clock_history=None,
    ))
    await h.view_last()
    await h.play_from_here(tc=TimeControl(60, 0))
    assert h._player_name == "Alice"
    assert h._human_white is True
    summary = h.play_game_summary()
    assert summary is not None
    assert summary["white"] == "Alice"


async def test_play_from_here_player_name_arg_overrides_session(hve):
    """Regression: importing a game on a fresh session leaves _player_name
    at the default ("Human"); the play-from-here caller must be able to
    inject the configured name (from client localStorage) so the new play
    game's PGN headers and clock label reflect the user's actual name."""
    h, _ = hve
    # Fresh session: _player_name is the default. Import a PGN and fork.
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5"],
        clock_history=None,
    ))
    await h.view_last()
    await h.play_from_here(tc=TimeControl(60, 0), player_name="Alice")
    assert h._player_name == "Alice"
    # Cursor lands after Black's reply -- White to move -- human plays White.
    assert h._human_white is True
    summary = h.play_game_summary()
    assert summary is not None
    assert summary["white"] == "Alice"


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
    assert h._clock.white_time == 4 * 60 + 48
    assert h._clock.black_time == 4 * 60 + 50


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
    assert h._clock.white_time == 4 * 60 + 55
    assert h._clock.black_time == 4 * 60 + 50


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


async def test_play_from_here_engine_spawn_failure_keeps_view_mode(hve):
    """Bug regression: a failed engine launch must leave the viewer intact,
    not strand the user in PLAY mode with the view payload wiped (which then
    rejects view nav with 'VIEW_GOTO not allowed in PLAY mode')."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5", "g1f3", "b8c6"],
        clock_history=None,
    ))
    await h.view_last()
    await h.view_back()  # cursor mid-game (ply 3)
    pre_id = h._game_id

    async def boom():
        raise FileNotFoundError("/nonexistent")
    h._ensure_engine = boom

    with pytest.raises(FileNotFoundError):
        await h.play_from_here(tc=TimeControl(60, 0))

    # Coherent rollback: still VIEWING, same game, payload + cursor intact.
    assert h._viewing is True
    assert h._mode is Mode.VIEWING
    assert h._game_id == pre_id
    assert len(h._view_full_moves) == 4
    assert h._view_cursor == 3
    # View nav still works (would raise if stranded in PLAY with view wiped).
    await h.view_first()
    assert h._view_cursor == 0


async def test_view_nav_rejected_during_analysis(hve):
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None, moves_uci=["e2e4", "e7e5"], clock_history=None,
    ))
    h._mode = Mode.ANALYZING
    with pytest.raises(ModeConflictError):
        await h.view_back()
    with pytest.raises(ModeConflictError):
        await h.play_from_here(tc=TimeControl(60, 0))


async def test_viewing_flag_true_while_analyzing_from_view(hve):
    """Regression: _viewing must stay True when analysis is entered from view
    mode, so _clock_event/_board_event keep emitting the view-mode payload."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None, moves_uci=["e2e4", "e7e5"], clock_history=None,
    ))
    assert h._viewing is True
    h._mode = Mode.ANALYZING
    h._pre_analysis_mode = Mode.VIEWING
    assert h._viewing is True  # must stay True -- not flip to False
    assert h._analysis_mode is True

    # When entered from play (PAUSED), _viewing must be False.
    h._pre_analysis_mode = Mode.PAUSED
    assert h._viewing is False


async def test_flag_fall_does_not_trample_game_installed_during_publish(hve):
    """Race regression: _handle_flag_fall releases the lock between its
    two critical sections to publish the game_result event. If a new_game
    or enter_view_mode lands in that gap, the second section MUST NOT
    clear the freshly-installed _board / _game_id."""
    h, _ = hve
    await h.new_game(human_white=True, tc=TimeControl(60, 0))
    # Force flag-fall: zero white's clock; it's white's turn.
    h._clock.white_time = 0.0

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


# ---------------------------------------------------------------------------
# play_from_here carries imported comments into the forked play game
# (Phase 2 of the annotation-edit work: view-mode commentary survives the
# view -> play fork so the eventual recents save / PGN export retain it).
# ---------------------------------------------------------------------------


async def test_play_from_here_carries_root_and_per_ply_comments(hve):
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5", "g1f3", "b8c6"],
        clock_history=None,
        comments=["c1", None, "c3", None],
        root_comment="pre-game thoughts",
    ))
    await h.view_last()
    await h.view_back()  # cursor at ply 3
    await h.play_from_here(tc=TimeControl(60, 0))
    assert h._play_root_comment == "pre-game thoughts"
    assert h._play_comments == ["c1", None, "c3"]


async def test_play_game_comments_helper(hve):
    """Exposes _play_comments + _play_root_comment so /game/view/start
    can re-seed a view game with them, closing the round trip."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5"],
        clock_history=None,
        comments=["c1", "c2"],
        root_comment="pre",
    ))
    await h.view_last()
    await h.play_from_here(tc=TimeControl(60, 0))
    c, root = h.play_game_comments()
    assert c == ["c1", "c2"]
    assert root == "pre"


async def test_view_game_comments_returns_none_on_fresh_hve(hve):
    """Fresh HVE (no PGN loaded yet) -> accessor returns (None, None).
    Used by the AI-analysis kick to detect "no annotations to inject"."""
    h, _ = hve
    comments, root = h.view_game_comments()
    assert comments is None
    assert root is None


async def test_view_game_comments_returns_loaded_pgn_data(hve):
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5"],
        clock_history=None,
        comments=["sharp", "Petrov"],
        root_comment="from the 1972 candidates",
    ))
    comments, root = h.view_game_comments()
    assert comments == ["sharp", "Petrov"]
    assert root == "from the 1972 candidates"


async def test_view_game_comments_returns_copy_not_alias(hve):
    """Mirror of play_game_comments: the returned list must be a copy so
    callers (AI kick capping) can mutate it without corrupting HVE state."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5"],
        clock_history=None,
        comments=["c1", "c2"],
        root_comment="r",
    ))
    comments, _ = h.view_game_comments()
    assert comments is not None
    comments[0] = "MUTATED"
    again, _ = h.view_game_comments()
    assert again == ["c1", "c2"]


async def test_play_from_here_no_view_comments_leaves_play_side_none(hve):
    """When the imported PGN had no commentary, no synthesis happens."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5"],
        clock_history=None,
    ))
    await h.view_last()
    await h.play_from_here(tc=TimeControl(60, 0))
    assert h._play_comments is None
    assert h._play_root_comment is None


async def test_play_from_here_at_ply_zero_keeps_only_root_comment(hve):
    """Cursor at ply 0 -> seed_comments is an empty slice; the forked
    play game still inherits the root comment but has no per-ply ones."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5"],
        clock_history=None,
        comments=["c1", "c2"],
        root_comment="pre-game",
    ))
    # cursor stays at 0 (no view_last / view_forward)
    await h.play_from_here(tc=TimeControl(60, 0))
    assert h._play_comments is None  # collapsed because slice was empty
    assert h._play_root_comment == "pre-game"


async def test_take_back_pops_play_comments_in_lockstep(hve):
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5", "g1f3", "b8c6"],
        clock_history=None,
        comments=["c1", "c2", "c3", "c4"],
    ))
    await h.view_last()
    await h.play_from_here(tc=TimeControl(60, 0))
    # After play_from_here at last ply: 4 plies on the board, 4 comments.
    assert len(h._board.move_stack) == 4
    assert h._play_comments == ["c1", "c2", "c3", "c4"]
    # Takeback drops engine reply + human's last (2 plies).
    # Side-to-move at ply 4 is White; human side is determined at fork.
    # We pop two if human is to move; otherwise one. Don't assume; just
    # check post-state is consistent.
    pre_n = len(h._board.move_stack)
    await h.takeback()
    post_n = len(h._board.move_stack)
    assert post_n < pre_n
    assert len(h._play_comments) == post_n


# ---------------------------------------------------------------------------
# Annotation editing via commit_edit (Phase 5).
# Edit-mode-only mutation; FEN unchanged + apply_comment=True path.
# ---------------------------------------------------------------------------


async def _enter_edit_at_ply(h, ply):
    """Helper: enter view + nav to ply + enter edit (cursor frozen at ply)."""
    if ply > 0:
        await h.view_goto(ply)
    return await h.enter_edit_mode()


async def test_commit_edit_annotation_sets_comment_at_entry_ply(hve):
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5", "g1f3"],
        clock_history=None,
    ))
    fen = await _enter_edit_at_ply(h, 2)  # cursor at ply 2 (after e5)
    result = await h.commit_edit(
        fen, apply_comment=True, comment_text="Best by test.",
    )
    assert result["changed"] == "comment"
    assert h._view_comments == [None, "Best by test.", None]
    # Cursor restored to the entry ply.
    assert h._view_cursor == 2


async def test_commit_edit_annotation_at_root_ply_zero(hve):
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4"],
        clock_history=None,
    ))
    fen = await _enter_edit_at_ply(h, 0)  # cursor at root
    result = await h.commit_edit(
        fen, apply_comment=True, comment_text="Pre-game thoughts.",
    )
    assert result["changed"] == "comment"
    assert h._view_root_comment == "Pre-game thoughts."


async def test_commit_edit_annotation_empty_text_deletes(hve):
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5"],
        clock_history=None,
        comments=["existing", None],
    ))
    fen = await _enter_edit_at_ply(h, 1)
    result = await h.commit_edit(fen, apply_comment=True, comment_text="")
    assert result["changed"] == "comment"
    # Comments list collapsed to None since no entries remain.
    assert h._view_comments is None


async def test_commit_edit_annotation_no_change_returns_none_branch(hve):
    """User opened the modal, didn't change anything (text matches current)
    -> changed='none', no recents disturbance."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4"],
        clock_history=None,
        comments=["same"],
    ))
    fen = await _enter_edit_at_ply(h, 1)
    result = await h.commit_edit(fen, apply_comment=True, comment_text="same")
    assert result["changed"] == "none"
    assert h._view_comments == ["same"]


async def test_commit_edit_annotation_whitespace_only_deletes(hve):
    """Empty after strip() -> delete, just like fully-empty text."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4"],
        clock_history=None,
        comments=["existing"],
    ))
    fen = await _enter_edit_at_ply(h, 1)
    result = await h.commit_edit(fen, apply_comment=True, comment_text="   \t\n")
    assert result["changed"] == "comment"
    assert h._view_comments is None


async def test_commit_edit_annotation_preserves_game_id(hve):
    """The whole point: annotation edit does NOT mint a new game_id."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4"],
        clock_history=None,
    ))
    pre_id = h._game_id
    fen = await _enter_edit_at_ply(h, 1)
    await h.commit_edit(fen, apply_comment=True, comment_text="x")
    assert h._game_id == pre_id


async def test_commit_edit_annotation_does_not_mutate_original_text(hve):
    """_view_original_text holds the import bytes -- annotation edits
    must NOT touch it."""
    h, _ = hve
    raw = (
        '[Event "T"]\n[White "A"]\n[Black "B"]\n[Result "*"]\n\n'
        '1. e4 *\n\n'
    )
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4"],
        clock_history=None,
        view_original_text=raw,
    ))
    fen = await _enter_edit_at_ply(h, 1)
    await h.commit_edit(fen, apply_comment=True, comment_text="annotated")
    assert h._view_original_text == raw


async def test_commit_edit_annotation_updates_hash(hve):
    """Content changed -> _view_hash recomputed from the regen'd PGN."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4"],
        clock_history=None,
        view_hash="deadbeef" * 8,
    ))
    fen = await _enter_edit_at_ply(h, 1)
    result = await h.commit_edit(fen, apply_comment=True, comment_text="new")
    assert h._view_hash == result["hash"]
    assert h._view_hash != "deadbeef" * 8


async def test_commit_edit_fen_change_ignores_apply_comment(hve):
    """FEN changed -> game truncated; the annotation request has no valid
    target ply, so it's silently dropped."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4"],
        clock_history=None,
    ))
    await h.enter_edit_mode()
    new_fen = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    # different EPD from the post-e4 position
    result = await h.commit_edit(
        new_fen, apply_comment=True, comment_text="ignored",
    )
    assert result["changed"] == "fen"
    # New view game has no comments.
    assert h._view_comments is None
    assert h._view_root_comment is None


async def test_commit_edit_unchanged_no_annotation_returns_none(hve):
    """Existing behavior preserved: FEN same, apply_comment=False -> 'none'."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4"],
        clock_history=None,
    ))
    fen = await h.enter_edit_mode()
    result = await h.commit_edit(fen)  # no apply_comment
    assert result["changed"] == "none"


async def test_play_to_view_via_edit_round_trip_preserves_comments(hve):
    """Repro for the user-reported bug:
       import w/ comments -> play_from_here -> enter_view_mode (the play
       -> view flip used by /game/view/start) -> enter edit -> cancel.
    The restored view must still have the imported comments."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5", "g1f3"],
        clock_history=None,
        comments=["c1", "c2", "c3"],
        root_comment="pre",
    ))
    await h.view_last()
    await h.play_from_here(tc=TimeControl(60, 0))
    # Simulate /game/view/start: snapshot play state + comments, enter view.
    start_fen, moves, _, _, _, _ = h.play_game_snapshot()
    comments, root = h.play_game_comments()
    await h.enter_view_mode(ViewModeParams(
        start_fen=start_fen,
        moves_uci=moves,
        clock_history=None,
        comments=comments,
        root_comment=root,
    ))
    assert h._view_comments == ["c1", "c2", "c3"]
    assert h._view_root_comment == "pre"
    # Now enter edit then cancel -- the post-cancel view must still see them.
    await h.enter_edit_mode()
    await h.cancel_edit()
    assert h._view_comments == ["c1", "c2", "c3"]
    assert h._view_root_comment == "pre"


async def test_play_from_here_pgn_export_preserves_comments(hve):
    """End-to-end: import with comments -> play_from_here at last ply ->
    PGN built via the play-mode path carries the seeded user prose."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5", "g1f3"],
        clock_history=None,
        comments=["king pawn", "symmetric", "knight develops"],
        root_comment="study line",
    ))
    await h.view_last()
    await h.play_from_here(tc=TimeControl(60, 0))
    # _build_play_game_pgn is the shared workhorse driven by autosave,
    # game-end recents save, and download. Exercises the same path that
    # users see on game-end.
    built = h._build_play_game_pgn(result="0-1", termination="resignation")
    assert built is not None
    pgn_text, _white, _black = built
    assert "study line" in pgn_text
    assert "king pawn" in pgn_text
    assert "symmetric" in pgn_text
    assert "knight develops" in pgn_text


# ---------------------------------------------------------------------------
# get_pgn_text divergence gate: after an annotation edit, the export must
# re-serialize structured state instead of returning the original text.
# ---------------------------------------------------------------------------


_RAW_NO_COMMENTS = (
    '[Event "T"]\n[Site "T"]\n[Date "2026.05.24"]\n'
    '[Round "-"]\n[White "A"]\n[Black "B"]\n[Result "*"]\n\n'
    '1. e4 e5 *\n\n'
)


async def test_get_pgn_text_returns_original_when_unedited(hve):
    """Optimization preserved: unedited view returns _view_original_text verbatim."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5"],
        clock_history=None,
        view_original_text=_RAW_NO_COMMENTS,
    ))
    result = h.get_pgn_text()
    assert result is not None
    pgn_text, _filename = result
    assert pgn_text == _RAW_NO_COMMENTS


async def test_get_pgn_text_reserializes_after_annotation_edit(hve):
    """The bug fix: annotation via commit_edit must appear in the exported PGN,
    not get masked by the verbatim-original short-circuit."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5"],
        clock_history=None,
        view_original_text=_RAW_NO_COMMENTS,
    ))
    fen = await _enter_edit_at_ply(h, 2)
    await h.commit_edit(fen, apply_comment=True, comment_text="user note here")
    result = h.get_pgn_text()
    assert result is not None
    pgn_text, _filename = result
    assert pgn_text != _RAW_NO_COMMENTS
    assert "user note here" in pgn_text


async def test_get_pgn_text_reserializes_after_root_annotation_edit(hve):
    """Root-comment edit (ply 0) must also flip the divergence flag."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4"],
        clock_history=None,
        view_original_text=_RAW_NO_COMMENTS,
    ))
    fen = await _enter_edit_at_ply(h, 0)
    await h.commit_edit(fen, apply_comment=True, comment_text="pre-game note")
    result = h.get_pgn_text()
    assert result is not None
    pgn_text, _filename = result
    assert "pre-game note" in pgn_text


async def test_get_pgn_text_stays_diverged_after_un_edit(hve):
    """Once edited, the flag is sticky: round-tripping back to original text
    leaves the export path on the regen branch (structurally equal but not
    necessarily byte-identical to the original)."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4"],
        clock_history=None,
        view_original_text=_RAW_NO_COMMENTS,
    ))
    # Add then delete the comment.
    fen = await _enter_edit_at_ply(h, 1)
    await h.commit_edit(fen, apply_comment=True, comment_text="temporary")
    fen = await _enter_edit_at_ply(h, 1)
    await h.commit_edit(fen, apply_comment=True, comment_text="")
    assert h._view_edited is True
    result = h.get_pgn_text()
    assert result is not None
    pgn_text, _filename = result
    assert "temporary" not in pgn_text


async def test_cancel_edit_preserves_edited_flag(hve):
    """enter_edit -> cancel_edit on an already-edited view must not clear
    the flag (cancel restores the pre-edit snapshot wholesale)."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4"],
        clock_history=None,
        view_original_text=_RAW_NO_COMMENTS,
    ))
    fen = await _enter_edit_at_ply(h, 1)
    await h.commit_edit(fen, apply_comment=True, comment_text="first edit")
    assert h._view_edited is True
    # Re-enter edit and cancel.
    await h.enter_edit_mode()
    await h.cancel_edit()
    assert h._view_edited is True
    pgn_text, _filename = h.get_pgn_text()
    assert "first edit" in pgn_text


async def test_no_op_annotation_does_not_set_edited_flag(hve):
    """Opening the modal and committing the same text -> not an edit."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4"],
        clock_history=None,
        comments=["same"],
        view_original_text=_RAW_NO_COMMENTS,
    ))
    fen = await _enter_edit_at_ply(h, 1)
    result = await h.commit_edit(fen, apply_comment=True, comment_text="same")
    assert result["changed"] == "none"
    assert h._view_edited is False
    pgn_text, _filename = h.get_pgn_text()
    assert pgn_text == _RAW_NO_COMMENTS


async def test_new_view_clears_edited_flag(hve):
    """Loading a new game must reset the divergence flag."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4"],
        clock_history=None,
        view_original_text=_RAW_NO_COMMENTS,
    ))
    fen = await _enter_edit_at_ply(h, 1)
    await h.commit_edit(fen, apply_comment=True, comment_text="stale")
    assert h._view_edited is True
    # Load a different game.
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["d2d4"],
        clock_history=None,
        view_original_text=_RAW_NO_COMMENTS,
    ))
    assert h._view_edited is False


# ---- no-fork resume (/view/start suspend -> /view/resume-play) -----------

async def _suspend_live_play(h):
    """Mimic /view/start: snapshot the live play game into a suspended view."""
    sf, mu, ch, wt, bt, _eh = h.play_game_snapshot()
    return await h.enter_view_mode(
        ViewModeParams(
            start_fen=sf, moves_uci=mu, clock_history=ch or None,
            final_white_time=wt, final_black_time=bt,
        ),
        suspend_play=True,
    )


async def test_resume_play_restores_same_game_and_sides(hve):
    """resume_play returns to the SAME game (no fork): original game_id,
    human color, and moves preserved -- unlike play_from_here."""
    h, _ = hve
    h._engine_to_move = AsyncMock()
    play_id = await h.new_game(human_white=False, tc=TimeControl(60, 0))
    # Two plies -> White (the engine) is to move again at the last ply.
    for u in ("e2e4", "e7e5"):
        h._board.push(chess.Move.from_uci(u))
        h._eval_history.append(None)
    view_id = await _suspend_live_play(h)
    assert h._suspended_play is not None
    h._engine_to_move.reset_mock()
    resumed_id = await h.resume_play()
    assert h._viewing is False
    assert h._mode is Mode.PLAY
    assert resumed_id == play_id          # same game, not the view id
    assert resumed_id != view_id
    assert h._human_white is False        # color preserved (not side-to-move)
    assert [m.uci() for m in h._board.move_stack] == ["e2e4", "e7e5"]
    assert h._suspended_play is None      # consumed


async def test_resume_play_kicks_engine_when_its_turn(hve):
    """On resume the engine resumes thinking if it is its move."""
    h, _ = hve
    h._engine_to_move = AsyncMock()
    await h.new_game(human_white=False, tc=TimeControl(60, 0))  # engine = White
    for u in ("e2e4", "e7e5"):  # White (engine) to move at the last ply
        h._board.push(chess.Move.from_uci(u))
        h._eval_history.append(None)
    await _suspend_live_play(h)
    h._engine_to_move.reset_mock()
    await h.resume_play()
    assert h._board.turn == chess.WHITE
    h._engine_to_move.assert_awaited_once()


async def test_suspend_snapshot_uses_live_clocks(hve):
    """The suspend snapshot debits the in-progress turn's elapsed time, so
    scrubbing back mid-move can't refund the clock. _persist keeps banked."""
    h, _ = hve
    h._engine_to_move = AsyncMock()
    await h.new_game(human_white=True, tc=TimeControl(60, 0))  # White (human) to move
    h._clock.start_turn()
    h._clock.turn_started_at = h._clock._now() - 5.0  # 5s already spent
    banked = h._game_state_snapshot()
    live = h._game_state_snapshot(live_clocks=True)
    assert banked.white_time == pytest.approx(60.0)
    assert live.white_time == pytest.approx(55.0, abs=0.5)


async def test_resume_play_restores_clocks(hve):
    """Resumed game's clocks come from the suspend snapshot, not a reset."""
    h, _ = hve
    h._engine_to_move = AsyncMock()
    await h.new_game(human_white=True, tc=TimeControl(60, 0))
    h._clock.white_time = 42.0
    h._clock.black_time = 17.0
    await _suspend_live_play(h)
    await h.resume_play()
    assert h._clock.white_time == pytest.approx(42.0)
    assert h._clock.black_time == pytest.approx(17.0)


async def test_resumable_flag_true_only_when_suspended(hve):
    """The view payload's `resumable` flag tracks _suspended_play: set by a
    suspend entry, absent for a plain import."""
    h, _ = hve
    h._engine_to_move = AsyncMock()
    await h.new_game(human_white=True, tc=TimeControl(60, 0))
    await _suspend_live_play(h)
    assert h._board_event().payload["view"]["resumable"] is True
    # Import-on-top (no suspend) drops it.
    await h.enter_view_mode(ViewModeParams(
        start_fen=None, moves_uci=["d2d4"], clock_history=None,
    ))
    assert h._board_event().payload["view"]["resumable"] is False


async def test_view_payload_carries_suspended_player_color(hve):
    """resume_human_white mirrors the suspended game's human color so the
    client can restore the board POV on remount (the board event's
    human_white is null in view mode). None for a non-resumable import."""
    h, _ = hve
    h._engine_to_move = AsyncMock()
    await h.new_game(human_white=False, tc=TimeControl(60, 0))
    await _suspend_live_play(h)
    assert h._board_event().payload["view"]["resume_human_white"] is False
    await h.enter_view_mode(ViewModeParams(
        start_fen=None, moves_uci=["d2d4"], clock_history=None,
    ))
    assert h._board_event().payload["view"]["resume_human_white"] is None


async def test_resume_play_raises_without_suspended_game(hve):
    """A view session that didn't suspend (e.g. a plain import) can't resume."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None, moves_uci=["e2e4", "e7e5"], clock_history=None,
    ))
    with pytest.raises(RuntimeError, match="no suspended"):
        await h.resume_play()


async def test_resume_play_rejected_outside_view(hve):
    """resume_play is a view-mode exit; play mode must reject it."""
    h, _ = hve
    h._engine_to_move = AsyncMock()
    await h.new_game(human_white=True, tc=TimeControl(60, 0))
    with pytest.raises(ModeConflictError):
        await h.resume_play()


async def test_new_game_clears_suspended_play(hve):
    """A fork/new game drops the suspended snapshot so a later view entry
    can't resume a game that no longer exists."""
    h, _ = hve
    h._engine_to_move = AsyncMock()
    await h.new_game(human_white=True, tc=TimeControl(60, 0))
    await _suspend_live_play(h)
    assert h._suspended_play is not None
    await h.new_game(human_white=True, tc=TimeControl(60, 0))
    assert h._suspended_play is None


async def test_suspend_ignored_when_already_viewing(hve):
    """suspend_play only captures a live play game; an import-on-top while
    already viewing must not stash a bogus (view-board) snapshot."""
    h, _ = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None, moves_uci=["e2e4"], clock_history=None,
    ))
    await h.enter_view_mode(
        ViewModeParams(start_fen=None, moves_uci=["d2d4"], clock_history=None),
        suspend_play=True,
    )
    assert h._suspended_play is None
