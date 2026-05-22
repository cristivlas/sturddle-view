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

from ..chess.board import board_from, side_to_move
from ..chess.pgn_walk import walk_mainline
from ..chess.results import DECISIVE_RESULTS


def explain_invalid(board: chess.Board) -> str:
    """Return a human-readable reason for an invalid board.status(), or
    'illegal position' if no flags are set."""
    status = board.status()
    if status == chess.STATUS_VALID:
        return "illegal position"
    reasons = [s.name.lower().replace("_", " ")
               for s in chess.Status if s != chess.STATUS_VALID and status & s]
    return ", ".join(reasons) if reasons else "illegal position"



@dataclass
class ImportedPosition:
    # None when the import begins at the standard startpos (no PGN FEN header);
    # downstream consumers (e.g. opening-book lookup) treat None as "startpos".
    start_fen: str | None

    moves_uci: list[str]  # UCI moves to replay from start_fen
    final_fen: str  # FEN after replaying moves
    side_to_move: str  # "white" | "black", at the final position
    ply: int  # ply count at the final position
    summary: dict  # {white, black, result, side_to_move}; client formats
    headers: dict[str, str] | None = None  # PGN headers, when applicable
    # Per-ply pre-move (white, black) clock snapshots reconstructed from
    # [%clk] comments. None when the PGN has no clock annotations at all.
    # Inner values may be None for sides that hadn't moved yet at that ply
    # (consumer fills those with the configured TC's initial seconds).
    clock_history: list[tuple[float | None, float | None]] | None = None
    # Live clocks AFTER the final ply. None when not derivable from the PGN.
    final_white_time: float | None = None
    final_black_time: float | None = None
    # Per-ply post-move eval (white POV). None entries when a ply's comment
    # carries no recognizable eval. Whole field is None when the PGN has no
    # evals at all. Each entry: {"cp": int} or {"mate": int}, optional "depth".
    eval_history: list[dict | None] | None = None
    # Per-ply sanitized PGN comments (machine annotations stripped). None
    # entries when a ply has no human-readable commentary. Whole field is
    # None when the PGN carries no commentary at all.
    comments: list[str | None] | None = None
    # Root annotation: pre-game commentary (game.comment) plus the Annotator
    # header, sanitized. None when neither is present.
    root_comment: str | None = None
    # The parsed game tree, kept so downstream hash sites can use
    # canonical_hash_from_game and skip a second chess.pgn.read_game pass.
    # None for FEN imports. Not serialized.
    parsed_game: chess.pgn.Game | None = None


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

# Bare time-only token: "{<time>s}" with no eval/depth prefix. Emitted on
# plies with no engine search (e.g. human moves) so clock info still travels
# under the cutechess convention. python-chess strips the surrounding
# braces, so what we see here is the bare token. Anchored at both ends to
# avoid false-matching prose like "took 7s" or "spent 2.5s thinking".
_CUTECHESS_TIME_ONLY_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(ms|s)\s*$"
)

# Cutechess / fastchess full eval+depth capture (eval and depth groups).
_CUTECHESS_EVAL_RE = re.compile(
    r"(?P<eval>[+-]?(?:M\d+|\d+(?:\.\d+)?))/(?P<depth>\d+)(?:\s+\d+(?:\.\d+)?\s*(?:ms|s)?)?\s*\}?\s*$"
)

# [%eval ...] bracket comment: ChessBase / GBSelect style is "<int_cp>,<depth>"
# (STM POV). Lichess style is "<float_pawns>" or "#<n>" (white POV) with no
# comma. Mate is "#N" or "-#N" (sign optional). Disambiguation is by comma:
# present -- integer-cp STM-POV; absent -- float-pawn white-POV.
_BRACKET_EVAL_RE = re.compile(r"\[%eval\s+(?P<body>[^\]]+)\]")

# Strip any [%key ...] bracket annotation (clk, emt, eval, cal, csl, ...).
_BRACKET_TAG_RE = re.compile(r"\[%[^\]]*\]")
# Strip parenthesized inline variations. Non-greedy; nested parens are rare
# in PGN comments -- python-chess parses RAVs as sibling nodes, not text.
_PAREN_VAR_RE = re.compile(r"\([^()]*\)")
_WS_RE = re.compile(r"\s+")


def _sanitize_comment(comment: str | None) -> str | None:
    """Strip machine annotations from a PGN comment, leaving human prose.
    Removes [%...] bracket tags, parenthesized variations, and the
    cutechess "<eval>/<depth> <time>" trailing token. Returns None when
    nothing readable remains."""
    if not comment:
        return None
    s = _BRACKET_TAG_RE.sub(" ", comment)
    s = _CUTECHESS_EVAL_RE.sub(" ", s)
    s = _CUTECHESS_TIME_ONLY_RE.sub(" ", s)
    # Repeatedly strip innermost parens so adjacent variations all go.
    while True:
        new = _PAREN_VAR_RE.sub(" ", s)
        if new == s:
            break
        s = new
    # Preserve paragraph breaks (blank lines) for rendering, collapse other
    # whitespace runs within each paragraph.
    paragraphs = [_WS_RE.sub(" ", p).strip() for p in re.split(r"\n\s*\n", s)]
    paragraphs = [p for p in paragraphs if p]
    if not paragraphs:
        return None
    # Stripping a leading [%clk]/[%eval]/etc often exposes a lowercase first
    # word ("best move"); capitalize it. Only the first paragraph, and only
    # when the first alpha char is currently lowercase.
    first = paragraphs[0]
    for i, ch in enumerate(first):
        if ch.isalpha():
            if ch.islower():
                paragraphs[0] = first[:i] + ch.upper() + first[i + 1:]
            break
    return "\n\n".join(paragraphs)


def _cutechess_time_seconds(comment: str | None) -> float | None:
    """Best-effort parse of the time-spent field from a cutechess-style
    comment. Returns seconds or None if no match.

    Accepts both the full form ``{<eval>/<depth> <time>s}`` and the
    time-only variant ``{<time>s}`` we emit on plies with no engine
    search.
    """
    if not comment:
        return None
    m = _CUTECHESS_TIME_RE.search(comment)
    if m is None:
        m = _CUTECHESS_TIME_ONLY_RE.search(comment)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    return v / 1000.0 if m.group(2) == "ms" else v


def _parse_eval_token(tok: str) -> dict | None:
    """Parse a single eval token into ``{"cp": int}`` or ``{"mate": int}``.
    Token may be ``+0.34``, ``-1.85``, ``225`` (cp), or ``#5``/``-#3``/``M5``/``-M3``.
    POV is the caller's responsibility -- this only extracts the magnitude
    and sign as written.
    Returns None on parse failure.
    """
    s = tok.strip()
    if not s:
        return None
    sign = 1
    if s.startswith("-"):
        sign = -1
        s = s[1:]
    elif s.startswith("+"):
        s = s[1:]
    # Mate: #N or MN
    if s.startswith("#") or s.startswith("M"):
        try:
            n = int(s[1:])
        except ValueError:
            return None
        return {"mate": sign * n}
    # Float pawns vs integer centipawns: a decimal point (or value < ~50)
    # implies pawns. Without a decimal we can't tell, but in practice:
    # - Cutechess uses dotted floats (+0.34).
    # - GBSelect/ChessBase uses raw int cp (225).
    # The bracket parser passes the format hint via caller.
    try:
        f = float(s)
    except ValueError:
        return None
    if "." in s:
        return {"cp": int(round(sign * f * 100))}
    return {"cp": sign * int(f)}


def _flip_pov(score: dict) -> dict:
    """Negate cp/mate for STM->white POV conversion."""
    if "cp" in score:
        return {"cp": -score["cp"]}
    if "mate" in score:
        return {"mate": -score["mate"]}
    return score


def _parse_pgn_eval(comment: str | None, mover_white: bool) -> dict | None:
    """Extract a per-ply eval from a PGN move comment, normalized to white
    POV. Returns ``{"cp": int}`` or ``{"mate": int}`` (optionally with
    ``"depth": int``), or None when no recognizable eval is present.

    Recognized formats (in order):
    1. ``[%eval CP,DEPTH]`` -- integer cp + depth, STM POV (GBSelect/CB).
       Mate: ``[%eval #N,DEPTH]`` or ``[%eval -#N,DEPTH]``.
    2. ``[%eval VALUE]`` (no comma) -- Lichess float pawns or ``#N``,
       white POV.
    3. Cutechess/fastchess trailing ``<eval>/<depth>`` -- float pawns or
       ``M<n>``, STM POV.
    """
    if not comment:
        return None

    # 1 & 2: bracket [%eval ...]
    m = _BRACKET_EVAL_RE.search(comment)
    if m:
        body = m.group("body").strip()
        if "," in body:
            # GBSelect/ChessBase: integer cp + depth, STM POV.
            head, _, depth_s = body.partition(",")
            score = _parse_eval_token(head)
            if score is not None:
                if not mover_white:
                    score = _flip_pov(score)
                try:
                    score["depth"] = int(depth_s.strip())
                except ValueError:
                    pass
                return score
        else:
            # Lichess: float pawns or mate, white POV (no flip).
            score = _parse_eval_token(body)
            if score is not None:
                return score

    # 3: cutechess trailing token
    m = _CUTECHESS_EVAL_RE.search(comment)
    if m:
        score = _parse_eval_token(m.group("eval"))
        if score is not None:
            if not mover_white:
                score = _flip_pov(score)
            try:
                score["depth"] = int(m.group("depth"))
            except ValueError:
                pass
            return score

    return None


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
        board = board_from(fen)
    except ValueError as e:
        # python-chess embeds the full FEN string in its message; collapse
        # to just the diagnostic so the UI doesn't render a giant blob.
        msg = str(e).split(":", 1)[0] if ":" in str(e) else str(e)
        raise PositionImportError(f"invalid FEN: {msg}") from e
    if not board.is_valid():
        raise PositionImportError("illegal position (e.g. adjacent kings, too many pieces, pawns on back rank)")
    side = side_to_move(board)
    # Treat the standard startpos as None so opening-book lookup engages
    # on subsequent moves (lookup keys on move history from startpos).
    is_startpos = board.fen() == chess.STARTING_FEN
    return ImportedPosition(
        start_fen=None if is_startpos else board.fen(),
        moves_uci=[],
        final_fen=board.fen(),
        side_to_move=side,
        ply=board.ply(),
        summary={
            "white": None,
            "black": None,
            "result": None,
            "side_to_move": side,
            "fen": board.fen(),
        },
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
        start_board = board_from(start_fen_header)
    except ValueError as e:
        raise PositionImportError(f"PGN has invalid starting FEN header: {e}") from e
    if start_fen_header and not start_board.is_valid():
        raise PositionImportError("PGN has illegal starting position in FEN header")
    moves_uci: list[str] = []
    board: chess.Board | None = None
    nodes: list[chess.pgn.ChildNode] = []
    movers_white: list[bool] = []
    try:
        for node, board, mover_white in walk_mainline(game, start_board=start_board.copy()):
            moves_uci.append(node.move.uci())
            nodes.append(node)
            movers_white.append(mover_white)
    except chess.IllegalMoveError as e:
        raise PositionImportError(f"illegal move in PGN at ply {len(moves_uci) + 1}: {e}") from e
    if board is None:
        board = start_board.copy()
    # python-chess's PGN parser is lenient: arbitrary text yields a valid
    # game with no moves and a startpos board. Reject that — an "import"
    # that just gets you to startpos is the New Game button.
    if not moves_uci and not start_fen_header:
        raise PositionImportError("PGN contains no moves")
    side = side_to_move(board)
    white = headers.get("White", "?")
    black = headers.get("Black", "?")
    result = headers.get("Result", "*")
    summary = {
        "white": white if white != "?" else None,
        "black": black if black != "?" else None,
        "result": result if result in DECISIVE_RESULTS else None,
        "side_to_move": side,
    }
    # Reconstruct (white, black) pre-move snapshots. Prefer [%clk] (state)
    # since it's authoritative; fall back to [%emt] (per-move elapsed) when
    # only that is present, deriving remaining clocks via initial+increment
    # from the [TimeControl] header.
    clk_values = [n.clock() for n in nodes]
    # Per-ply eval, white POV. Extracted on the same node walk so the STM
    # flip can use the side-to-move at each ply.
    eval_per_ply: list[dict | None] = []
    clock_history: list[tuple[float, float]] | None = None
    final_white = final_black = None
    if any(v is not None for v in clk_values):
        clock_history = []
        last_w = last_b = None  # last known post-move clock per side
        for i, (node, mover_white) in enumerate(zip(nodes, movers_white)):
            # Pre-move snapshot for ply i = last known clocks for each side.
            clock_history.append((last_w, last_b))
            after = clk_values[i]
            if after is not None:
                if mover_white:
                    last_w = after
                else:
                    last_b = after
            eval_per_ply.append(_parse_pgn_eval(node.comment, mover_white))
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
            last_w = tc_initial
            last_b = tc_initial
            for i, (node, mover_white) in enumerate(zip(nodes, movers_white)):
                clock_history.append((last_w, last_b))
                spent = emt_values[i]
                if spent is not None:
                    new_remaining = max(0.0, (last_w if mover_white else last_b)
                                        - spent + tc_increment)
                    if mover_white:
                        last_w = new_remaining
                    else:
                        last_b = new_remaining
                eval_per_ply.append(_parse_pgn_eval(node.comment, mover_white))
            final_white, final_black = last_w, last_b
        else:
            # No clock info but we still want evals if any are present.
            for node, mover_white in zip(nodes, movers_white):
                eval_per_ply.append(_parse_pgn_eval(node.comment, mover_white))
    eval_history: list[dict | None] | None = (
        eval_per_ply if any(e is not None for e in eval_per_ply) else None
    )
    # Per-ply sanitized comments (run AFTER eval extraction so the bracket
    # tags are still parseable above). Comments are independent of clock
    # branches above -- they apply to every node walked.
    comments_per_ply = [_sanitize_comment(n.comment) for n in nodes]
    comments: list[str | None] | None = (
        comments_per_ply if any(c is not None for c in comments_per_ply) else None
    )
    # Root comment: pre-game prose plus the Annotator header, sanitized.
    annotator = headers.get("Annotator", "").strip()
    pieces = []
    if annotator and annotator != "?":
        pieces.append(f"Annotator: {annotator}")
    root_raw = (game.comment or "").strip()
    if root_raw:
        cleaned = _sanitize_comment(root_raw)
        if cleaned:
            pieces.append(cleaned)
    root_comment = "\n\n".join(pieces) if pieces else None
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
        eval_history=eval_history,
        comments=comments,
        root_comment=root_comment,
        parsed_game=game,
    )
