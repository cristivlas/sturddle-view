"""Server-authoritative board edit mode: state transitions and that
play/analysis/view-nav endpoints reject while editing."""
from __future__ import annotations

from unittest.mock import AsyncMock

import chess
import pytest

from sturddle_view.config import Settings
from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine, TimeControl, ViewModeParams
from sturddle_view.play.import_position import parse_pgn
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
    return h


async def _enter_view(h: HumanVsEngine, fen: str | None = None) -> None:
    await h.enter_view_mode(ViewModeParams(
        start_fen=fen,
        moves_uci=[],
        clock_history=None,
    ))


async def test_enter_edit_mode_requires_view_mode(hve: HumanVsEngine):
    with pytest.raises(ModeConflictError):
        await hve.enter_edit_mode()


async def test_enter_edit_mode_snapshots_current_fen(hve: HumanVsEngine):
    await _enter_view(hve)
    pre = await hve.enter_edit_mode()
    assert hve._editing is True
    assert pre == chess.STARTING_FEN
    assert hve._edit_saved_view.board.fen() == chess.STARTING_FEN


async def test_commit_edit_with_valid_fen_enters_view_at_fen(hve: HumanVsEngine):
    await _enter_view(hve)
    await hve.enter_edit_mode()
    new_fen = "4k3/8/8/8/8/8/8/4K3 w - - 0 1"
    await hve.commit_edit(new_fen)
    assert hve._editing is False
    assert hve._viewing is True
    assert hve._board.fen() == new_fen


async def test_commit_edit_with_illegal_position_stays_editing(hve: HumanVsEngine):
    await _enter_view(hve)
    await hve.enter_edit_mode()
    # Two white kings -> illegal.
    bad = "4k3/8/8/8/8/8/8/3KK3 w - - 0 1"
    with pytest.raises(RuntimeError):
        await hve.commit_edit(bad)
    assert hve._editing is True


async def test_commit_edit_with_invalid_fen_stays_editing(hve: HumanVsEngine):
    await _enter_view(hve)
    await hve.enter_edit_mode()
    with pytest.raises(RuntimeError, match="invalid FEN"):
        await hve.commit_edit("not a fen")
    assert hve._editing is True


async def test_cancel_edit_restores_pre_edit_fen(hve: HumanVsEngine):
    start = "4k3/8/8/8/8/8/8/4K3 w - - 0 1"
    await _enter_view(hve, fen=start)
    await hve.enter_edit_mode()
    await hve.cancel_edit()
    assert hve._editing is False
    assert hve._board.fen() == start


async def test_editing_blocks_view_nav(hve: HumanVsEngine):
    await hve.enter_view_mode(ViewModeParams(
        start_fen=None, moves_uci=["e2e4", "e7e5"], clock_history=None,
    ))
    await hve.enter_edit_mode()
    with pytest.raises(ModeConflictError):
        await hve.view_first()
    with pytest.raises(ModeConflictError):
        await hve.view_back()
    with pytest.raises(ModeConflictError):
        await hve.view_forward()
    with pytest.raises(ModeConflictError):
        await hve.view_last()
    with pytest.raises(ModeConflictError):
        await hve.view_goto(0)


async def test_editing_blocks_play_from_here(hve: HumanVsEngine):
    await _enter_view(hve)
    await hve.enter_edit_mode()
    with pytest.raises(ModeConflictError):
        await hve.play_from_here(tc=TimeControl(initial_seconds=60.0, increment_seconds=0.0))


async def test_editing_blocks_analysis(hve: HumanVsEngine):
    await _enter_view(hve)
    await hve.enter_edit_mode()
    with pytest.raises(ModeConflictError):
        await hve.start_analysis()


async def test_editing_blocks_import(hve: HumanVsEngine):
    await _enter_view(hve)
    await hve.enter_edit_mode()
    with pytest.raises(ModeConflictError):
        await hve.enter_view_mode(ViewModeParams(start_fen=None, moves_uci=[], clock_history=None))


async def test_editing_blocks_new_game(hve: HumanVsEngine):
    await _enter_view(hve)
    await hve.enter_edit_mode()
    with pytest.raises(ModeConflictError):
        await hve.new_game(
            human_white=True,
            tc=TimeControl(initial_seconds=60.0, increment_seconds=0.0),
        )


async def test_enter_edit_mode_from_viewing_transitions_to_editing(hve: HumanVsEngine):
    """VIEWING -> EDITING is the valid path; mode must flip correctly."""
    await _enter_view(hve)
    assert hve._mode == Mode.VIEWING
    await hve.enter_edit_mode()
    assert hve._mode == Mode.EDITING
    assert hve._editing is True
    assert hve._analysis_mode is False


async def test_enter_edit_from_analyzing_cancels_analysis(hve: HumanVsEngine):
    """ANALYZING -> EDITING: analysis is cancelled, mode lands in EDITING."""
    await _enter_view(hve)
    hve._mode = Mode.ANALYZING
    cancel_called = []

    async def fake_cancel():
        cancel_called.append(True)

    hve._cancel_analysis = fake_cancel
    await hve.enter_edit_mode()
    assert hve._mode is Mode.EDITING
    assert cancel_called == [True]


async def test_enter_edit_mode_rejects_when_already_editing(hve: HumanVsEngine):
    await _enter_view(hve)
    await hve.enter_edit_mode()
    with pytest.raises(ModeConflictError):
        await hve.enter_edit_mode()


_MOVES = ["e2e4", "e7e5"]
_AFTER_MOVES_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
_DIFF_FEN = "4k3/8/8/8/8/8/8/4K3 w - - 0 1"


async def _enter_view_with_moves(h: HumanVsEngine) -> None:
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=_MOVES,
        clock_history=None,
    ))


async def test_view_with_moves_populates_move_list(hve: HumanVsEngine):
    await _enter_view_with_moves(hve)
    assert [m.uci() for m in hve._view_full_moves] == _MOVES


async def test_enter_edit_mode_snapshots_cursor_fen_not_start(hve: HumanVsEngine):
    await _enter_view_with_moves(hve)
    await hve.view_last()
    pre = await hve.enter_edit_mode()
    assert pre == _AFTER_MOVES_FEN
    assert hve._edit_saved_view.board.fen() == _AFTER_MOVES_FEN


async def test_view_start_lands_at_last_ply(hve: HumanVsEngine):
    """play->view transition must land at the last ply, not ply 0."""
    await _enter_view_with_moves(hve)
    await hve.view_last()
    assert hve._view_cursor == len(_MOVES)
    assert hve._board.fen() == _AFTER_MOVES_FEN


async def test_commit_edit_unchanged_fen_preserves_history(hve: HumanVsEngine):
    await _enter_view_with_moves(hve)
    await hve.view_last()
    await hve.enter_edit_mode()
    await hve.commit_edit(_AFTER_MOVES_FEN)
    assert [m.uci() for m in hve._view_full_moves] == _MOVES


async def test_commit_edit_client_fen_with_reset_clocks_preserves_history(hve: HumanVsEngine):
    """Client's getEditFen() always emits halfmove=0 fullmove=1. The unchanged
    check must compare positions, not raw FEN strings."""
    await _enter_view_with_moves(hve)
    await hve.view_last()
    await hve.enter_edit_mode()
    # Same position, but halfmove/fullmove reset like the client sends.
    client_fen = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 1"
    await hve.commit_edit(client_fen)
    assert [m.uci() for m in hve._view_full_moves] == _MOVES


async def test_commit_edit_changed_fen_drops_history(hve: HumanVsEngine):
    await _enter_view_with_moves(hve)
    await hve.enter_edit_mode()
    await hve.commit_edit(_DIFF_FEN)
    assert hve._view_full_moves == []


async def test_cancel_edit_preserves_history(hve: HumanVsEngine):
    await _enter_view_with_moves(hve)
    await hve.enter_edit_mode()
    await hve.cancel_edit()
    assert [m.uci() for m in hve._view_full_moves] == _MOVES


async def _enter_view_with_metadata(h: HumanVsEngine) -> None:
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=_MOVES,
        clock_history=[(60.0, 60.0), (59.0, 59.0)],
        final_white_time=58.0,
        final_black_time=58.0,
        white_name="Alice",
        black_name="Bob",
        eval_history=[{"cp": 20}, {"cp": -10}],
        comments=["good move", None],
        root_comment="opening",
        pgn_result="*",
        pgn_termination="unterminated",
    ))


async def test_cancel_edit_preserves_metadata(hve: HumanVsEngine):
    await _enter_view_with_metadata(hve)
    await hve.view_last()
    await hve.enter_edit_mode()
    await hve.cancel_edit()
    assert hve._view_white_name == "Alice"
    assert hve._view_black_name == "Bob"
    assert hve._view_eval_history == [{"cp": 20}, {"cp": -10}]
    assert hve._view_comments == ["good move", None]
    assert hve._view_root_comment == "opening"
    assert hve._view_pgn_result == "*"
    assert hve._view_pgn_termination == "unterminated"
    assert hve._view_final_white == 58.0
    assert hve._view_final_black == 58.0


async def test_commit_edit_unchanged_preserves_metadata(hve: HumanVsEngine):
    await _enter_view_with_metadata(hve)
    await hve.view_last()
    await hve.enter_edit_mode()
    await hve.commit_edit(_AFTER_MOVES_FEN)
    assert hve._view_white_name == "Alice"
    assert hve._view_black_name == "Bob"
    assert hve._view_eval_history == [{"cp": 20}, {"cp": -10}]
    assert hve._view_comments == ["good move", None]
    assert hve._view_root_comment == "opening"
    assert hve._view_pgn_result == "*"
    assert hve._view_pgn_termination == "unterminated"


async def test_cancel_edit_restores_cursor(hve: HumanVsEngine):
    await _enter_view_with_moves(hve)
    await hve.view_goto(1)
    await hve.enter_edit_mode()
    await hve.cancel_edit()
    assert hve._view_cursor == 1


async def test_commit_edit_unchanged_restores_cursor(hve: HumanVsEngine):
    await _enter_view_with_moves(hve)
    await hve.view_last()
    await hve.enter_edit_mode()
    await hve.commit_edit(_AFTER_MOVES_FEN)
    assert hve._view_cursor == len(_MOVES)


# Snippet from a real fastchess export: headers + eval/depth/time comments.
_ANNOTATED_PGN = """\
[White "IsaBB NN 4.4"]
[Black "Sturddle 2.3.1"]
[Result "1/2-1/2"]

1. e4 {(Book)} e5 {(Book)} 2. Nf3 {(Book)} Nc6 {(Book)} \
3. Bb5 {(Bb5 a6 Ba4) 0.50/24 14} a6 {(e4 dxc6) -0.76/29 27} \
4. Ba4 {(Ba4 Nf6) 0.36/25 50} Nf6 {(Nf6 O-O) -0.74/29 28} *
"""


async def test_annotated_pgn_edit_cancel_is_lossless(hve: HumanVsEngine):
    """edit->cancel must not drop any field from an annotated imported game."""
    p = parse_pgn(_ANNOTATED_PGN)
    headers = p.headers or {}
    await hve.enter_view_mode(ViewModeParams(
        start_fen=p.start_fen,
        moves_uci=p.moves_uci,
        clock_history=p.clock_history,
        final_white_time=p.final_white_time,
        final_black_time=p.final_black_time,
        white_name=headers.get("White"),
        black_name=headers.get("Black"),
        eval_history=p.eval_history,
        comments=p.comments,
        root_comment=p.root_comment,
        pgn_result=headers.get("Result"),
        pgn_termination=headers.get("Termination"),
    ))
    await hve.view_last()

    snap_moves = list(hve._view_full_moves)
    snap_clocks = list(hve._view_clock_history)
    snap_evals = list(hve._view_eval_history) if hve._view_eval_history is not None else None
    snap_comments = list(hve._view_comments) if hve._view_comments is not None else None
    snap_cursor = hve._view_cursor

    await hve.enter_edit_mode()
    await hve.cancel_edit()

    assert hve._view_white_name == headers.get("White")
    assert hve._view_black_name == headers.get("Black")
    assert hve._view_pgn_result == headers.get("Result")
    assert hve._view_eval_history == snap_evals
    assert hve._view_comments == snap_comments
    assert [m.uci() for m in hve._view_full_moves] == [m.uci() for m in snap_moves]
    assert hve._view_clock_history == snap_clocks
    assert hve._view_cursor == snap_cursor


async def test_cancel_edit_preserves_game_id(hve: HumanVsEngine):
    await _enter_view_with_moves(hve)
    gid = hve._game_id
    await hve.enter_edit_mode()
    await hve.cancel_edit()
    assert hve._game_id == gid


async def test_commit_edit_unchanged_preserves_game_id(hve: HumanVsEngine):
    await _enter_view_with_moves(hve)
    await hve.view_last()
    gid = hve._game_id
    await hve.enter_edit_mode()
    await hve.commit_edit(_AFTER_MOVES_FEN)
    assert hve._game_id == gid


async def test_commit_edit_changed_generates_new_game_id(hve: HumanVsEngine):
    await _enter_view_with_moves(hve)
    gid = hve._game_id
    await hve.enter_edit_mode()
    await hve.commit_edit(_DIFF_FEN)
    assert hve._game_id != gid


async def test_commit_edit_unchanged_publishes_correct_view_payload(hve: HumanVsEngine):
    """After unchanged-commit, the board_update event must carry the full
    move list so the client move-list and nav buttons stay alive."""
    await _enter_view_with_moves(hve)
    await hve.view_last()
    await hve.enter_edit_mode()
    q = await hve._bus.subscribe()
    await hve.commit_edit(_AFTER_MOVES_FEN)
    latest = None
    while not q.empty():
        evt = q.get_nowait()
        if evt.kind == "board_update":
            latest = evt
    assert latest is not None
    payload = latest.payload
    assert payload.get("moves_san") and len(payload["moves_san"]) == len(_MOVES)
    v = payload.get("view")
    assert v is not None
    assert v["total_plies"] == len(_MOVES)
    assert v["cursor"] == len(_MOVES)
    assert payload.get("editing") is False


async def test_board_update_payload_includes_editing_flag(hve: HumanVsEngine):
    q = await hve._bus.subscribe()
    await _enter_view(hve)
    await hve.enter_edit_mode()
    # Drain the queue and grab the most recent board_update.
    latest = None
    while not q.empty():
        evt = q.get_nowait()
        if evt.kind == "board_update":
            latest = evt
    assert latest is not None
    assert latest.payload.get("editing") is True
