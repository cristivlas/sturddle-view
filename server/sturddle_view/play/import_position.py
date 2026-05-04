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
    # None when the import begins at the standard startpos (no PGN FEN header);
    # downstream consumers (e.g. opening-book lookup) treat None as "startpos".
    start_fen: str | None

    moves_uci: list[str]  # UCI moves to replay from start_fen
    final_fen: str  # FEN after replaying moves
    side_to_move: str  # "white" | "black", at the final position
    ply: int  # ply count at the final position
    summary: str  # short human-readable description
    headers: dict[str, str] | None = None  # PGN headers, when applicable
    # Per-ply pre-move (white, black) clock snapshots reconstructed from
    # [%clk] comments. None when the PGN has no clock annotations at all.
    # Inner values may be None for sides that hadn't moved yet at that ply
    # (consumer fills those with the configured TC's initial seconds).
    clock_history: list[tuple[float | None, float | None]] | None = None
    # Live clocks AFTER the final ply. None when not derivable from the PGN.
    final_white_time: float | None = None
    final_black_time: float | None = None


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
    # Treat the standard startpos as None so opening-book lookup engages
    # on subsequent moves (lookup keys on move history from startpos).
    is_startpos = board.fen() == chess.STARTING_FEN
    return ImportedPosition(
        start_fen=None if is_startpos else board.fen(),
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
    nodes: list[chess.pgn.ChildNode] = []
    for node in game.mainline():
        move = node.move
        if move not in board.legal_moves:
            raise PositionImportError(
                f"illegal move in PGN at ply {len(moves_uci) + 1}: {move.uci()}"
            )
        moves_uci.append(move.uci())
        board.push(move)
        nodes.append(node)
    if board.is_game_over():
        raise PositionImportError("PGN ends in a finished position")
    # python-chess's PGN parser is lenient: arbitrary text yields a valid
    # game with no moves and a startpos board. Reject that — an "import"
    # that just gets you to startpos is the New Game button.
    if not moves_uci and not start_fen_header:
        raise PositionImportError("PGN contains no moves")
    side = "white" if board.turn == chess.WHITE else "black"
    white = headers.get("White", "?")
    black = headers.get("Black", "?")
    summary = (
        f"{white} vs {black} — {side} to move (ply {board.ply()})"
        if (white != "?" or black != "?")
        else f"{side.capitalize()} to move (ply {board.ply()})"
    )
    # Reconstruct (white, black) pre-move snapshots from [%clk] comments.
    # Only emit a clock_history if at least one ply carries a clock.
    clk_values = [n.clock() for n in nodes]
    clock_history: list[tuple[float, float]] | None = None
    final_white = final_black = None
    if any(v is not None for v in clk_values):
        clock_history = []
        replay = start_board.copy()
        last_w = last_b = None  # last known post-move clock per side
        for i, node in enumerate(nodes):
            mover_white = (replay.turn == chess.WHITE)
            # Pre-move snapshot for ply i = last known clocks for each side.
            clock_history.append((last_w, last_b))
            after = clk_values[i]
            if after is not None:
                if mover_white:
                    last_w = after
                else:
                    last_b = after
            replay.push(node.move)
        final_white, final_black = last_w, last_b
    return ImportedPosition(
        start_fen=start_fen_header if start_fen_header else None,
        moves_uci=moves_uci,
        final_fen=board.fen(),
        side_to_move=side,
        ply=board.ply(),
        summary=summary,
        headers=headers,
        clock_history=clock_history,
        final_white_time=final_white,
        final_black_time=final_black,
    )
