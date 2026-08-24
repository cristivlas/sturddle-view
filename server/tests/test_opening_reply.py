"""Opening-reply probe: ECO-first cascade, mainline ranking, line naming,
book-file fallback, deterministic line choice, settings-to-BookRef guards."""
from __future__ import annotations

import chess

from sturddle_view.openings import Opening, OpeningBook
from sturddle_view.play.opening_lines import BookRef
from sturddle_view.play.opening_reply import (
    _ALTERNATIVES_MAX_ENV,
    _THEORY_MIN_SHARE_ENV,
    REPLY_SOURCE_BOOK,
    REPLY_SOURCE_ECO,
    Alternative,
    book_ref_from_settings,
    probe_opening_reply,
)

from .ai_kick_helpers import FakeSettings


def _eco_book() -> OpeningBook:
    book = OpeningBook()
    book.add("B20", "Sicilian Defense", "1. e4 c5")
    book.add("C20", "King's Pawn Game", "1. e4 e5")
    book.add("C40", "King's Knight Opening", "1. e4 e5 2. Nf3")
    # 1.e4 e5 2.Nf3 Nc6 itself is unnamed here: exercises the name fallback.
    book.add("C50", "Italian Game", "1. e4 e5 2. Nf3 Nc6 3. Bc4")
    return book


# What the kick's lookup names for the positions under test.
_KINGS_PAWN = Opening(eco="B00", name="King's Pawn")
_KINGS_PAWN_GAME = Opening(eco="C20", name="King's Pawn Game")
_KINGS_KNIGHT = Opening(eco="C40", name="King's Knight Opening")


def _board_after(*sans: str) -> chess.Board:
    board = chess.Board()
    for san in sans:
        board.push_san(san)
    return board


def _pgn_book(tmp_path, *movetexts: str):
    path = tmp_path / "book.pgn"
    games = "\n\n".join(f'[Event "?"]\n\n{m}' for m in movetexts)
    path.write_text(games + "\n", encoding="utf-8")
    return BookRef(path=str(path), plies=None, order=None, anchor=0)


def test_eco_continuation_supplies_next_move():
    reply = probe_opening_reply(
        _eco_book(), None, _board_after("e4", "e5"), _KINGS_PAWN_GAME,
    )
    assert reply is not None
    assert reply.san == "Nf3"
    assert reply.uci == "g1f3"
    assert reply.source == REPLY_SOURCE_ECO
    assert reply.line_name == "C40 King's Knight Opening"


def test_eco_prefers_move_with_most_named_lines():
    # After 1.e4, c5 sorts first by ECO (B20) but only one line runs
    # through it; three run through e5. The mainline proxy picks e5, and
    # the reply is named for the position it reaches, not the first line.
    reply = probe_opening_reply(_eco_book(), None, _board_after("e4"), _KINGS_PAWN)
    assert reply is not None
    assert reply.san == "e5"
    assert reply.line_name == "C20 King's Pawn Game"


def test_eco_prefers_played_move_when_theory_has_it():
    # View mode: c5 is theory too, so the played move is the reply --
    # not the most-travelled one.
    reply = probe_opening_reply(
        _eco_book(), None, _board_after("e4"), _KINGS_PAWN, "c7c5",
    )
    assert reply is not None
    assert reply.san == "c5"
    assert reply.line_name == "B20 Sicilian Defense"


def test_eco_prefer_needs_a_share_of_the_top_move(monkeypatch):
    # ECO names every trick: a played move whose following is too thin
    # next to the top continuation (c5: 1 line vs e5: 3) is not vetted
    # theory, and the file-leg rule does not apply here.
    monkeypatch.setenv(_THEORY_MIN_SHARE_ENV, "0.5")
    reply = probe_opening_reply(
        _eco_book(), None, _board_after("e4"), _KINGS_PAWN, "c7c5",
    )
    assert reply is not None
    assert reply.san == "e5"


def test_eco_ignores_played_move_outside_theory():
    reply = probe_opening_reply(
        _eco_book(), None, _board_after("e4"), _KINGS_PAWN, "h7h5",
    )
    assert reply is not None
    assert reply.san == "e5"


def test_book_file_prefers_played_move_when_in_book(tmp_path):
    book = _pgn_book(tmp_path, "1. e4 e5 *", "1. e4 c5 *")
    board = _board_after("e4")
    assert probe_opening_reply(None, book, board, None).san == "e5"
    assert probe_opening_reply(None, book, board, None, "c7c5").san == "c5"
    assert probe_opening_reply(None, book, board, None, "h7h5").san == "e5"


def test_eco_reply_carries_sibling_theory_moves():
    # After 1.e4 the other theory move (c5, 1 of 4 lines) rides along with
    # the line it enters, so the prose can name the equally valid choice.
    reply = probe_opening_reply(_eco_book(), None, _board_after("e4"), _KINGS_PAWN)
    assert reply is not None
    assert reply.san == "e5"
    assert reply.alternatives == (Alternative("c5", "c7c5", "B20 Sicilian Defense"),)


def test_eco_prefer_siblings_become_the_alternatives():
    reply = probe_opening_reply(
        _eco_book(), None, _board_after("e4"), _KINGS_PAWN, "c7c5",
    )
    assert reply is not None
    assert reply.san == "c5"
    assert reply.alternatives == (Alternative("e5", "e7e5", "C20 King's Pawn Game"),)


def test_eco_alternatives_respect_share_bar_and_cap(monkeypatch):
    board = _board_after("e4")
    monkeypatch.setenv(_THEORY_MIN_SHARE_ENV, "0.5")
    thin = probe_opening_reply(_eco_book(), None, board, _KINGS_PAWN)
    assert thin is not None and thin.alternatives == ()
    monkeypatch.delenv(_THEORY_MIN_SHARE_ENV)
    monkeypatch.setenv(_ALTERNATIVES_MAX_ENV, "0")
    capped = probe_opening_reply(_eco_book(), None, board, _KINGS_PAWN)
    assert capped is not None and capped.alternatives == ()


def test_book_file_reply_carries_other_continuations(tmp_path):
    book = _pgn_book(tmp_path, "1. e4 e5 *", "1. e4 c5 *")
    board = _board_after("e4")
    first = probe_opening_reply(None, book, board, None)
    assert first is not None
    assert (first.san, first.alternatives) == ("e5", (Alternative("c5", "c7c5", None),))
    played = probe_opening_reply(None, book, board, None, "c7c5")
    assert played is not None
    assert (played.san, played.alternatives) == ("c5", (Alternative("e5", "e7e5", None),))


def test_eco_names_unregistered_position_by_line_through_it():
    reply = probe_opening_reply(
        _eco_book(), None, _board_after("e4", "e5", "Nf3"), _KINGS_KNIGHT,
    )
    assert reply is not None
    assert reply.san == "Nc6"
    assert reply.line_name == "C50 Italian Game"


def test_eco_leg_off_without_matched_line():
    # Ply 0: no line matched yet, and continuations([]) is the whole
    # dataset -- its first row must not ship as theory.
    assert probe_opening_reply(_eco_book(), None, chess.Board(), None) is None


def test_book_file_fallback_when_eco_misses(tmp_path):
    book = _pgn_book(tmp_path, "1. d4 d5 2. c4 *")
    reply = probe_opening_reply(_eco_book(), book, _board_after("d4", "d5"), None)
    assert reply is not None
    assert reply.san == "c4"
    assert reply.source == REPLY_SOURCE_BOOK
    assert reply.line_name is None


def test_eco_takes_precedence_over_book_file(tmp_path):
    book = _pgn_book(tmp_path, "1. e4 e5 2. Bc4 *")
    reply = probe_opening_reply(
        _eco_book(), book, _board_after("e4", "e5"), _KINGS_PAWN_GAME,
    )
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
