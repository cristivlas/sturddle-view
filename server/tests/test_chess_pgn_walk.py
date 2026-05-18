"""Tests for chess.pgn_walk.walk_mainline -- red until chess/pgn_walk.py exists."""
from __future__ import annotations

import io

import chess
import chess.pgn
import pytest

try:
    from sturddle_view.chess.pgn_walk import walk_mainline
except ImportError:
    walk_mainline = None  # type: ignore[assignment]

pytestmark = pytest.mark.skipif(
    walk_mainline is None, reason="chess/pgn_walk.py not yet created"
)

_STARTPOS_4_MOVES = "1. e4 e5 2. Nf3 Nc6"
_BLACK_TO_MOVE_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"


def _parse(pgn_text: str) -> chess.pgn.Game:
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    assert game is not None
    return game


def _parse_with_fen(fen: str, pgn_moves: str) -> chess.pgn.Game:
    text = f'[FEN "{fen}"]\n[SetUp "1"]\n\n{pgn_moves}'
    return _parse(text)


def test_walk_yields_one_tuple_per_mainline_node():
    game = _parse(_STARTPOS_4_MOVES)
    result = list(walk_mainline(game))
    assert len(result) == 4
    for node, board_before, mover_white in result:
        assert isinstance(node, chess.pgn.ChildNode)
        assert isinstance(board_before, chess.Board)
        assert isinstance(mover_white, bool)


def test_walk_from_custom_start_board():
    game = _parse_with_fen(_BLACK_TO_MOVE_FEN, "1... e5 2. Nf3")
    start = chess.Board(_BLACK_TO_MOVE_FEN)
    result = list(walk_mainline(game, start_board=start))
    assert len(result) == 2
    _, _, first_mover_white = result[0]
    assert first_mover_white is False


def test_walk_mover_white_alternates_from_startpos():
    game = _parse(_STARTPOS_4_MOVES)
    movers = [mover_white for _, _, mover_white in walk_mainline(game)]
    assert movers == [True, False, True, False]


def test_walk_mover_white_respects_custom_start_with_black_to_move():
    game = _parse_with_fen(_BLACK_TO_MOVE_FEN, "1... e5 2. Nf3 Nc6")
    start = chess.Board(_BLACK_TO_MOVE_FEN)
    movers = [mw for _, _, mw in walk_mainline(game, start_board=start)]
    assert movers == [False, True, False]


def test_walk_advances_board_in_place():
    game = _parse(_STARTPOS_4_MOVES)
    boards_before = [b.fen() for _, b, _ in walk_mainline(game)]
    # Each successive board_before must differ from the previous.
    for i in range(1, len(boards_before)):
        assert boards_before[i] != boards_before[i - 1]
    # First board_before is startpos.
    assert boards_before[0] == chess.STARTING_FEN


def test_walk_empty_game_yields_nothing():
    game = chess.pgn.Game()
    assert list(walk_mainline(game)) == []


def test_walk_supports_node_annotation_writes():
    game = _parse(_STARTPOS_4_MOVES)
    for node, _, _ in walk_mainline(game):
        node.set_clock(5.0)
    # Re-serialize and re-parse; annotations must survive.
    buf = io.StringIO()
    print(game, file=buf, end="\n\n")
    buf.seek(0)
    game2 = chess.pgn.read_game(buf)
    assert game2 is not None
    for node in game2.mainline():
        assert node.clock() == pytest.approx(5.0)
