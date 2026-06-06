"""Lightweight prose check against the single current board.

Pure functions over (text, board) -> list[str]. A claim is flagged when
it does not match the board at the cursor. Unlike the removed multi-board
validator, there is no history walk and no examined-position carve-out:
the coordinator surfaces a flag as a clarifying question ("do you mean a
past or hypothetical position?"), not a rewrite demand, so a legitimate
reference to another position is the model's to explain rather than ours
to prove.
"""
from __future__ import annotations

import re

import chess


# Shared SAN sub-patterns, factored so the per-token and continuation
# recognizers can't drift. All groups non-capturing so finditer/findall
# return whole tokens.
_SAN_CASTLE = r"O-O-O|O-O"
_SAN_PIECE_MOVE = r"[KQRBN][a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?[+#]?"
_SAN_PAWN_CAPTURE = r"[a-h]x[a-h][1-8](?:=[QRBN])?[+#]?"
_SAN_PAWN_PUSH = r"[a-h][1-8](?:=[QRBN])?[+#]?"
_SAN_GLYPHS = r"[!?]{0,2}"


# SAN-like token recognizer. Bare pawn pushes (e4) are NOT matched: prose
# names squares constantly with no marker to tell description from move.
# A leading "..." marks a Black move per SAN convention ("...Nd3"). When the
# "..." is glued to a preceding word ("develops...Nf3") it is ambiguous --
# trailing punctuation or the marker -- and we deliberately read it as the
# marker (validate from Black). Not a bug: do not "fix" the boundary.
_SAN_TOKEN_RE = re.compile(
    rf"(\.\.\.)?\b((?:{_SAN_CASTLE}|{_SAN_PIECE_MOVE}|{_SAN_PAWN_CAPTURE}){_SAN_GLYPHS})"
)


def _pov_for(prefix: str | None, board: chess.Board) -> chess.Color:
    """Side a SAN is validated from: Black when the prose marks it with a
    leading "..." (PGN convention for a Black move), else the side to move."""
    return chess.BLACK if prefix else board.turn


def _board_for_pov(board: chess.Board, color: chess.Color) -> chess.Board | None:
    """A copy of `board` with `color` to move. None when flipping the turn
    would be illegal (the side to move is in check, so the other side can't
    be on move). The same-color case returns the board unflipped."""
    if board.turn == color:
        return board
    if board.is_check():
        return None
    probe = board.copy(stack=False)
    probe.turn = color
    return probe


_PIECE_LETTER_TO_TYPE = {
    "K": chess.KING,
    "Q": chess.QUEEN,
    "R": chess.ROOK,
    "B": chess.BISHOP,
    "N": chess.KNIGHT,
}


_GLYPH_RE = re.compile(r"[!?]{1,2}$")


def _strip_annotation_glyphs(token: str) -> str:
    return _GLYPH_RE.sub("", token)


_SAN_LABEL_RE = re.compile(r"^([KQRBN])[a-h]?[1-8]?([a-h][1-8])$")


def _is_san_label(bare: str, board: chess.Board) -> bool:
    """True when `bare` is a 'piece(+disambiguator)+square' SAN-shape naming a
    piece already on that square for the side to move -- 'Qd1' (queen on d1) or
    the disambiguated 'Ngf3' (knight on f3). Such tokens are labels in prose,
    not move proposals. The trailing two chars are always the named square."""
    m = _SAN_LABEL_RE.match(bare)
    if m is None:
        return False
    square = chess.parse_square(m.group(2))
    piece = board.piece_at(square)
    if piece is None:
        return False
    if piece.piece_type != _PIECE_LETTER_TO_TYPE[m.group(1)]:
        return False
    return piece.color == board.turn


def _parse_san_real(board: chess.Board, bare: str) -> chess.Move | None:
    """parse_san, but reject a phantom capture: `x` in the token while the
    parsed move captures nothing. python-chess parses 'Bxe4' onto an empty
    e4 as a quiet move, masking a false capture claim."""
    try:
        move = board.parse_san(bare)
    except (chess.IllegalMoveError, chess.InvalidMoveError, chess.AmbiguousMoveError):
        return None
    if "x" in bare and not board.is_capture(move):
        return None
    return move


def _token_is_illegal(bare: str, board: chess.Board) -> bool:
    try:
        move = board.parse_san(bare)
    except chess.IllegalMoveError:
        return not _is_san_label(bare, board)
    except (chess.InvalidMoveError, chess.AmbiguousMoveError):
        return False
    # Parsed cleanly -- illegal only if it claims a capture but takes nothing.
    return "x" in bare and not board.is_capture(move)


def iter_illegal_moves(text: str, board: chess.Board):
    """Yield (surface, label) for each illegal SAN token. `surface` is the
    exact prose span (with "..." and glyphs) to strike; `label` the bare token.
    A leading "..." validates from Black's POV ("...Nd3"), else side to move.
    Legal moves, square-labels ('Qd1'), and phantom-capture-free parses pass."""
    seen: set[str] = set()
    for match in _SAN_TOKEN_RE.finditer(text):
        prefix, token = match.group(1), match.group(2)
        if token in seen:
            continue
        seen.add(token)
        bare = _strip_annotation_glyphs(token)
        pov_board = _board_for_pov(board, _pov_for(prefix, board))
        if pov_board is None:
            continue
        if _parse_san_real(pov_board, bare) is not None:
            continue
        if _is_san_label(bare, pov_board):
            continue
        if _token_is_illegal(bare, pov_board):
            yield match.group(0), bare


def find_illegal_moves(text: str, board: chess.Board) -> list[str]:
    """Bare illegal-move labels (see iter_illegal_moves for the surface form)."""
    return [label for _surface, label in iter_illegal_moves(text, board)]


# A single move inside a continuation. Adds bare pawn pushes (e4): inside a
# whitespace-delimited run a push reads as a move, not a square reference.
_CONT_MOVE = (
    rf"(?:{_SAN_CASTLE}|{_SAN_PIECE_MOVE}|{_SAN_PAWN_CAPTURE}|{_SAN_PAWN_PUSH})"
    rf"{_SAN_GLYPHS}"
)
# Per-move move-number prefix: "1.", "12.", "3...". Captured so a white-only
# shorthand ("1.e4 2.Nf3", skipping Black) can be told from a true sequence.
_MOVE_NUM = r"(\d+\.(?:\.\.)?\s*)?"
# Two or more moves separated only by whitespace and optional move numbers.
# A prose word between moves breaks the run, so only genuine lines (not
# "Nf3 is strong, and Bb5 too") are captured.
_CONTINUATION_RE = re.compile(
    rf"\b{_MOVE_NUM}(?:{_CONT_MOVE})(?:\s+{_MOVE_NUM}(?:{_CONT_MOVE}))+"
)
_CONT_PAIR_RE = re.compile(rf"{_MOVE_NUM}({_CONT_MOVE})")
# A run is a real line (not prose listing squares) only with a move number,
# piece letter, castle, capture, or promotion somewhere in it.
_LINE_PROOF_RE = re.compile(r"\d+\.|[KQRBN]|O-O|[a-h]x|=[QRBN]")


def _every_move_numbered(pairs) -> bool:
    """True iff every move carries its own move number, i.e. white-only
    shorthand ("1.e4 2.Nf3") that skips Black's replies."""
    return len(pairs) >= 2 and all(num for num, _move in pairs)


def _move_legal(san: str, board: chess.Board) -> bool:
    try:
        board.parse_san(san)
    except (chess.IllegalMoveError, chess.InvalidMoveError, chess.AmbiguousMoveError):
        return False
    return True


def _line_plays(moves, board: chess.Board) -> bool:
    """True iff every move parses+pushes in order from a copy of `board`."""
    walker = board.copy()
    for san in moves:
        if not _move_legal(san, walker):
            return False
        walker.push_san(san)
    return True


def iter_illegal_continuations(text: str, board: chess.Board):
    """Yield (surface, label) for each illegal continuation run (2+ moves):
    first move legal on `board` but the run does not replay cleanly in order.
    `surface` is the matched run; `label` the normalized "Nf3 Nc6" key. A line
    we cannot anchor, or white-only numbered shorthand, is left alone."""
    seen: set[str] = set()
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
        if _move_legal(moves[0], board) and not _line_plays(moves, board):
            yield run.strip(), key


def find_illegal_continuations(text: str, board: chess.Board) -> list[str]:
    """Normalized illegal-line keys (see iter_illegal_continuations)."""
    return [label for _surface, label in iter_illegal_continuations(text, board)]


def projected_boards(text: str, board: chess.Board) -> list[chess.Board]:
    """Boards reachable by the valid SAN moves named in `text`, one 1-ply hop
    each from `board`. Lets a piece-claim about a square a named move reaches
    ("a knight on d3 after ...Nd3") validate against the projected position.
    POV per token: Black on a leading "...", else the side to move.

    By design the claim and the move need not be linked: a claim clears if ANY
    move named anywhere in the prose reaches it. Loose by intent -- the check
    only asks the model to clarify, so we bias toward not flagging."""
    out: list[chess.Board] = []
    seen: set[str] = set()
    for match in _SAN_TOKEN_RE.finditer(text):
        prefix, token = match.group(1), match.group(2)
        if token in seen:
            continue
        seen.add(token)
        pov_board = _board_for_pov(board, _pov_for(prefix, board))
        if pov_board is None:
            continue
        move = _parse_san_real(pov_board, _strip_annotation_glyphs(token))
        if move is None:
            continue
        hop = pov_board.copy(stack=False)
        hop.push(move)
        out.append(hop)
    return out


# Piece-on-square claim recognizer. Two phrasings: "<piece> on <square>"
# ("the knight on f1") and "<square> <piece>" ("the b5 pawn"), each with
# optional leading color and article. Case-insensitive words, exact coords.
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
# Optional movement verb between piece and "to" in a "<piece> to <square>"
# move phrase. Closed set so "tied to"/"according to" don't match;
# (?:re)?(?:direct|deploy)\w* covers direct/redirect/redeployment/etc.
_MOVE_VERB = (
    r"(?:goes|moves|moving|jumps|hops|swings|lifts|drops|retreats|"
    r"advances|heads|comes|returns|relocates|travels|slides|"
    r"(?:re)?(?:direct|deploy)\w*)\s+"
)
_PIECE_TO_SQUARE_RE = re.compile(
    rf"\b{_COLOR_OPT}({_PIECE_ALT})\s+(?:{_MOVE_VERB})?to\s+([a-h][1-8])\b",
    re.IGNORECASE,
)


def _iter_piece_claims(text: str):
    """Yield (surface, color_word, piece_word, square_name) for every claim
    matched by either phrasing. `surface` is the exact prose span (for the UI
    to strike). Positional groups expose (color, piece-or-square,
    square-or-piece) in a known order."""
    for m in _PIECE_ON_SQUARE_RE.finditer(text):
        color, piece, square = m.group(1), m.group(2), m.group(3)
        yield m.group(0), (color or "").lower(), piece.lower(), square.lower()
    for m in _SQUARE_PIECE_RE.finditer(text):
        color, square, piece = m.group(1), m.group(2), m.group(3)
        yield m.group(0), (color or "").lower(), piece.lower(), square.lower()


_TYPE_TO_NAME = {t: n for n, t in _PIECE_WORDS.items()}


def describe_square(square_name: str, board: chess.Board) -> str:
    """Ground truth for a square: 'd5 is empty' or 'd5 has a white knight'.
    Used to anchor the corrective so a weak model can't just restate."""
    square = chess.parse_square(square_name)
    piece = board.piece_at(square)
    if piece is None:
        return f"{square_name} is empty"
    color = "white" if piece.color == chess.WHITE else "black"
    return f"{square_name} has a {color} {_TYPE_TO_NAME[piece.piece_type]}"


def _claim_holds(
    board: chess.Board, square: int, piece_type: int, color_word: str,
) -> bool:
    """True iff `board` has a piece of `piece_type` (and matching color word,
    if given) on `square`."""
    actual = board.piece_at(square)
    if actual is None or actual.piece_type != piece_type:
        return False
    return not color_word or actual.color == _COLOR_WORDS[color_word]


def iter_false_claim_squares(text: str, board: chess.Board):
    """Yield (surface, label, square_name) for each false piece claim.
    `surface` is the exact prose span ("White's knight on b1") for the UI to
    strike; `label` is the normalized form for facts/keys. A claim holds when
    the piece sits there on the current board OR on a board reached by a move
    named earlier in the prose ('a knight on d3 after ...Nd3')."""
    boards = [board, *projected_boards(text, board)]
    seen: set[str] = set()
    for surface, color_word, piece_word, square_name in _iter_piece_claims(text):
        key = f"{color_word}|{piece_word}|{square_name}"
        if key in seen:
            continue
        seen.add(key)
        square = chess.parse_square(square_name)
        piece_type = _PIECE_WORDS[piece_word]
        if any(_claim_holds(b, square, piece_type, color_word) for b in boards):
            continue
        prefix = f"{color_word} " if color_word else ""
        yield surface, f"{prefix}{piece_word} on {square_name}", square_name


def find_false_piece_claims(text: str, board: chess.Board) -> list[str]:
    """'piece on square' claims that hold on neither the current board nor any
    position reached by a move named in the prose. 'the bishop on g6' with no
    move putting a bishop there is flagged; 'a knight on d3 after ...Nd3' is
    cleared by the projected board."""
    return [label for _surface, label, _square in iter_false_claim_squares(text, board)]


def _reaches_for_color(
    board: chess.Board, square: int, piece_type: int, color: chess.Color,
) -> bool:
    """True iff `color` has a legal move landing a piece of `piece_type` on
    `square`. Tests the named side by flipping turn when it isn't theirs;
    flipping into check is illegal, so that case is False. Promotions resolve
    to the promoted type."""
    pov = _board_for_pov(board, color)
    if pov is None:
        return False
    for move in pov.legal_moves:
        if move.to_square != square:
            continue
        mover = pov.piece_at(move.from_square)
        if mover is None:
            continue
        landed = move.promotion if move.promotion else mover.piece_type
        if landed == piece_type:
            return True
    return False


def iter_illegal_piece_moves(text: str, board: chess.Board):
    """Yield (surface, label) for each '<piece> to <square>' prose move whose
    destination no piece of that type can legally reach. `surface` is the exact
    prose span for the UI to strike. Forward-looking, so a reachable plan is
    fine; only an impossible move ('bishop to a1') is flagged. POV: the named
    color when given, else either side."""
    seen: set[str] = set()
    for m in _PIECE_TO_SQUARE_RE.finditer(text):
        color_word = (m.group(1) or "").lower()
        piece_word = m.group(2).lower()
        square_name = m.group(3).lower()
        key = f"{color_word}|{piece_word}|{square_name}"
        if key in seen:
            continue
        seen.add(key)
        piece_type = _PIECE_WORDS[piece_word]
        square = chess.parse_square(square_name)
        colors = (
            [_COLOR_WORDS[color_word]] if color_word else [chess.WHITE, chess.BLACK]
        )
        if any(_reaches_for_color(board, square, piece_type, c) for c in colors):
            continue
        prefix = f"{color_word} " if color_word else ""
        yield m.group(0), f"{prefix}{piece_word} to {square_name}"


def find_illegal_piece_moves(text: str, board: chess.Board) -> list[str]:
    """Normalized '<piece> to <square>' labels (see iter_illegal_piece_moves)."""
    return [label for _surface, label in iter_illegal_piece_moves(text, board)]
