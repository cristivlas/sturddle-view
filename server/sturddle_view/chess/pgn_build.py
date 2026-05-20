"""Build a complete PGN text from move list and metadata. Pure function; no I/O."""
from __future__ import annotations

import chess
import chess.pgn

from sturddle_view.chess.board import board_from


def _format_eval(score: dict) -> str:
    """Format a white-POV (already flipped if needed) score dict for cutechess."""
    if "mate" in score:
        n = score["mate"]
        return f"+M{n}" if n >= 0 else f"-M{-n}"
    cp = score["cp"]
    pawns = cp / 100.0
    return f"{pawns:+.2f}"


def _flip_pov(score: dict) -> dict:
    """Negate cp/mate for white-POV -> STM-POV on a black-to-move ply."""
    if "mate" in score:
        return {"mate": -score["mate"], **{k: v for k, v in score.items() if k != "mate"}}
    return {"cp": -score["cp"], **{k: v for k, v in score.items() if k != "cp"}}


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
    eval_history: list[dict | None] | None = None,
) -> str:
    """Return PGN text for a game.

    clock_history[i] is (white_seconds, black_seconds) BEFORE ply i.

    When eval_history is None, per-ply clock annotations are emitted as
    [%clk]. When eval_history is provided, the cutechess/fastchess
    trailing token "{<eval>/<depth> <time>s}" is emitted instead and
    [%clk] is dropped. eval entries are white-POV in memory; the sign
    is flipped at write time on black-to-move plies.

    Raises ValueError on length mismatch between eval_history and moves.
    """
    if eval_history is not None and len(eval_history) != len(moves_uci):
        raise ValueError(
            f"eval_history length {len(eval_history)} != moves length {len(moves_uci)}"
        )

    board = board_from(start_fen)
    for uci in moves_uci:
        board.push(chess.Move.from_uci(uci))

    game = chess.pgn.Game.from_board(board)

    for key, val in headers.items():
        game.headers[key] = val
    game.headers["Result"] = result
    game.headers["Termination"] = termination
    if time_control is not None:
        game.headers["TimeControl"] = f"{time_control[0]}+{time_control[1]}"
    if opening is not None:
        game.headers["ECO"] = opening[0]
        game.headers["Opening"] = opening[1]

    nodes = list(game.mainline())
    if not nodes:
        return f"{game}\n\n"

    replay_board = board_from(start_fen)
    increment = float(time_control[1]) if time_control is not None else 0.0
    movers_white: list[bool] = []
    for uci in moves_uci:
        movers_white.append(replay_board.turn == chess.WHITE)
        replay_board.push(chess.Move.from_uci(uci))

    if eval_history is None:
        if clock_history is not None:
            for i, node in enumerate(nodes):
                if i + 1 < len(clock_history):
                    w_after, b_after = clock_history[i + 1]
                elif final_clocks is not None:
                    w_after, b_after = final_clocks
                else:
                    continue
                node.set_clock(w_after if movers_white[i] else b_after)
        return f"{game}\n\n"

    # Cutechess token path. Compute per-ply elapsed from clock_history delta
    # plus increment (consume_turn credits the increment after debiting).
    for i, node in enumerate(nodes):
        before_w, before_b = (
            clock_history[i] if clock_history is not None and i < len(clock_history) else (None, None)
        )
        if clock_history is not None and i + 1 < len(clock_history):
            after_w, after_b = clock_history[i + 1]
        elif final_clocks is not None:
            after_w, after_b = final_clocks
        else:
            after_w = after_b = None

        before = before_w if movers_white[i] else before_b
        after = after_w if movers_white[i] else after_b
        elapsed: float | None
        if before is not None and after is not None:
            elapsed = max(0.0, before + increment - after)
        else:
            elapsed = None

        score = eval_history[i]
        parts: list[str] = []
        if score is not None:
            display = score if movers_white[i] else _flip_pov(score)
            eval_str = _format_eval(display)
            depth = score.get("depth") if isinstance(score, dict) else None
            parts.append(f"{eval_str}/{depth}" if depth is not None else eval_str)
        if elapsed is not None:
            parts.append(f"{elapsed:.1f}s")
        if parts:
            node.comment = " ".join(parts)

    return f"{game}\n\n"
