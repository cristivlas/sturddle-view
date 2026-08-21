"""Book-reply probe for the AI analysis agent.

One question, two sources: does known opening theory name a reply for
the position under review? The vendored ECO dataset is consulted first
(a named line extending the played moves supplies its next move,
same-family continuations preferred), then the configured HVE opening
book via ``book_reply``. A hit lets the agent favor theory without an
engine comparison round; a miss falls back to normal analysis.
Startpos games only -- callers gate on an unset start FEN.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import chess

from ..openings import Opening, OpeningBook
from .opening_lines import BookRef, book_reply, is_epd_book

log = logging.getLogger(__name__)

REPLY_SOURCE_ECO = "eco"
REPLY_SOURCE_BOOK = "book"


@dataclass(slots=True, frozen=True)
class OpeningReply:
    """A theory reply for the position under review. `line_name` is the
    named ECO line ("B96 Sicilian Defense: Najdorf Variation") for the
    dataset source, None for the configured book file."""

    san: str
    source: str  # REPLY_SOURCE_ECO | REPLY_SOURCE_BOOK
    line_name: str | None


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
) -> OpeningReply | None:
    """Theory reply for the position on `board`, or None when off book.
    `opening` (the line matched so far) steers the ECO leg toward
    same-family continuations."""
    played = [m.uci() for m in board.move_stack]
    reply = _eco_reply(eco_book, board, played, opening)
    if reply is not None:
        return reply
    return _book_file_reply(book, board, played)


def _eco_reply(
    eco_book: OpeningBook | None,
    board: chess.Board,
    played: list[str],
    opening: Opening | None,
) -> OpeningReply | None:
    if eco_book is None or len(eco_book) == 0:
        return None
    rows = eco_book.continuations(played)
    if opening is not None:
        family = OpeningBook.family_of(opening.name)
        # Stable sort: same-family lines first, fundamental order kept.
        rows.sort(key=lambda o: OpeningBook.family_of(o.name) != family)
    for o in rows:
        san = _san_for(board, o.moves[len(played)])
        if san is not None:
            log.debug("opening reply: %s continuing %s %s", san, o.eco, o.name)
            return OpeningReply(san, REPLY_SOURCE_ECO, f"{o.eco} {o.name}")
    return None


def _book_file_reply(
    book: BookRef | None, board: chess.Board, played: list[str],
) -> OpeningReply | None:
    if book is None:
        return None
    # Sequential-from-anchor even for random-order books: the same
    # position must always name the same reply, never re-roll per probe.
    uci = book_reply(book.path, played, book.plies, None, book.anchor)
    if uci is None:
        return None
    san = _san_for(board, uci)
    if san is None:
        return None
    log.debug("opening reply: %s from book %s", san, book.path)
    return OpeningReply(san, REPLY_SOURCE_BOOK, None)


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
