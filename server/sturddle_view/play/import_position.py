"""Parse FEN / PGN payloads into a normalized seed for HumanVsEngine.

Returns the final FEN, the UCI move list (empty for FEN imports), and a
short human-readable summary used by the client to confirm before commit.
"""
from __future__ import annotations

import io
import re
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


def _parse_pgn_timecontrol(tc: str | None) -> tuple[float | None, float]:
    """Best-effort parser for the PGN [TimeControl] header.

    Recognized: ``"sec"``, ``"sec+inc"``, ``"moves/sec"``, ``"moves/sec+inc"``.
    Returns (initial_seconds, increment_seconds); initial may be None when
    the header is missing/unrecognized. Increment defaults to 0.

    Multi-stage controls (``"40/7200:1800"``) collapse to the first stage.
    """
    if not tc or tc.strip() in ("?", "-"):
        return None, 0.0
    first = tc.split(":", 1)[0].strip()
    head, _, inc_s = first.partition("+")
    seconds_part = head.split("/", 1)[-1].strip()
    try:
        initial = float(seconds_part)
    except ValueError:
        return None, 0.0
    try:
        increment = float(inc_s) if inc_s else 0.0
    except ValueError:
        increment = 0.0
    return initial, increment


# Cutechess / fastchess inline comment: "<eval>/<depth> <time>" at the END
# of the comment (allowing a leading variation in parens or other prose).
# Eval forms: +0.06, -0.44, 0.00, M5, -M3, +M2. Time: integer or float,
# optional 's' or 'ms' suffix. We only care about the time field.
_CUTECHESS_TIME_RE = re.compile(
    r"[+-]?(?:M\d+|\d+(?:\.\d+)?)/\d+\s+(\d+(?:\.\d+)?)\s*(ms|s)?\s*\}?\s*$"
)


def _cutechess_time_seconds(comment: str | None) -> float | None:
    """Best-effort parse of the time-spent field from a cutechess-style
    comment. Returns seconds or None if no match."""
    if not comment:
        return None
    m = _CUTECHESS_TIME_RE.search(comment)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    return v / 1000.0 if m.group(2) == "ms" else v


def parse_fen(text: str) -> ImportedPosition:
    fen = text.strip()
    if not fen:
        raise PositionImportError("empty FEN")
    # Common mistake: paste a PGN into the FEN tab. Detect early and give a
    # clear redirect instead of letting python-chess echo back the full
    # pasted blob in its ValueError.
    if "[Event " in fen or "[White " in fen or "[FEN " in fen:
        raise PositionImportError(
            "This looks like a PGN, not a FEN. Switch to the PGN tab."
        )
    try:
        board = chess.Board(fen)
    except ValueError as e:
        # python-chess embeds the full FEN string in its message; collapse
        # to just the diagnostic so the UI doesn't render a giant blob.
        msg = str(e).split(":", 1)[0] if ":" in str(e) else str(e)
        raise PositionImportError(f"invalid FEN: {msg}") from e
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
    # Reconstruct (white, black) pre-move snapshots. Prefer [%clk] (state)
    # since it's authoritative; fall back to [%emt] (per-move elapsed) when
    # only that is present, deriving remaining clocks via initial+increment
    # from the [TimeControl] header.
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
    else:
        emt_values = [n.emt() for n in nodes]
        tc_initial, tc_increment = _parse_pgn_timecontrol(headers.get("TimeControl"))
        # Third tier: cutechess/fastchess inline "<eval>/<depth> <time>".
        # Only consulted when %clk and %emt are both absent.
        if not any(v is not None for v in emt_values):
            emt_values = [_cutechess_time_seconds(n.comment) for n in nodes]
        if any(v is not None for v in emt_values) and tc_initial is not None:
            clock_history = []
            replay = start_board.copy()
            last_w = tc_initial
            last_b = tc_initial
            for i, node in enumerate(nodes):
                mover_white = (replay.turn == chess.WHITE)
                clock_history.append((last_w, last_b))
                spent = emt_values[i]
                if spent is not None:
                    new_remaining = max(0.0, (last_w if mover_white else last_b)
                                        - spent + tc_increment)
                    if mover_white:
                        last_w = new_remaining
                    else:
                        last_b = new_remaining
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
