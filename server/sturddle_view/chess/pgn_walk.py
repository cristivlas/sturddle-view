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
    must not retain references to it across iterations. Raises
    chess.IllegalMoveError on illegal moves.
    """
    board = (start_board if start_board is not None else game.board()).copy()
    for node in game.mainline():
        move = node.move
        if move not in board.legal_moves:
            raise chess.IllegalMoveError(
                f"illegal move at ply {board.ply() + 1}: {move.uci()}"
            )
        mover_white = board.turn == chess.WHITE
        yield node, board, mover_white
        board.push(move)
