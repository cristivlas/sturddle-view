"""Server-side validation of model prose against the live position.

Two pure functions over (text, board) -> list[str]:

- `find_illegal_moves`: SAN-shaped tokens in the prose that fail
  board.parse_san() (piece moves, pawn captures, castling).
- `find_false_piece_claims`: "<piece> on <square>" phrases whose
  square does not actually hold that piece in the live position.

Empty list means the prose is consistent. The coordinator combines
both and injects a corrective user message when either fires.
"""
from __future__ import annotations

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
_SAN_TOKEN_RE = re.compile(
    r"\b("
    r"(?:O-O-O|O-O"                                       # castling
    r"|[KQRBN][a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?[+#]?"  # piece move
    r"|[a-h]x[a-h][1-8](?:=[QRBN])?[+#]?)"                # pawn capture only
    r"[!?]{0,2}"                                          # annotation glyphs
    r")"
)


# Tokens that look like SAN but are not. The parser would reject these
# anyway, but skipping them up front cuts noise in transcripts.
_NON_MOVE_LITERALS = frozenset({
    # Common chess prose that can match piece-move regex.
    # (Add here only when a real-world prose example trips a false positive.)
})


def find_illegal_moves(text: str, board: chess.Board) -> list[str]:
    """Return distinct illegal SAN tokens found in `text` (order of first
    appearance, no duplicates). Tokens that don't parse as SAN at all are
    ignored -- only well-formed moves the board rejects count as illegal.

    Annotation glyphs (!, ?, !?, !!) are stripped before parsing but
    preserved in the returned token so the corrective message echoes
    what the model wrote.
    """
    seen: set[str] = set()
    illegal: list[str] = []
    for match in _SAN_TOKEN_RE.finditer(text):
        token = match.group(0)
        if token in seen or token in _NON_MOVE_LITERALS:
            continue
        seen.add(token)
        bare = _strip_annotation_glyphs(token)
        try:
            board.parse_san(bare)
        except chess.IllegalMoveError:
            illegal.append(token)
        except (chess.InvalidMoveError, chess.AmbiguousMoveError):
            # Not a move at all, or ambiguous (which is the model's
            # problem to disambiguate, not ours to flag as illegal).
            continue
    return illegal


_GLYPH_RE = re.compile(r"[!?]{1,2}$")


def _strip_annotation_glyphs(token: str) -> str:
    return _GLYPH_RE.sub("", token)


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


def find_false_piece_claims(text: str, board: chess.Board) -> list[str]:
    """Return distinct false 'piece on square' claims (order of first
    appearance). A claim is false when the named square is empty or
    holds a piece of a different type. When the claim names a color,
    a mismatched color also counts as false. Two phrasings recognized:
    "<piece> on <square>" and "<square> <piece>".
    """
    seen: set[str] = set()
    false: list[str] = []
    for color_word, piece_word, square_name in _iter_piece_claims(text):
        key = f"{color_word}|{piece_word}|{square_name}"
        if key in seen:
            continue
        seen.add(key)
        piece_type = _PIECE_WORDS[piece_word]
        square = chess.parse_square(square_name)
        actual = board.piece_at(square)
        prefix = f"{color_word} " if color_word else ""
        label = f"{prefix}{piece_word} on {square_name}"
        if actual is None or actual.piece_type != piece_type:
            false.append(label)
            continue
        if color_word and actual.color != _COLOR_WORDS[color_word]:
            false.append(label)
    return false
