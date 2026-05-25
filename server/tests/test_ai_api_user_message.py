"""_ai_kick internals: build the initial user message + pick persona
from live hve state via HVE's public accessors. Replace when the
coordinator owns its own session state."""
from __future__ import annotations

import chess

from sturddle_view.api._ai_kick import (
    _build_user_message,
    _prompt_mode_for,
    _san_history_for,
)
from sturddle_view.chess.board import board_from
from sturddle_view.play.mode import Mode


class _FakeHve:
    """Minimal stand-in: only the accessors _ai_kick helpers call."""
    def __init__(
        self,
        board: chess.Board | None,
        start_fen: str | None = None,
        pre_analysis_mode: Mode | None = None,
        view_full_moves: list | None = None,
    ):
        self._board = board
        self._start_fen = start_fen
        self._pre_analysis_mode = pre_analysis_mode
        self._view_full_moves = list(view_full_moves or [])

    def current_board(self) -> chess.Board | None:
        return self._board

    def start_fen(self) -> str | None:
        return self._start_fen

    def pre_analysis_mode(self) -> Mode | None:
        return self._pre_analysis_mode

    def view_full_moves_san(self) -> list[str]:
        """Empty list = play mode; non-empty = view mode, render
        against the start FEN."""
        if not self._view_full_moves:
            return []
        replay = board_from(self._start_fen)
        out = []
        for m in self._view_full_moves:
            out.append(replay.san(m))
            replay.push(m)
        return out


def test_build_user_message_handles_no_hve():
    assert _build_user_message(None) is None


def test_build_user_message_handles_no_board():
    assert _build_user_message(_FakeHve(board=None)) is None


def test_build_user_message_startpos_no_moves():
    board = chess.Board()
    msg = _build_user_message(_FakeHve(board=board))
    assert msg is not None
    assert board.fen() in msg
    assert "(none yet" in msg


def test_build_user_message_after_moves():
    board = chess.Board()
    board.push_san("e4")
    board.push_san("e5")
    board.push_san("Nf3")

    msg = _build_user_message(_FakeHve(board=board))
    assert msg is not None
    assert board.fen() in msg
    assert "1. e4 e5 2. Nf3" in msg


def test_build_user_message_honors_start_fen():
    # Imported game whose move_stack is replayed from a custom start_fen.
    # Without honoring _start_fen, moves_san would replay from startpos
    # and either crash or produce wrong SAN.
    start_fen = "4k3/8/8/8/8/8/4P3/4K3 w - - 0 1"
    board = chess.Board(start_fen)
    board.push_san("e4")

    msg = _build_user_message(_FakeHve(board=board, start_fen=start_fen))
    assert msg is not None
    assert "1. e4" in msg
    assert board.fen() in msg


# ---------- _prompt_mode_for ------------------------------------------


def test_prompt_mode_from_play_is_coach():
    h = _FakeHve(board=chess.Board(), pre_analysis_mode=Mode.PLAY)
    assert _prompt_mode_for(h) == "coach"


def test_prompt_mode_from_paused_is_coach():
    h = _FakeHve(board=chess.Board(), pre_analysis_mode=Mode.PAUSED)
    assert _prompt_mode_for(h) == "coach"


def test_prompt_mode_from_viewing_is_commentator():
    h = _FakeHve(board=chess.Board(), pre_analysis_mode=Mode.VIEWING)
    assert _prompt_mode_for(h) == "commentator"


def test_prompt_mode_no_hve_defaults_to_coach():
    assert _prompt_mode_for(None) == "coach"


# ---------- _san_history_for -----------------------------------------


def test_san_history_play_mode_uses_board_move_stack():
    # No _view_full_moves => play mode => moves come straight off board.
    board = chess.Board()
    board.push_san("e4")
    board.push_san("c5")
    h = _FakeHve(board=board)
    assert _san_history_for(h) == ["e4", "c5"]


def test_san_history_view_mode_returns_full_game_not_prefix():
    # View mode at cursor 2 of a 4-move game. _board reflects the cursor
    # (only the first 2 moves applied). _view_full_moves carries all 4.
    # The agent should see ALL 4 so the commentator can reference moves
    # past the cursor.
    full_board = chess.Board()
    moves = []
    for san in ("e4", "e5", "Nf3", "Nc6"):
        moves.append(full_board.parse_san(san))
        full_board.push_san(san)

    cursor_board = chess.Board()
    cursor_board.push_san("e4")
    cursor_board.push_san("e5")

    h = _FakeHve(board=cursor_board, view_full_moves=moves)
    assert _san_history_for(h) == ["e4", "e5", "Nf3", "Nc6"]


def test_build_user_message_view_mode_includes_future_moves():
    # End-to-end: a view-mode build at cursor 1 includes the full game
    # under "Game moves" while the FEN locates the cursor at ply 1.
    full_board = chess.Board()
    moves = []
    for san in ("e4", "e5", "Nf3"):
        moves.append(full_board.parse_san(san))
        full_board.push_san(san)

    cursor_board = chess.Board()
    cursor_board.push_san("e4")

    h = _FakeHve(board=cursor_board, view_full_moves=moves)
    msg = _build_user_message(h)
    assert msg is not None
    assert cursor_board.fen() in msg          # FEN reflects the cursor
    assert "1. e4 e5 2. Nf3" in msg            # full game appears
    assert "Game moves:" in msg                # new label
