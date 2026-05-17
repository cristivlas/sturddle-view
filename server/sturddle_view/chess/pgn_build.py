"""Build a complete PGN text from move list and metadata. Pure function; no I/O."""
from __future__ import annotations

import chess
import chess.pgn

from sturddle_view.chess.board import board_from


def build_pgn(
    *,
    start_fen: str | None,
    moves_uci: list[str],
    clock_history: list[tuple[float, float]] | None = None,
    final_clocks: tuple[float, float] | None = None,
    headers: dict[str, str],
    opening: tuple[str, str] | None = None,
    result: str = "*",
    termination: str = "unterminated",
    time_control: tuple[int, int] | None = None,
) -> str:
    """Return PGN text for a game.

    clock_history[i] is (white_seconds, black_seconds) BEFORE ply i.
    The clock annotation for ply i uses clock_history[i+1] if present,
    else final_clocks for that mover's side.
    """
    board = board_from(start_fen)
    for uci in moves_uci:
        board.push(chess.Move.from_uci(uci))

    game = chess.pgn.Game.from_board(board)

    # Apply caller-supplied headers, then overwrite controlled fields so
    # explicit kwargs always win regardless of what headers contains.
    for key, val in headers.items():
        game.headers[key] = val
    game.headers["Result"] = result
    game.headers["Termination"] = termination
    if time_control is not None:
        game.headers["TimeControl"] = f"{time_control[0]}+{time_control[1]}"
    if opening is not None:
        game.headers["ECO"] = opening[0]
        game.headers["Opening"] = opening[1]

    if clock_history is not None:
        replay_board = board_from(start_fen)
        nodes = list(game.mainline())
        for i, node in enumerate(nodes):
            white_to_move = replay_board.turn == chess.WHITE
            replay_board.push(chess.Move.from_uci(moves_uci[i]))
            if i + 1 < len(clock_history):
                w_after, b_after = clock_history[i + 1]
            elif final_clocks is not None:
                w_after, b_after = final_clocks
            else:
                continue
            node.set_clock(w_after if white_to_move else b_after)

    return f"{game}\n\n"