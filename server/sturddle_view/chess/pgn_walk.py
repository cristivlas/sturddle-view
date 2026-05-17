"""Walk a PGN game's mainline, yielding (node, board_before_move, mover_white)."""
from __future__ import annotations

from typing import Iterator

import chess
import chess.pgn


def walk_mainline(
    game: chess.pgn.Game,
    start_board: chess.Board | None = None,
) -> Iterator[tuple[chess.pgn.ChildNode, chess.Board, bool]]:
    """Yield (node, board_BEFORE_move, mover_white) for each mainline node.

    The board is the live game board advanced as iteration proceeds -- callers
    must not retain references to it across iterations. Trusts the PGN: does
    not validate move legality (callers on perf-supercritical paths cannot
    afford an O(legal_moves) check per ply). board.push may raise on
    structurally invalid moves; chess.InvalidMoveError propagates.
    """
    board = (start_board if start_board is not None else game.board()).copy()
    for node in game.mainline():
        move = node.move
        mover_white = board.turn == chess.WHITE
        yield node, board, mover_white
        board.push(move)
