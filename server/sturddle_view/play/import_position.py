"""Parse FEN / PGN payloads into a normalized seed for HumanVsEngine.

Returns the final FEN, the UCI move list (empty for FEN imports), and a
short human-readable summary used by the client to confirm before commit.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

import chess
import chess.pgn


@dataclass
class ImportedPosition:
    start_fen: str  # FEN of the starting position (before replaying moves)
    moves_uci: list[str]  # UCI moves to replay from start_fen
    final_fen: str  # FEN after replaying moves
    side_to_move: str  # "white" | "black", at the final position
    ply: int  # ply count at the final position
    summary: str  # short human-readable description
    headers: dict[str, str] | None = None  # PGN headers, when applicable


class PositionImportError(ValueError):
    pass


def parse_fen(text: str) -> ImportedPosition:
    fen = text.strip()
    if not fen:
        raise PositionImportError("empty FEN")
    try:
        board = chess.Board(fen)
    except ValueError as e:
        raise PositionImportError(f"invalid FEN: {e}") from e
    if board.is_game_over():
        raise PositionImportError("position is already over (checkmate / stalemate / draw)")
    side = "white" if board.turn == chess.WHITE else "black"
    return ImportedPosition(
        start_fen=board.fen(),
        moves_uci=[],
        final_fen=board.fen(),
        side_to_move=side,
        ply=board.ply(),
        summary=f"{side.capitalize()} to move (ply {board.ply()})",
    )


def parse_pgn(text: str) -> ImportedPosition:
    if not text.strip():
        raise PositionImportError("empty PGN")
    try:
        game = chess.pgn.read_game(io.StringIO(text))
    except Exception as e:  # python-chess can raise a variety of types
        raise PositionImportError(f"could not parse PGN: {e}") from e
    if game is None:
        raise PositionImportError("no game found in PGN")
    headers = dict(game.headers)
    # FEN header lets the PGN start from a non-standard position.
    start_fen_header = headers.get("FEN")
    try:
        start_board = (
            chess.Board(start_fen_header) if start_fen_header else chess.Board()
        )
    except ValueError as e:
        raise PositionImportError(f"PGN has invalid starting FEN header: {e}") from e
    moves_uci: list[str] = []
    board = start_board.copy()
    for move in game.mainline_moves():
        if move not in board.legal_moves:
            raise PositionImportError(
                f"illegal move in PGN at ply {len(moves_uci) + 1}: {move.uci()}"
            )
        moves_uci.append(move.uci())
        board.push(move)
    if board.is_game_over():
        raise PositionImportError("PGN ends in a finished position")
    side = "white" if board.turn == chess.WHITE else "black"
    white = headers.get("White", "?")
    black = headers.get("Black", "?")
    summary = (
        f"{white} vs {black} — {side} to move (ply {board.ply()})"
        if (white != "?" or black != "?")
        else f"{side.capitalize()} to move (ply {board.ply()})"
    )
    return ImportedPosition(
        start_fen=start_board.fen(),
        moves_uci=moves_uci,
        final_fen=board.fen(),
        side_to_move=side,
        ply=board.ply(),
        summary=summary,
        headers=headers,
    )
