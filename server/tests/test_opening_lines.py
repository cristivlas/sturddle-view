from __future__ import annotations

import pytest

from sturddle_view.config import BOOK_ORDER_RANDOM, BOOK_ORDER_SEQUENTIAL
from sturddle_view.play import opening_lines
from sturddle_view.play.opening_lines import OpeningSeed, select_seed


@pytest.fixture(autouse=True)
def _clear_index_cache():
    """opening_lines._index_cache is module-global and keyed by file stat.
    Distinct tmp files won't collide, but clear it so a reused tmp path
    (same name, rewritten) can't serve a stale index across tests."""
    opening_lines._index_cache.clear()
    yield
    opening_lines._index_cache.clear()


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


# ----- selection: missing / empty -----

def test_missing_file_returns_none(tmp_path):
    assert select_seed(str(tmp_path / "nope.pgn"), None, None, 0) is None


def test_empty_pgn_returns_none(tmp_path):
    path = _write(tmp_path, "empty.pgn", "\n\n   \n")
    assert select_seed(path, None, None, 0) is None


def test_empty_epd_returns_none(tmp_path):
    path = _write(tmp_path, "empty.epd", "\n\n")
    assert select_seed(path, None, None, 0) is None


# ----- PGN books -----

def test_pgn_yields_uci_line(tmp_path):
    path = _write(tmp_path, "book.pgn", "1. e4 e5 2. Nf3 Nc6 *\n")
    seed = select_seed(path, None, BOOK_ORDER_SEQUENTIAL, 0)
    assert seed == OpeningSeed(moves_uci=("e2e4", "e7e5", "g1f3", "b8c6"))
    assert seed.start_fen is None


def test_pgn_respects_ply_cap(tmp_path):
    path = _write(tmp_path, "book.pgn", "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 *\n")
    seed = select_seed(path, 3, BOOK_ORDER_SEQUENTIAL, 0)
    assert seed.moves_uci == ("e2e4", "e7e5", "g1f3")


def test_pgn_strips_comments_nags_variations(tmp_path):
    text = "1. e4 {best by test} e5 $1 (1... c5 2. Nf3) 2. Nf3 Nc6 *\n"
    path = _write(tmp_path, "book.pgn", text)
    seed = select_seed(path, None, BOOK_ORDER_SEQUENTIAL, 0)
    # The (1... c5 ...) sideline is dropped; mainline survives intact.
    assert seed.moves_uci == ("e2e4", "e7e5", "g1f3", "b8c6")


def test_pgn_with_headers(tmp_path):
    text = '[Event "X"]\n[White "A"]\n\n1. d4 d5 2. c4 *\n'
    path = _write(tmp_path, "book.pgn", text)
    seed = select_seed(path, None, BOOK_ORDER_SEQUENTIAL, 0)
    assert seed.moves_uci == ("d2d4", "d7d5", "c2c4")


def test_pgn_truncates_at_first_illegal_token(tmp_path):
    # "Qz9" is unparseable SAN: the line truncates there, not dropped.
    path = _write(tmp_path, "book.pgn", "1. e4 e5 2. Qz9 Nc6 *\n")
    seed = select_seed(path, None, BOOK_ORDER_SEQUENTIAL, 0)
    assert seed.moves_uci == ("e2e4", "e7e5")


# ----- EPD books -----

def test_epd_yields_start_fen(tmp_path):
    path = _write(tmp_path, "book.epd", "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -\n")
    seed = select_seed(path, None, BOOK_ORDER_SEQUENTIAL, 0)
    assert seed.start_fen is not None
    assert seed.start_fen.startswith("rnbqkbnr/pp1ppppp/8/2p5/4P3")
    assert seed.moves_uci == ()


def test_epd_extension_case_insensitive(tmp_path):
    path = _write(tmp_path, "book.EPD", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -\n")
    seed = select_seed(path, None, BOOK_ORDER_SEQUENTIAL, 0)
    assert seed.start_fen is not None


# ----- malformed-line skip -----

def test_skips_bad_epd_line_for_next_usable(tmp_path):
    text = "garbage not a fen\nrnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -\n"
    path = _write(tmp_path, "book.epd", text)
    # cursor 0 lands on the bad line; selection probes forward to line 1.
    seed = select_seed(path, None, BOOK_ORDER_SEQUENTIAL, 0)
    assert seed is not None
    assert seed.start_fen is not None


def test_skips_unparseable_pgn_line(tmp_path):
    # First game has no legal moves (bare result); second is real. Games are
    # header-delimited (PGN multi-game format the splitter keys on).
    text = '[Event "A"]\n\n*\n\n[Event "B"]\n\n1. e4 e5 *\n'
    path = _write(tmp_path, "book.pgn", text)
    seed = select_seed(path, None, BOOK_ORDER_SEQUENTIAL, 0)
    assert seed is not None
    assert seed.moves_uci == ("e2e4", "e7e5")


# ----- sequential cursor / random selection -----

# Multi-game PGN: games are split on the blank line before a header block,
# so each fixture game carries its own [Event] tag.
def _multigame(*movetexts):
    return "\n\n".join(f'[Event "g{i}"]\n\n{m}' for i, m in enumerate(movetexts))


def test_sequential_cursor_walks_lines(tmp_path):
    path = _write(tmp_path, "book.pgn", _multigame("1. e4 *", "1. d4 *", "1. c4 *"))
    first = [select_seed(path, None, BOOK_ORDER_SEQUENTIAL, c).moves_uci[0] for c in range(3)]
    assert first == ["e2e4", "d2d4", "c2c4"]


def test_sequential_cursor_wraps_modulo_count(tmp_path):
    path = _write(tmp_path, "book.pgn", _multigame("1. e4 *", "1. d4 *"))
    # cursor 2 wraps to line 0, cursor 3 to line 1.
    assert select_seed(path, None, BOOK_ORDER_SEQUENTIAL, 2).moves_uci[0] == "e2e4"
    assert select_seed(path, None, BOOK_ORDER_SEQUENTIAL, 3).moves_uci[0] == "d2d4"


def test_random_order_ignores_cursor_and_stays_in_range(tmp_path):
    path = _write(tmp_path, "book.pgn", _multigame("1. e4 *", "1. d4 *", "1. c4 *"))
    firsts = {
        select_seed(path, None, BOOK_ORDER_RANDOM, 99).moves_uci[0]
        for _ in range(40)
    }
    assert firsts <= {"e2e4", "d2d4", "c2c4"}


def test_none_order_is_sequential(tmp_path):
    path = _write(tmp_path, "book.pgn", _multigame("1. e4 *", "1. d4 *"))
    assert select_seed(path, None, None, 1).moves_uci[0] == "d2d4"


# ----- index cache keyed by file stat -----

def test_index_cache_reused_for_same_file(tmp_path):
    path = _write(tmp_path, "book.pgn", "1. e4 e5 *\n")
    select_seed(path, None, BOOK_ORDER_SEQUENTIAL, 0)
    keys = list(opening_lines._index_cache.keys())
    assert len(keys) == 1
    select_seed(path, None, BOOK_ORDER_SEQUENTIAL, 0)
    assert list(opening_lines._index_cache.keys()) == keys
