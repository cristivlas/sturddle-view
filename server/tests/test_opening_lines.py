from __future__ import annotations

import pytest

from sturddle_view.config import BOOK_ORDER_RANDOM, BOOK_ORDER_SEQUENTIAL
from sturddle_view.play import opening_lines
from sturddle_view.play.opening_lines import (
    book_next_moves,
    book_reply,
    is_epd_book,
    select_epd_seed,
)


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


# Multi-game PGN: games are split on the blank line before a header block,
# so each fixture game carries its own [Event] tag.
def _multigame(*movetexts):
    return "\n\n".join(f'[Event "g{i}"]\n\n{m}' for i, m in enumerate(movetexts))


# ----- format inference -----

def test_is_epd_book_by_extension():
    assert is_epd_book("/x/book.epd")
    assert is_epd_book("/x/book.EPD")
    assert not is_epd_book("/x/book.pgn")


# ----- EPD seeds -----

def test_epd_missing_file_returns_none(tmp_path):
    assert select_epd_seed(str(tmp_path / "nope.epd"), None, 0) is None


def test_epd_empty_returns_none(tmp_path):
    path = _write(tmp_path, "empty.epd", "\n\n")
    assert select_epd_seed(path, None, 0) is None


def test_epd_yields_start_fen(tmp_path):
    path = _write(tmp_path, "book.epd", "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -\n")
    fen = select_epd_seed(path, None, 0)
    assert fen is not None
    assert fen.startswith("rnbqkbnr/pp1ppppp/8/2p5/4P3")


def test_epd_seed_rejects_pgn_file(tmp_path):
    path = _write(tmp_path, "book.pgn", "1. e4 e5 *\n")
    assert select_epd_seed(path, None, 0) is None


def test_epd_sequential_cursor_walks_and_wraps(tmp_path):
    text = (
        "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -\n"
        "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq -\n"
    )
    path = _write(tmp_path, "book.epd", text)
    boards = [select_epd_seed(path, BOOK_ORDER_SEQUENTIAL, c).split()[0] for c in range(3)]
    assert boards[0].count("4P3") == 1
    assert boards[1].count("3P4") == 1
    assert boards[2] == boards[0]  # cursor 2 wraps to line 0


def test_epd_random_stays_in_range(tmp_path):
    text = (
        "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -\n"
        "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq -\n"
    )
    path = _write(tmp_path, "book.epd", text)
    expected = {select_epd_seed(path, BOOK_ORDER_SEQUENTIAL, c) for c in range(2)}
    fens = {select_epd_seed(path, BOOK_ORDER_RANDOM, 99) for _ in range(40)}
    assert fens == expected


def test_epd_skips_bad_line_for_next_usable(tmp_path):
    text = "garbage not a fen\nrnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -\n"
    path = _write(tmp_path, "book.epd", text)
    # cursor 0 lands on the bad line; selection probes forward to line 1.
    assert select_epd_seed(path, BOOK_ORDER_SEQUENTIAL, 0) is not None


# ----- book_reply: basics -----

def test_reply_missing_file_returns_none(tmp_path):
    assert book_reply(str(tmp_path / "nope.pgn"), [], None, None, 0) is None


def test_reply_empty_pgn_returns_none(tmp_path):
    path = _write(tmp_path, "empty.pgn", "\n\n   \n")
    assert book_reply(path, [], None, None, 0) is None


def test_reply_rejects_epd_file(tmp_path):
    path = _write(tmp_path, "book.epd", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -\n")
    assert book_reply(path, [], None, None, 0) is None


def test_reply_follows_line_each_ply(tmp_path):
    path = _write(tmp_path, "book.pgn", '[Event "X"]\n\n1. e4 e5 2. Nf3 Nc6 *\n')
    assert book_reply(path, [], None, None, 0) == "e2e4"
    assert book_reply(path, ["e2e4"], None, None, 0) == "e7e5"
    assert book_reply(path, ["e2e4", "e7e5"], None, None, 0) == "g1f3"


def test_reply_strips_comments_nags_variations(tmp_path):
    text = "1. e4 {best by test} e5 $1 (1... c5 2. Nf3) 2. Nf3 Nc6 *\n"
    path = _write(tmp_path, "book.pgn", text)
    # The (1... c5 ...) sideline is dropped; mainline survives intact.
    assert book_reply(path, ["e2e4"], None, None, 0) == "e7e5"
    assert book_reply(path, ["e2e4", "e7e5"], None, None, 0) == "g1f3"


def test_reply_prefer_wins_when_a_line_continues_with_it(tmp_path):
    path = _write(tmp_path, "book.pgn", _multigame("1. e4 e5 *", "1. e4 c5 *"))
    assert book_reply(path, ["e2e4"], None, None, 0) == "e7e5"
    assert book_reply(path, ["e2e4"], None, None, 0, prefer="c7c5") == "c7c5"


def test_reply_prefer_matches_over_disambiguated_spelling(tmp_path):
    games = _multigame("1. e4 e5 2. Nc3 *", "1. e4 e5 2. Ngf3 *")
    path = _write(tmp_path, "book.pgn", games)
    assert book_reply(path, ["e2e4", "e7e5"], None, None, 0, prefer="g1f3") == "g1f3"


def test_reply_prefer_ignored_when_absent_or_illegal(tmp_path):
    path = _write(tmp_path, "book.pgn", _multigame("1. e4 e5 *", "1. e4 c5 *"))
    for prefer in ("h7h5", "e1e8", "zz"):
        assert book_reply(path, ["e2e4"], None, None, 0, prefer=prefer) == "e7e5"


# ----- book_next_moves -----

def test_next_moves_lists_distinct_continuations_in_line_order(tmp_path):
    games = _multigame("1. e4 e5 *", "1. e4 c5 *", "1. e4 e5 2. Nf3 *", "1. d4 d5 *")
    path = _write(tmp_path, "book.pgn", games)
    assert book_next_moves(path, [], None) == ["e2e4", "d2d4"]
    assert book_next_moves(path, ["e2e4"], None) == ["e7e5", "c7c5"]
    assert book_next_moves(path, ["e2e4", "c7c5"], None) == []


def test_next_moves_collapses_spellings_and_skips_junk(tmp_path):
    games = _multigame("1. e4 e5 2. Ngf3 *", "1. e4 e5 2. Nf3 *", "1. e4 e5 2. Zz9 *")
    path = _write(tmp_path, "book.pgn", games)
    assert book_next_moves(path, ["e2e4", "e7e5"], None) == ["g1f3"]


def test_next_moves_empty_for_missing_or_epd(tmp_path):
    assert book_next_moves(str(tmp_path / "nope.pgn"), [], None) == []
    path = _write(tmp_path, "book.epd", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -\n")
    assert book_next_moves(path, [], None) == []


def test_reply_none_when_no_first_move_match(tmp_path):
    path = _write(tmp_path, "book.pgn", _multigame("1. e4 e5 *", "1. d4 d5 *"))
    assert book_reply(path, ["g1f3"], None, None, 0) is None


def test_reply_none_on_mid_line_deviation(tmp_path):
    path = _write(tmp_path, "book.pgn", "1. e4 e5 2. Nf3 *\n")
    assert book_reply(path, ["e2e4", "c7c5"], None, None, 0) is None


def test_reply_falls_back_to_other_matching_line(tmp_path):
    # First line wants 1...e5; the human played the Sicilian, which still
    # matches the second line -- the engine must stay in book via the pool.
    path = _write(tmp_path, "book.pgn", _multigame("1. e4 e5 2. Nf3 *", "1. e4 c5 2. Nf3 *"))
    assert book_reply(path, ["e2e4", "c7c5"], None, None, 0) == "g1f3"


def test_reply_none_when_matching_line_has_no_next_move(tmp_path):
    path = _write(tmp_path, "book.pgn", "1. e4 *\n")
    assert book_reply(path, ["e2e4"], None, None, 0) is None


def test_reply_none_for_bad_played_uci(tmp_path):
    path = _write(tmp_path, "book.pgn", "1. e4 e5 *\n")
    assert book_reply(path, ["zz"], None, None, 0) is None


def test_reply_skips_unparseable_candidate_token(tmp_path):
    # Line 0's reply token is junk SAN; the pool falls through to line 1.
    path = _write(tmp_path, "book.pgn", _multigame("1. e4 Qz9 *", "1. e4 e5 *"))
    assert book_reply(path, ["e2e4"], None, None, 0) == "e7e5"


# ----- book_reply: SAN normalization -----

def test_reply_normalizes_decorations_and_mate(tmp_path):
    path = _write(tmp_path, "book.pgn", "1. f4! e6?? 2. g4 Qh4# *\n")
    assert book_reply(path, ["f2f4"], None, None, 0) == "e7e6"
    assert book_reply(path, ["f2f4", "e7e6", "g2g4"], None, None, 0) == "d8h4"


def test_reply_normalizes_zero_castling(tmp_path):
    path = _write(tmp_path, "book.pgn", "1. e4 e5 2. Nf3 Nc6 3. Bc4 Bc5 4. 0-0 *\n")
    played = ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "f8c5"]
    assert book_reply(path, played, None, None, 0) == "e1g1"


def test_reply_parses_over_disambiguated_candidate(tmp_path):
    path = _write(tmp_path, "book.pgn", "1. e4 e5 2. Ngf3 Nc6 *\n")
    assert book_reply(path, ["e2e4", "e7e5"], None, None, 0) == "g1f3"


def test_match_tolerates_over_disambiguated_book_san(tmp_path):
    # board.san() emits "Nf3" but the book spells it "Ngf3": the prefix
    # match must parse-fallback instead of false-missing the line.
    path = _write(tmp_path, "book.pgn", "1. e4 e5 2. Ngf3 Nc6 *\n")
    assert book_reply(path, ["e2e4", "e7e5", "g1f3"], None, None, 0) == "b8c6"


def test_bucket_keys_canonicalize_first_move(tmp_path):
    path = _write(tmp_path, "book.pgn", "1. Ngf3 d5 *\n")
    assert book_reply(path, ["g1f3"], None, None, 0) == "d7d5"


def test_reply_normalizes_promotion_equals(tmp_path):
    text = "1. a4 b5 2. axb5 a6 3. bxa6 Bb7 4. axb7 Nc6 5. bxa8=Q *\n"
    path = _write(tmp_path, "book.pgn", text)
    played = ["a2a4", "b7b5", "a4b5", "a7a6", "b5a6", "c8b7", "a6b7", "b8c6"]
    assert book_reply(path, played, None, None, 0) == "b7a8q"


# ----- book_reply: order and anchor -----

def test_sequential_anchor_rotates_first_move(tmp_path):
    path = _write(tmp_path, "book.pgn", _multigame("1. e4 *", "1. d4 *", "1. c4 *"))
    firsts = [book_reply(path, [], None, BOOK_ORDER_SEQUENTIAL, a) for a in range(4)]
    assert firsts == ["e2e4", "d2d4", "c2c4", "e2e4"]  # anchor 3 wraps


def test_sequential_anchor_picks_nearest_pool_line(tmp_path):
    path = _write(tmp_path, "book.pgn", _multigame("1. e4 e5 *", "1. e4 c5 *", "1. d4 d5 *"))
    # Pool after 1.e4 is lines {0, 1}; nearest at/after the anchor wins.
    assert book_reply(path, ["e2e4"], None, BOOK_ORDER_SEQUENTIAL, 0) == "e7e5"
    assert book_reply(path, ["e2e4"], None, BOOK_ORDER_SEQUENTIAL, 1) == "c7c5"
    assert book_reply(path, ["e2e4"], None, BOOK_ORDER_SEQUENTIAL, 2) == "e7e5"  # wraps past line 2
    assert book_reply(path, ["e2e4"], None, BOOK_ORDER_SEQUENTIAL, 5) == "e7e5"  # modulo line count


def test_none_order_is_sequential(tmp_path):
    path = _write(tmp_path, "book.pgn", _multigame("1. e4 *", "1. d4 *"))
    assert book_reply(path, [], None, None, 1) == "d2d4"


def test_random_order_covers_pool(tmp_path):
    path = _write(tmp_path, "book.pgn", _multigame("1. e4 e5 *", "1. e4 c5 *", "1. d4 d5 *"))
    replies = {book_reply(path, ["e2e4"], None, BOOK_ORDER_RANDOM, 0) for _ in range(60)}
    assert replies == {"e7e5", "c7c5"}


# ----- book_reply: depth caps -----

def test_reply_respects_plies_setting(tmp_path):
    path = _write(tmp_path, "book.pgn", "1. e4 e5 2. Nf3 *\n")
    assert book_reply(path, ["e2e4"], 1, None, 0) is None
    assert book_reply(path, ["e2e4"], 2, None, 0) == "e7e5"


def test_reply_clamped_to_index_max_plies(tmp_path, monkeypatch):
    monkeypatch.setattr(opening_lines, "BOOK_INDEX_MAX_PLIES", 2)
    path = _write(tmp_path, "book.pgn", "1. e4 e5 2. Nf3 Nc6 *\n")
    assert book_reply(path, ["e2e4"], None, None, 0) == "e7e5"
    # plies=10 exceeds the index cap; depth is clamped to 2 tokens.
    assert book_reply(path, ["e2e4", "e7e5"], 10, None, 0) is None


# ----- index cache keyed by file stat -----

def test_index_cache_reused_for_same_file(tmp_path):
    path = _write(tmp_path, "book.pgn", "1. e4 e5 *\n")
    book_reply(path, [], None, None, 0)
    keys = list(opening_lines._index_cache.keys())
    assert len(keys) == 1
    book_reply(path, ["e2e4"], None, None, 0)
    assert list(opening_lines._index_cache.keys()) == keys


def test_index_cache_evicts_superseded_snapshot(tmp_path):
    path = _write(tmp_path, "book.pgn", "1. e4 e5 *\n")
    assert book_reply(path, [], None, None, 0) == "e2e4"
    # Rewrite with different content (size change guarantees a new stat
    # key even on coarse mtime filesystems); old snapshot must be evicted.
    _write(tmp_path, "book.pgn", "1. d4 d5 2. c4 e6 *\n")
    assert book_reply(path, [], None, None, 0) == "d2d4"
    assert len(opening_lines._index_cache) == 1
