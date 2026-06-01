"""Server-side validation of model prose against position(s).

Pure functions over (text, boards) -> list[str]. A claim is invalid
only when it matches no board in the sequence. Play mode passes
[live_board]; view mode passes [current, current.pop(), ...] so a
commentator's reference to an earlier-position piece isn't flagged.
"""
from __future__ import annotations

from typing import Sequence

import re

import chess


# SAN-like token recognizer. Captures piece moves (Nf3, Qxh7+, R1d2),
# castles (O-O, O-O-O), pawn captures (exd5, exd1=N+), and trailing
# annotation glyphs (!, ?, !?, ?!, !!, ??).
#
# Bare-square pawn moves (e4, h6, ...) are intentionally NOT matched:
# chess prose mentions squares constantly ("the h6 pawn", "targets g7"),
# and a bare square has no syntactic marker to distinguish description
# from move. Loss: cannot flag a model that writes "play e5" when e5
# is illegal. Mitigation: prompt the model to name pawn moves with
# capture notation or a piece-style cue when feasible. Same scoping
# convention python-chess and PGN parsers use.
#
# Glyphs are inside the captured group so they survive to the return
# value (the corrective message echoes them). Intentionally over-accepts:
# parse_san() is the authoritative filter.
# Shared SAN sub-patterns, factored so the per-token and continuation
# recognizers can't drift. All groups non-capturing so finditer/findall
# return whole tokens.
_SAN_CASTLE = r"O-O-O|O-O"
_SAN_PIECE_MOVE = r"[KQRBN][a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?[+#]?"
_SAN_PAWN_CAPTURE = r"[a-h]x[a-h][1-8](?:=[QRBN])?[+#]?"
_SAN_PAWN_PUSH = r"[a-h][1-8](?:=[QRBN])?[+#]?"
_SAN_GLYPHS = r"[!?]{0,2}"

_SAN_TOKEN_RE = re.compile(
    rf"\b((?:{_SAN_CASTLE}|{_SAN_PIECE_MOVE}|{_SAN_PAWN_CAPTURE}){_SAN_GLYPHS})"
)


# Tokens that look like SAN but are not. The parser would reject these
# anyway, but skipping them up front cuts noise in transcripts.
_NON_MOVE_LITERALS = frozenset({
    # Common chess prose that can match piece-move regex.
    # (Add here only when a real-world prose example trips a false positive.)
})


_PIECE_LETTER_TO_TYPE = {
    "K": chess.KING,
    "Q": chess.QUEEN,
    "R": chess.ROOK,
    "B": chess.BISHOP,
    "N": chess.KNIGHT,
}


def _is_san_label(bare: str, board: chess.Board) -> bool:
    """True when `bare` is a 'piece+square' SAN-shape that names a piece
    already on the named square for the side to move -- e.g. 'Qd1' when
    the queen is on d1. Such tokens are labels in prose, not move
    proposals, and should not be flagged as illegal."""
    if len(bare) != 3:
        return False
    piece_char = bare[0]
    if piece_char not in _PIECE_LETTER_TO_TYPE:
        return False
    try:
        square = chess.parse_square(bare[1:3])
    except ValueError:
        return False
    piece = board.piece_at(square)
    if piece is None:
        return False
    if piece.piece_type != _PIECE_LETTER_TO_TYPE[piece_char]:
        return False
    return piece.color == board.turn


def find_illegal_moves(
    text: str,
    boards: Sequence[chess.Board],
    extra_boards: Sequence[chess.Board] = (),
) -> list[str]:
    """Return distinct illegal SAN tokens. `boards` is ordered current
    first then prior positions. A token passes iff legal on the current
    board, parses on a prior board to a move actually played in this
    game, OR is legal on any `extra_board` (positions the model examined
    via tool calls, where a projected move is legitimate reasoning).
    Legal-but-never-played alternatives are flagged."""
    seen: set[str] = set()
    illegal: list[str] = []
    for match in _SAN_TOKEN_RE.finditer(text):
        token = match.group(0)
        if token in seen or token in _NON_MOVE_LITERALS:
            continue
        seen.add(token)
        bare = _strip_annotation_glyphs(token)
        if _token_legal_or_played(bare, boards, extra_boards):
            continue
        if any(_token_is_illegal(bare, b) for b in boards):
            illegal.append(token)
    return illegal


def _token_legal_or_played(
    bare: str,
    boards: Sequence[chess.Board],
    extra_boards: Sequence[chess.Board] = (),
) -> bool:
    """True iff legal on the current board, parses on a prior board to
    the move actually played from there, or is legal on any extra board.
    Label carve-out applies on the current board only."""
    if not boards:
        return False
    current = boards[0]
    try:
        current.parse_san(bare)
        return True
    except chess.IllegalMoveError:
        if _is_san_label(bare, current):
            return True
    except (chess.InvalidMoveError, chess.AmbiguousMoveError):
        pass
    for i in range(1, len(boards)):
        prior = boards[i]
        played = _played_move_from_prior(boards, i)
        if played is None:
            continue
        try:
            parsed = prior.parse_san(bare)
        except (chess.IllegalMoveError, chess.InvalidMoveError, chess.AmbiguousMoveError):
            continue
        if parsed == played:
            return True
    for extra in extra_boards:
        try:
            extra.parse_san(bare)
            return True
        except (chess.IllegalMoveError, chess.InvalidMoveError, chess.AmbiguousMoveError):
            continue
    return False


def _played_move_from_prior(boards: Sequence[chess.Board], i: int) -> chess.Move | None:
    """Move played from boards[i] to reach boards[i-1] (i.e., the last
    move pushed on boards[i-1])."""
    newer = boards[i - 1]
    if not newer.move_stack:
        return None
    return newer.move_stack[-1]


def _token_is_illegal(bare: str, board: chess.Board) -> bool:
    try:
        board.parse_san(bare)
    except chess.IllegalMoveError:
        return not _is_san_label(bare, board)
    except (chess.InvalidMoveError, chess.AmbiguousMoveError):
        return False
    return False


_GLYPH_RE = re.compile(r"[!?]{1,2}$")


def _strip_annotation_glyphs(token: str) -> str:
    return _GLYPH_RE.sub("", token)


# A single move inside a continuation. Reuses the shared SAN fragments
# but adds bare pawn pushes (e4): inside a whitespace-delimited run a
# push reads as a move, not a prose square reference.
_CONT_MOVE = (
    rf"(?:{_SAN_CASTLE}|{_SAN_PIECE_MOVE}|{_SAN_PAWN_CAPTURE}|{_SAN_PAWN_PUSH})"
    rf"{_SAN_GLYPHS}"
)
# Per-move move-number prefix: "1.", "12.", "3...". Captured so we can
# tell a fully-numbered white-only line ("1.e4 2.Nf3", skipping Black)
# from a true ply sequence.
_MOVE_NUM = r"(\d+\.(?:\.\.)?\s*)?"
# Two or more moves separated only by whitespace and optional move
# numbers. A prose word between moves breaks the run, so only genuine
# lines (not "Nf3 is strong, and Bb5 too") are captured.
_CONTINUATION_RE = re.compile(
    rf"\b{_MOVE_NUM}(?:{_CONT_MOVE})(?:\s+{_MOVE_NUM}(?:{_CONT_MOVE}))+"
)
# Splits a matched run into (move_number_or_empty, move) pairs.
_CONT_PAIR_RE = re.compile(rf"{_MOVE_NUM}({_CONT_MOVE})")
# A run is a real line (not prose listing squares) only with a move
# number, piece letter, castle, capture, or promotion somewhere in it;
# a bare-pawn-square-only run ("c3 c4", "f5 e6") is descriptive prose.
_LINE_PROOF_RE = re.compile(r"\d+\.|[KQRBN]|O-O|[a-h]x|=[QRBN]")


def find_illegal_continuations(
    text: str,
    boards: Sequence[chess.Board],
    extra_boards: Sequence[chess.Board] = (),
) -> list[str]:
    """Return continuation runs (2+ moves) that do not play cleanly as a
    sequence from any anchor whose position the run's first move is legal
    in. `boards` is current-first; `extra_boards` are examined positions.

    Per-token validation accepts each move if it is legal on some board;
    a run can pass that way while being an incoherent line (each move
    legal alone, illegal in order). Two guards keep prose from being read
    as a line: the run must contain a line-proof token (number, piece,
    castle, capture, promotion), and its first move must be legal on at
    least one anchor -- a floating quote we cannot place is left alone.
    Fully per-move-numbered white-only lines ("1.e4 2.Nf3") are skipped:
    they omit Black's plies, so they are not a ply sequence to replay."""
    anchors = list(boards) + list(extra_boards)
    if not anchors:
        return []
    seen: set[str] = set()
    illegal: list[str] = []
    for match in _CONTINUATION_RE.finditer(text):
        run = match.group(0)
        pairs = _CONT_PAIR_RE.findall(run)
        moves = [_strip_annotation_glyphs(move) for _num, move in pairs]
        if len(moves) < 2 or _every_move_numbered(pairs):
            continue
        key = " ".join(moves)
        if key in seen or not _LINE_PROOF_RE.search(run):
            continue
        seen.add(key)
        candidates = [a for a in anchors if _move_legal(moves[0], a)]
        if candidates and not any(_line_plays(moves, a) for a in candidates):
            illegal.append(key)
    return illegal


def _every_move_numbered(pairs: Sequence[tuple[str, str]]) -> bool:
    """True iff every move in the run carries its own move number, i.e.
    white-only shorthand ("1.e4 2.Nf3") that skips Black's replies."""
    return len(pairs) >= 2 and all(num for num, _move in pairs)


def _move_legal(san: str, board: chess.Board) -> bool:
    try:
        board.parse_san(san)
    except (chess.IllegalMoveError, chess.InvalidMoveError, chess.AmbiguousMoveError):
        return False
    return True


def _line_plays(moves: Sequence[str], anchor: chess.Board) -> bool:
    """True iff every move parses+pushes in order from a copy of `anchor`."""
    board = anchor.copy()
    for san in moves:
        if not _move_legal(san, board):
            return False
        board.push_san(san)
    return True


# Natural-language castle mention -- models often write "castle"
# instead of "O-O". False positive risk: "castles" colloquially names
# rook pieces ("Black's castles guard the back rank") -- rare in prose.
_CASTLE_WORD_RE = re.compile(r"\b(castles?|castling|castled)\b", re.IGNORECASE)


def _any_castle_legal(board: chess.Board) -> bool:
    for move in board.legal_moves:
        if board.is_castling(move):
            return True
    # Copy so flipping turn doesn't mutate the caller's board.
    other = board.copy(stack=False)
    other.turn = not other.turn
    for move in other.legal_moves:
        if other.is_castling(move):
            return True
    return False


def find_castle_word_violations(
    text: str, boards: Sequence[chess.Board],
) -> list[str]:
    """Castle-word mentions when castling is legal in NO board.
    Empty when at least one board allows castling for some side."""
    matches = list(_CASTLE_WORD_RE.finditer(text))
    if not matches:
        return []
    if any(_any_castle_legal(b) for b in boards):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in matches:
        token = m.group(0)
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
    return out


# Piece-on-square claim recognizer. Two phrasings, both common in
# chess prose:
#   1. "<piece> on <square>"  -- "the knight on f1", "White's bishop on d2"
#   2. "<square> <piece>"      -- "the b5 pawn", "Black's g1 knight"
# Optional leading color (with or without possessive 's) and optional
# article ("the"). Case-insensitive on words, sensitive on coords.
_PIECE_WORDS = {
    "king":   chess.KING,
    "queen":  chess.QUEEN,
    "rook":   chess.ROOK,
    "bishop": chess.BISHOP,
    "knight": chess.KNIGHT,
    "pawn":   chess.PAWN,
}
_COLOR_WORDS = {"white": chess.WHITE, "black": chess.BLACK}
_PIECE_ALT = "|".join(_PIECE_WORDS)
_COLOR_OPT = r"(?:(white|black)(?:'s)?\s+)?(?:the\s+)?"
# Group layout (positional): (color, piece, square) in both regexes.
_PIECE_ON_SQUARE_RE = re.compile(
    rf"\b{_COLOR_OPT}({_PIECE_ALT})\s+on\s+([a-h][1-8])\b",
    re.IGNORECASE,
)
_SQUARE_PIECE_RE = re.compile(
    rf"\b{_COLOR_OPT}([a-h][1-8])\s+({_PIECE_ALT})\b",
    re.IGNORECASE,
)


def _iter_piece_claims(text: str):
    """Yield (color_word, piece_word, square_name) for every claim
    matched by either phrasing regex. Positional groups: both regexes
    expose (color, piece-or-square, square-or-piece) in a known order;
    we adapt per regex."""
    for m in _PIECE_ON_SQUARE_RE.finditer(text):
        color, piece, square = m.group(1), m.group(2), m.group(3)
        yield (color or "").lower(), piece.lower(), square.lower()
    for m in _SQUARE_PIECE_RE.finditer(text):
        color, square, piece = m.group(1), m.group(2), m.group(3)
        yield (color or "").lower(), piece.lower(), square.lower()


# Bare-square pawn-push token; carve-out-only (bare pushes are still
# excluded from illegal-move detection). Trailing lookahead instead of
# \b so glyphs at the end ('e8=Q+') don't break the boundary.
_BARE_PAWN_PUSH_RE = re.compile(r"\b[a-h][1-8](?:=[QRBN])?[+#]?(?![a-zA-Z0-9])")


def _add_move_target(
    out: set[tuple[int, chess.Color, int]], token: str, board: chess.Board,
) -> None:
    try:
        move = board.parse_san(token)
    except (chess.IllegalMoveError, chess.InvalidMoveError, chess.AmbiguousMoveError):
        return
    piece = board.piece_at(move.from_square)
    if piece is None:
        return
    dest_piece_type = move.promotion if move.promotion else piece.piece_type
    out.add((dest_piece_type, piece.color, move.to_square))


def _move_target_set(text: str, board: chess.Board) -> set[tuple[int, chess.Color, int]]:
    """Scan `text` for tokens that parse as legal moves on `board`;
    return the set of (piece_type, color, dest_square) tuples those
    moves would create on the post-move board. Carves out forward-looking
    prose (post-Re1 "the rook on e1", post-e4 "the e4 pawn") from
    false-piece-claim flagging."""
    out: set[tuple[int, chess.Color, int]] = set()
    for match in _SAN_TOKEN_RE.finditer(text):
        _add_move_target(out, _strip_annotation_glyphs(match.group(0)), board)
    for match in _BARE_PAWN_PUSH_RE.finditer(text):
        # Strip kept for symmetry; _BARE_PAWN_PUSH_RE doesn't capture !?.
        _add_move_target(out, _strip_annotation_glyphs(match.group(0)), board)
    return out


def find_false_piece_claims(
    text: str,
    boards: Sequence[chess.Board],
    extra_boards: Sequence[chess.Board] = (),
) -> list[str]:
    """False 'piece on square' claims. A claim is false only when NO
    board in `boards` or `extra_boards` matches it (empty square or wrong
    piece/color in all). `extra_boards` are positions the model examined
    via tool calls, so a piece named in a projected line isn't flagged.
    The forward-looking carve-out uses `boards[-1]` (current) since
    post-move targets are derived from the latest position."""
    current = boards[-1]
    targets = _move_target_set(text, current)
    seen: set[str] = set()
    false: list[str] = []
    for color_word, piece_word, square_name in _iter_piece_claims(text):
        key = f"{color_word}|{piece_word}|{square_name}"
        if key in seen:
            continue
        seen.add(key)
        piece_type = _PIECE_WORDS[piece_word]
        square = chess.parse_square(square_name)
        claim_color = _COLOR_WORDS[color_word] if color_word else current.turn
        if (piece_type, claim_color, square) in targets:
            continue
        if _claim_holds_on_any(boards, piece_type, square, color_word):
            continue
        if _claim_holds_on_any(extra_boards, piece_type, square, color_word):
            continue
        prefix = f"{color_word} " if color_word else ""
        false.append(f"{prefix}{piece_word} on {square_name}")
    return false


def _claim_holds_on_any(
    boards: Sequence[chess.Board],
    piece_type: int,
    square: int,
    color_word: str,
) -> bool:
    """True iff some board has a piece of `piece_type` on `square`,
    matching the optional color word."""
    for board in boards:
        actual = board.piece_at(square)
        if actual is None or actual.piece_type != piece_type:
            continue
        if color_word and actual.color != _COLOR_WORDS[color_word]:
            continue
        return True
    return False
