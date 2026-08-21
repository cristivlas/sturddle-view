"""Opening-reply probe: ECO-first cascade, family preference, book-file
fallback, deterministic line choice, settings-to-BookRef guards."""
from __future__ import annotations

import chess

from sturddle_view.openings import Opening, OpeningBook
from sturddle_view.play.opening_lines import BookRef
from sturddle_view.play.opening_reply import (
    REPLY_SOURCE_BOOK,
    REPLY_SOURCE_ECO,
    book_ref_from_settings,
    probe_opening_reply,
)

from .ai_kick_helpers import FakeSettings


def _eco_book() -> OpeningBook:
    book = OpeningBook()
    book.add("B20", "Sicilian Defense", "1. e4 c5")
    book.add("C20", "King's Pawn Game", "1. e4 e5")
    book.add("C40", "King's Knight Opening", "1. e4 e5 2. Nf3")
    return book


def _board_after(*sans: str) -> chess.Board:
    board = chess.Board()
    for san in sans:
        board.push_san(san)
    return board


def _pgn_book(tmp_path, movetext: str):
    path = tmp_path / "book.pgn"
    path.write_text(f'[Event "?"]\n\n{movetext}\n', encoding="utf-8")
    return BookRef(path=str(path), plies=None, order=None, anchor=0)


def test_eco_continuation_supplies_next_move():
    reply = probe_opening_reply(_eco_book(), None, _board_after("e4", "e5"), None)
    assert reply is not None
    assert reply.san == "Nf3"
    assert reply.source == REPLY_SOURCE_ECO
    assert reply.line_name == "C40 King's Knight Opening"


def test_eco_prefers_current_opening_family():
    # After 1.e4 both c5 (B20) and e5 (C20) continue; without a matched
    # opening the fundamental (eco, name) order picks B20. A matched
    # King's Pawn line must flip the preference to its own family.
    board = _board_after("e4")
    free = probe_opening_reply(_eco_book(), None, board, None)
    assert free is not None and free.san == "c5"
    matched = Opening(eco="C20", name="King's Pawn Game: Some Variation")
    steered = probe_opening_reply(_eco_book(), None, board, matched)
    assert steered is not None and steered.san == "e5"


def test_book_file_fallback_when_eco_misses(tmp_path):
    book = _pgn_book(tmp_path, "1. d4 d5 2. c4 *")
    reply = probe_opening_reply(_eco_book(), book, _board_after("d4", "d5"), None)
    assert reply is not None
    assert reply.san == "c4"
    assert reply.source == REPLY_SOURCE_BOOK
    assert reply.line_name is None


def test_eco_takes_precedence_over_book_file(tmp_path):
    book = _pgn_book(tmp_path, "1. e4 e5 2. Bc4 *")
    reply = probe_opening_reply(_eco_book(), book, _board_after("e4", "e5"), None)
    assert reply is not None
    assert reply.san == "Nf3"
    assert reply.source == REPLY_SOURCE_ECO


def test_off_book_position_returns_none(tmp_path):
    book = _pgn_book(tmp_path, "1. e4 e5 *")
    assert probe_opening_reply(_eco_book(), book, _board_after("a4", "h5"), None) is None


def test_probe_ignores_random_order_for_determinism(tmp_path):
    # Two lines diverge after 1.e4; a random-order book must not re-roll
    # per probe: every call names the anchor-sequential line.
    path = tmp_path / "book.pgn"
    path.write_text(
        '[Event "?"]\n\n1. e4 e5 *\n\n[Event "?"]\n\n1. e4 c5 *\n',
        encoding="utf-8",
    )
    book = BookRef(path=str(path), plies=None, order="random", anchor=0)
    board = _board_after("e4")
    replies = {
        probe_opening_reply(None, book, board, None).san for _ in range(10)
    }
    assert replies == {"e5"}


def _settings(**kw) -> FakeSettings:
    base = dict(
        hve_use_opening_book=True,
        engine_default_book_plies=12,
        engine_default_book_order="random",
        engine_default_book_cursor=3,
    )
    base.update(kw)
    return FakeSettings(**base)


def test_book_ref_from_settings_mirrors_fields(tmp_path):
    path = tmp_path / "book.pgn"
    path.write_text('[Event "?"]\n\n1. e4 *\n', encoding="utf-8")
    ref = book_ref_from_settings(_settings(engine_default_book_path=str(path)))
    assert ref == BookRef(path=str(path), plies=12, order="random", anchor=3)


def test_book_ref_from_settings_guards(tmp_path):
    pgn = tmp_path / "book.pgn"
    pgn.write_text('[Event "?"]\n\n1. e4 *\n', encoding="utf-8")
    epd = tmp_path / "book.epd"
    epd.write_text(chess.Board().epd() + "\n", encoding="utf-8")
    assert book_ref_from_settings(_settings()) is None  # no path
    assert (
        book_ref_from_settings(
            _settings(engine_default_book_path=str(pgn), hve_use_opening_book=False)
        )
        is None
    )
    assert (
        book_ref_from_settings(
            _settings(engine_default_book_path=str(tmp_path / "missing.pgn"))
        )
        is None
    )
    assert book_ref_from_settings(_settings(engine_default_book_path=str(epd))) is None
