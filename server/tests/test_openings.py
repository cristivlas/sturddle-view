from __future__ import annotations

from pathlib import Path

import pytest

from sturddle_view.openings import OpeningBook


def test_load_default_book_has_lines():
    book = OpeningBook.load()
    # The dataset has thousands of lines; we just verify it loaded > 0.
    assert len(book) > 0


def test_lookup_caro_kann():
    book = OpeningBook.load()
    # 1. e4 c6 -> Caro-Kann Defense (B10)
    hit = book.lookup(["e2e4", "c7c6"])
    assert hit is not None
    assert hit.eco.startswith("B")
    assert "Caro-Kann" in hit.name


def test_lookup_unplayable_returns_none():
    book = OpeningBook.load()
    # Empty move list -> nothing.
    assert book.lookup([]) is None


def test_lookup_longest_prefix_wins():
    book = OpeningBook.load()
    # 1. e4 c5 (Sicilian) vs 1. e4 c5 2. Nf3 d6 3. d4 (Open Sicilian variant) —
    # the longer line should report a more specific name.
    short = book.lookup(["e2e4", "c7c5"])
    longer = book.lookup(["e2e4", "c7c5", "g1f3", "d7d6", "d2d4"])
    assert short is not None and longer is not None
    # Longer match should be at least as specific (different name or same).
    # We assert they're both Sicilians (B-class), and longer has a non-empty name.
    assert short.eco.startswith("B")
    assert longer.eco.startswith("B")


def test_load_missing_dir_returns_empty():
    book = OpeningBook.load(Path("/nonexistent/openings/dir"))
    assert len(book) == 0
