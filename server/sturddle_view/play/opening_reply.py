"""Book-reply probe for the AI analysis agent.

One question, two sources: does known opening theory name a reply for
the position under review? The vendored ECO dataset is consulted first
(the next move with the most named lines through it -- the mainline
proxy), then the configured HVE opening book via ``book_reply``. A hit
lets the agent favor theory without an engine comparison round and
carries the node's other theory moves so the prose can name the equally
valid choices; a miss falls back to normal analysis. Startpos games only
-- callers gate on an unset start FEN.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

import chess

from ..env_utils import env_float, env_int
from ..openings import Opening, OpeningBook
from .opening_lines import BookRef, book_next_moves, book_reply, is_epd_book

log = logging.getLogger(__name__)

REPLY_SOURCE_ECO = "eco"
REPLY_SOURCE_BOOK = "book"

# A move counts as ECO theory at a node only when the named lines through
# it reach this share of the top continuation's: tricks sit below 0.01
# (Bongcloud, Barnes), real systems at 0.04+ (Pirc, Scandinavian).
_THEORY_MIN_SHARE_DEFAULT = 0.04
_THEORY_MIN_SHARE_ENV = "SV_AI_OPENING_THEORY_MIN_SHARE"
# Sibling theory moves carried alongside the reply, for the prose to name.
_ALTERNATIVES_MAX_DEFAULT = 3
_ALTERNATIVES_MAX_ENV = "SV_AI_OPENING_ALTERNATIVES_MAX"

# (san, line_name) -- line_name None for the book file, which has no names.
Alternative = tuple[str, str | None]


@dataclass(slots=True, frozen=True)
class OpeningReply:
    """A theory reply for the position under review. `line_name` is the
    named ECO line ("B96 Sicilian Defense: Najdorf Variation") for the
    dataset source, None for the configured book file. `alternatives`
    are the equally standard sibling moves at this node."""

    san: str
    uci: str
    source: str  # REPLY_SOURCE_ECO | REPLY_SOURCE_BOOK
    line_name: str | None
    alternatives: tuple[Alternative, ...] = ()


def book_ref_from_settings(s) -> BookRef | None:
    """BookRef for the configured PGN opening book, or None when the book
    is disabled, unset, missing, or an EPD position book (no replies)."""
    path = s.engine_default_book_path
    if not s.hve_use_opening_book or not path:
        return None
    if not Path(path).is_file() or is_epd_book(path):
        return None
    return BookRef(
        path=path,
        plies=s.engine_default_book_plies,
        order=s.engine_default_book_order,
        anchor=s.engine_default_book_cursor,
    )


def probe_opening_reply(
    eco_book: OpeningBook | None,
    book: BookRef | None,
    board: chess.Board,
    opening: Opening | None,
    prefer: str | None = None,
) -> OpeningReply | None:
    """Theory reply for the position on `board`, or None when off book.
    `opening` (the line matched so far) gates the ECO leg; the book-file
    leg needs no match. `prefer` (UCI; view mode: the move played here)
    is the reply whenever either source knows it -- theory has several
    answers, and what matters is whether the played one is among them."""
    played = [m.uci() for m in board.move_stack]
    reply = (
        _eco_reply(eco_book, board, played, opening, prefer)
        or _book_file_reply(book, board, played, prefer)
    )
    if reply is None:
        log.info("opening reply: off book at ply %d", len(played))
    return reply


def _eco_reply(
    eco_book: OpeningBook | None,
    board: chess.Board,
    played: list[str],
    opening: Opening | None,
    prefer: str | None,
) -> OpeningReply | None:
    # No matched line, no ECO leg: at ply 0 the unsteered pool is the
    # whole dataset and its first row (an A00 oddity) would ship as theory.
    if eco_book is None or len(eco_book) == 0 or opening is None:
        return None
    n = len(played)
    rows = eco_book.continuations(played)
    # Mainline proxy: the move with the most named lines through it (ECO
    # order alone lands on Barnes/Bongcloud). `prefer` outranks it only
    # with a real following; stable sort keeps fundamental order per move.
    weight = Counter(o.moves[n] for o in rows)
    top = max(weight.values(), default=0)
    bar = top * env_float(_THEORY_MIN_SHARE_ENV, _THEORY_MIN_SHARE_DEFAULT)
    if prefer is not None and weight[prefer] < bar:
        prefer = None
    rows.sort(key=lambda o: (o.moves[n] != prefer, -weight[o.moves[n]]))
    for o in rows:
        uci = o.moves[n]
        san = _san_for(board, uci)
        if san is not None:
            name = _line_name(eco_book, played + [uci], o)
            alts = _alternatives(
                board, _eco_candidates(eco_book, played, rows, weight, bar), uci,
            )
            log.info("opening reply: %s entering %s; also %s", san, name, [a for a, _ in alts])
            return OpeningReply(san, uci, REPLY_SOURCE_ECO, name, alts)
    return None


def _eco_candidates(
    eco_book: OpeningBook,
    played: list[str],
    rows: list[Opening],
    weight: Counter,
    bar: float,
) -> Iterator[tuple[str, str]]:
    """Distinct theory moves at the node in `rows` order, with the line
    each enters; moves below the share bar are not theory."""
    n = len(played)
    seen: set[str] = set()
    for o in rows:
        uci = o.moves[n]
        if uci in seen or weight[uci] < bar:
            continue
        seen.add(uci)
        yield uci, _line_name(eco_book, played + [uci], o)


def _alternatives(
    board: chess.Board, candidates: Iterable[tuple[str, str | None]], chosen: str,
) -> tuple[Alternative, ...]:
    """Up to the cap of `candidates` (uci, line_name) that are legal and
    not `chosen`, as (san, line_name)."""
    cap = env_int(_ALTERNATIVES_MAX_ENV, _ALTERNATIVES_MAX_DEFAULT)
    out: list[Alternative] = []
    for uci, name in candidates:
        if len(out) >= cap:
            break
        if uci == chosen:
            continue
        san = _san_for(board, uci)
        if san is not None:
            out.append((san, name))
    return tuple(out)


def _line_name(eco_book: OpeningBook, line: list[str], through: Opening) -> str:
    """'B50 Sicilian Defense: Modern Variations' -- the name of the
    position `line` reaches when the dataset has one, else the shortest
    named line passing through it (`through`)."""
    hit = eco_book.lookup(line)
    o = hit if hit is not None and hit.ply == len(line) else through
    return f"{o.eco} {o.name}"


def _book_file_reply(
    book: BookRef | None, board: chess.Board, played: list[str], prefer: str | None,
) -> OpeningReply | None:
    if book is None:
        return None
    # Sequential-from-anchor even for random-order books: the same
    # position must always name the same reply, never re-roll per probe.
    uci = book_reply(book.path, played, book.plies, None, book.anchor, prefer)
    if uci is None:
        return None
    san = _san_for(board, uci)
    if san is None:
        return None
    alts = _alternatives(
        board,
        ((u, None) for u in book_next_moves(book.path, played, book.plies)),
        uci,
    )
    log.info("opening reply: %s from book %s; also %s", san, book.path, [a for a, _ in alts])
    return OpeningReply(san, uci, REPLY_SOURCE_BOOK, None, alts)


def _san_for(board: chess.Board, uci: str) -> str | None:
    """SAN for `uci` on `board`; None when unparseable or illegal (a
    malformed dataset row must fall through, not raise)."""
    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        return None
    if move not in board.legal_moves:
        return None
    return board.san(move)
