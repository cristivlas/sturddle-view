"""Build a complete PGN text from move list and metadata. Pure function; no I/O."""
from __future__ import annotations

import chess
import chess.pgn

from .board import board_from
from .pgn_tags import TAG_ECO, TAG_OPENING, TAG_RESULT, TAG_TERMINATION, TAG_TIME_CONTROL
from .results import UNKNOWN_RESULT, UNTERMINATED
from .score import CP_PER_PAWN, SCORE_CP, SCORE_DEPTH, SCORE_MATE, flip_score


def _format_eval(score: dict) -> str:
    """Format a white-POV (already flipped if needed) score dict for cutechess."""
    if SCORE_MATE in score:
        n = score[SCORE_MATE]
        return f"+M{n}" if n >= 0 else f"-M{-n}"
    pawns = score[SCORE_CP] / CP_PER_PAWN
    return f"{pawns:+.2f}"


def _pgn_text(game: chess.pgn.Game) -> str:
    return f"{game}\n\n"


def build_pgn(
    *,
    start_fen: str | None,
    moves_uci: list[str],
    clock_history: list[tuple[float, float]] | None = None,
    final_clocks: tuple[float, float] | None = None,
    headers: dict[str, str],
    opening: tuple[str, str] | None = None,
    result: str = UNKNOWN_RESULT,
    termination: str = UNTERMINATED,
    time_control: tuple[int, int] | None = None,
    eval_history: list[dict | None] | None = None,
    comments: list[str | None] | None = None,
    root_comment: str | None = None,
) -> str:
    """Return PGN text for a game.

    clock_history[i] is (white_seconds, black_seconds) BEFORE ply i.

    When eval_history is None, per-ply clock annotations are emitted as
    [%clk]. When eval_history is provided, the cutechess/fastchess
    trailing token "{<eval>/<depth> <time>s}" is emitted instead and
    [%clk] is dropped. eval entries are white-POV in memory; the sign
    is flipped at write time on black-to-move plies.

    `comments[i]` is the comment after move `i` (parallel to moves_uci).
    `root_comment` is the pre-game comment (game-root). User comments
    coexist with the [%clk] / eval-token machinery -- the latter are
    appended to whatever the user-facing comment string already is.

    Raises ValueError on length mismatch between eval_history/comments
    and moves.
    """
    if eval_history is not None and len(eval_history) != len(moves_uci):
        raise ValueError(
            f"eval_history length {len(eval_history)} != moves length {len(moves_uci)}"
        )
    if comments is not None and len(comments) != len(moves_uci):
        raise ValueError(
            f"comments length {len(comments)} != moves length {len(moves_uci)}"
        )

    board = board_from(start_fen)
    for uci in moves_uci:
        board.push(chess.Move.from_uci(uci))

    game = chess.pgn.Game.from_board(board)

    for key, val in headers.items():
        game.headers[key] = val
    game.headers[TAG_RESULT] = result
    game.headers[TAG_TERMINATION] = termination
    if time_control is not None:
        game.headers[TAG_TIME_CONTROL] = f"{time_control[0]}+{time_control[1]}"
    if opening is not None:
        game.headers[TAG_ECO] = opening[0]
        game.headers[TAG_OPENING] = opening[1]

    if root_comment:
        game.comment = root_comment

    nodes = list(game.mainline())
    if not nodes:
        return _pgn_text(game)

    # Seed user-prose comments first so the [%clk] / cutechess-token
    # machinery below appends to them rather than clobbering them.
    if comments is not None:
        for i, node in enumerate(nodes):
            c = comments[i]
            if c:
                node.comment = c

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
        return _pgn_text(game)

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
            # White-POV in memory -> STM-POV on a black-to-move ply.
            display = score if movers_white[i] else flip_score(score)
            eval_str = _format_eval(display)
            depth = score.get(SCORE_DEPTH) if isinstance(score, dict) else None
            parts.append(f"{eval_str}/{depth}" if depth is not None else eval_str)
        if elapsed is not None:
            parts.append(f"{elapsed:.1f}s")
        if parts:
            token = " ".join(parts)
            node.comment = f"{node.comment} {token}" if node.comment else token

    return _pgn_text(game)
