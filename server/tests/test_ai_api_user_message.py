"""_ai_kick internals: build the initial user message + pick persona
from live hve state via HVE's public accessors. Replace when the
coordinator owns its own session state."""
from __future__ import annotations

import chess

from sturddle_view.api._ai_kick import (
    _prompt_mode_for,
    _san_history_for,
)
from sturddle_view.chess.board import board_from
from sturddle_view.openings import Opening
from sturddle_view.play.mode import Mode
from sturddle_view.play.opening_lines import BookRef

from .ai_kick_helpers import FakeSettings, build_inputs, build_message as _build


class _FakeHve:
    """Minimal stand-in: only the accessors _ai_kick helpers call."""
    def __init__(
        self,
        board: chess.Board | None,
        start_fen: str | None = None,
        pre_analysis_mode: Mode | None = None,
        view_full_moves: list | None = None,
        engine_name: str | None = None,
        opening: Opening | None = None,
        viewed_pgn_result: str | None = None,
        book_ref: BookRef | None = None,
    ):
        self._board = board
        self._start_fen = start_fen
        self._pre_analysis_mode = pre_analysis_mode
        self._view_full_moves = list(view_full_moves or [])
        self._engine_name = engine_name
        self._opening = opening
        self._viewed_pgn_result = viewed_pgn_result
        self._book_ref = book_ref

    def current_board(self) -> chess.Board | None:
        return self._board

    def start_fen(self) -> str | None:
        return self._start_fen

    def book_ref(self) -> BookRef | None:
        return self._book_ref

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

    def engine_display_name(self) -> str | None:
        return self._engine_name

    def lookup_opening(self) -> Opening | None:
        return self._opening

    def viewed_pgn_result(self) -> str | None:
        return self._viewed_pgn_result


def test_build_user_message_handles_no_hve():
    assert _build(None) is None


def test_build_user_message_handles_no_board():
    assert _build(_FakeHve(board=None)) is None


def test_build_user_message_startpos_no_moves():
    board = chess.Board()
    msg = _build(_FakeHve(board=board))
    assert msg is not None
    assert board.fen() in msg
    assert "(none yet" in msg


def test_build_user_message_after_moves():
    board = chess.Board()
    board.push_san("e4")
    board.push_san("e5")
    board.push_san("Nf3")

    msg = _build(_FakeHve(board=board))
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

    msg = _build(_FakeHve(board=board, start_fen=start_fen))
    assert msg is not None
    assert "1. e4" in msg
    assert board.fen() in msg


def test_build_user_message_includes_engine_name():
    # AI prompt path shortens to the first whitespace token to avoid
    # leaking full UCI ids ("MyEngine 2.5.1-rc9...") into model prose.
    h = _FakeHve(board=chess.Board(), engine_name="MyEngine 2.5.1-rc9")
    msg = _build(h)
    assert msg is not None
    assert msg.startswith("Engine: MyEngine\n")


def test_build_user_message_includes_opening_when_book_hit():
    h = _FakeHve(
        board=chess.Board(),
        opening=Opening(eco="C44", name="King's Pawn Game"),
    )
    msg = _build(h)
    assert msg is not None
    assert "Opening: [C44] King's Pawn Game\n" in msg


def test_build_user_message_omits_opening_when_book_misses():
    h = _FakeHve(board=chess.Board(), opening=None)
    msg = _build(h)
    assert msg is not None
    assert "Opening:" not in msg


def test_build_user_message_includes_view_pgn_result():
    h = _FakeHve(board=chess.Board(), viewed_pgn_result="0-1")
    msg = _build(h)
    assert msg is not None
    assert "Game result: 0-1" in msg


def test_build_user_message_omits_unknown_pgn_result():
    h = _FakeHve(board=chess.Board(), viewed_pgn_result="*")
    msg = _build(h)
    assert msg is not None
    assert "Game result:" not in msg


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
    msg = _build(h)
    assert msg is not None
    assert cursor_board.fen() in msg          # FEN reflects the cursor
    assert "1. e4 e5 2. Nf3" in msg            # full game appears
    assert "Game moves:" in msg                # new label


def test_build_user_message_view_mode_includes_move_played_here():
    # Cursor at ply 2 (after 1.e4 e5); the move played here is Nf3.
    full_board = chess.Board()
    moves = []
    for san in ("e4", "e5", "Nf3", "Nc6"):
        moves.append(full_board.parse_san(san))
        full_board.push_san(san)

    cursor_board = chess.Board()
    cursor_board.push_san("e4")
    cursor_board.push_san("e5")

    h = _FakeHve(board=cursor_board, view_full_moves=moves)
    msg = _build(h)
    assert msg is not None
    assert "Move played here: Nf3" in msg


def test_build_user_message_view_mode_omits_move_played_at_end_of_game():
    # Cursor at the final position; no move follows.
    full_board = chess.Board()
    moves = []
    for san in ("e4", "e5"):
        moves.append(full_board.parse_san(san))
        full_board.push_san(san)

    cursor_board = chess.Board()
    cursor_board.push_san("e4")
    cursor_board.push_san("e5")

    h = _FakeHve(board=cursor_board, view_full_moves=moves)
    msg = _build(h)
    assert msg is not None
    assert "Move played here:" not in msg


# ---------- probe wiring: armed book, settings fallback, gates --------


def _book_file(tmp_path, name: str, *movetexts: str) -> str:
    path = tmp_path / name
    games = "\n\n".join(f'[Event "?"]\n\n{m}' for m in movetexts)
    path.write_text(games + "\n", encoding="utf-8")
    return str(path)


def _book_settings(path: str) -> FakeSettings:
    return FakeSettings(hve_use_opening_book=True, engine_default_book_path=path)


def _e4_board() -> chess.Board:
    board = chess.Board()
    board.push_san("e4")
    return board


_E4_OPENING = Opening(eco="B00", name="King's Pawn")


def test_probe_prefers_armed_book_over_settings(tmp_path):
    armed = BookRef(
        path=_book_file(tmp_path, "armed.pgn", "1. e4 e5 *"),
        plies=None, order=None, anchor=0,
    )
    settings = _book_settings(_book_file(tmp_path, "configured.pgn", "1. e4 c5 *"))
    h = _FakeHve(board=_e4_board(), opening=_E4_OPENING, book_ref=armed)
    msg = _build(h, settings=settings)
    assert msg is not None
    assert "Book reply here: 1...e5 (configured opening book)" in msg


def test_probe_falls_back_to_settings_book_when_none_armed(tmp_path):
    settings = _book_settings(_book_file(tmp_path, "configured.pgn", "1. e4 c5 *"))
    h = _FakeHve(board=_e4_board(), opening=_E4_OPENING)
    msg = _build(h, settings=settings)
    assert msg is not None
    assert "Book reply here: 1...c5 (configured opening book)" in msg


def test_probe_skipped_for_custom_start_fen(tmp_path):
    # Armed book would answer 1...e5; a non-None start FEN must keep the
    # probe off entirely.
    armed = BookRef(
        path=_book_file(tmp_path, "armed.pgn", "1. e4 e5 *"),
        plies=None, order=None, anchor=0,
    )
    h = _FakeHve(
        board=_e4_board(),
        start_fen=chess.STARTING_FEN,
        opening=_E4_OPENING,
        book_ref=armed,
    )
    msg = _build(h)
    assert msg is not None
    assert "Book reply here:" not in msg


def test_turn_inputs_carry_book_move_uci(tmp_path):
    # The hit rides out as UCI for the loop's red-team exemption.
    settings = _book_settings(_book_file(tmp_path, "configured.pgn", "1. e4 c5 *"))
    h = _FakeHve(board=_e4_board(), opening=_E4_OPENING)
    inputs = build_inputs(h, settings=settings)
    assert inputs is not None
    assert inputs.book_move_uci == "c7c5"


def test_view_mode_played_move_in_book_is_the_reply(tmp_path):
    # Reviewing 1...c5 in a Sicilian game: the book also has 1...e5 first,
    # but the played move is theory, so it is the reply.
    settings = _book_settings(
        _book_file(tmp_path, "configured.pgn", "1. e4 e5 *", "1. e4 c5 *")
    )
    h = _FakeHve(
        board=_e4_board(),
        view_full_moves=[chess.Move.from_uci(u) for u in ("e2e4", "c7c5", "g1f3")],
        opening=_E4_OPENING,
    )
    inputs = build_inputs(h, settings=settings)
    assert inputs is not None
    assert "Move played here: c5" in inputs.user_message
    assert (
        "Book reply here: 1...c5 (configured opening book); also standard: 1...e5"
    ) in inputs.user_message
    assert inputs.book_move_uci == "c7c5"


def test_turn_inputs_book_move_none_when_off_book():
    inputs = build_inputs(_FakeHve(board=_e4_board(), opening=_E4_OPENING))
    assert inputs is not None
    assert inputs.book_move_uci is None


def test_probe_runs_outside_opening(tmp_path):
    # No matched ECO line doesn't gate the probe: the armed book file can
    # still answer past the named theory.
    armed = BookRef(
        path=_book_file(tmp_path, "armed.pgn", "1. e4 e5 *"),
        plies=None, order=None, anchor=0,
    )
    h = _FakeHve(board=_e4_board(), opening=None, book_ref=armed)
    msg = _build(h)
    assert msg is not None
    assert "Book reply here: 1...e5 (configured opening book)" in msg
