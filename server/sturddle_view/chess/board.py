"""Shared board construction and query helpers."""
from __future__ import annotations

import chess

from .results import SIDE_BLACK, SIDE_WHITE


def board_from(fen: str | None) -> chess.Board:
    """Return a Board from *fen*, or the standard starting position when None."""
    if fen is None:
        return chess.Board()
    try:
        return chess.Board(fen)
    except ValueError as exc:
        raise ValueError(f"invalid FEN: {exc}") from exc


def replay_uci(board: chess.Board, moves: list[str]) -> chess.Board:
    """Return a copy of *board* with *moves* (UCI strings) applied.

    Raises ValueError on malformed or illegal moves.
    """
    b = board.copy()
    for uci in moves:
        try:
            move = chess.Move.from_uci(uci)
        except ValueError as exc:
            raise ValueError(f"malformed UCI: {uci!r}") from exc
        if not b.is_legal(move):
            raise ValueError(f"illegal move: {uci!r}")
        b.push(move)
    return b


def side_to_move(board: chess.Board) -> str:
    """Return "white" or "black" for the side to move."""
    return SIDE_WHITE if board.turn == chess.WHITE else SIDE_BLACK


def moves_san(board: chess.Board, start_fen: str | None = None) -> list[str]:
    """Return the move stack of *board* as SAN strings replayed from *start_fen*."""
    if not board.move_stack:
        return []
    replay = board_from(start_fen)
    out = []
    for m in board.move_stack:
        out.append(replay.san(m))
        replay.push(m)
    return out
