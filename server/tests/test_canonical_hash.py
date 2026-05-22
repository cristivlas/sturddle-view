"""Canonical hash: two semantically identical PGN/FEN imports hash equal."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from sturddle_view.play.canonical_hash import canonical_hash


FIXTURES = Path(__file__).parent / "fixtures"
REAL_PGN_FIXTURES = [
    "Fischer vs Bolbochan, Stockholm 1962.pgn",
    "Fischer vs Petrosian, Herceg Novi 1970.pgn",
    "Mamedyarov vs Nakamura, Zug 2013.pgn",
    "Quirky vs Sturddle, blunder.pgn",
    "IsaBB vs Sturddle, 2025.pgn",
    "Byrne vs Fischer, Game of the Century 1956.pgn",
    "Sturddle vs Arcanum, CCRL 2026.pgn",
]


# --- PGN: header order ---------------------------------------------------

def test_pgn_header_order_does_not_matter():
    a = '[Event "X"]\n[Site "Y"]\n[Result "1-0"]\n\n1. e4 e5 1-0\n'
    b = '[Site "Y"]\n[Event "X"]\n[Result "1-0"]\n\n1. e4 e5 1-0\n'
    assert canonical_hash(a, "pgn") == canonical_hash(b, "pgn")


# --- PGN: comment whitespace --------------------------------------------

def test_pgn_comment_tabs_vs_spaces_hash_equal():
    a = '[Event "X"]\n[Result "*"]\n\n1. e4 {A\tB} e5 *\n'
    b = '[Event "X"]\n[Result "*"]\n\n1. e4 {A B} e5 *\n'
    assert canonical_hash(a, "pgn") == canonical_hash(b, "pgn")


def test_pgn_comment_multiple_spaces_hash_equal():
    a = '[Event "X"]\n[Result "*"]\n\n1. e4 {A   B} e5 *\n'
    b = '[Event "X"]\n[Result "*"]\n\n1. e4 {A B} e5 *\n'
    assert canonical_hash(a, "pgn") == canonical_hash(b, "pgn")


def test_pgn_comment_linewrap_hash_equal():
    wrapped = '[Event "X"]\n[Result "*"]\n\n1. e4 {Hello\nworld} e5 *\n'
    flat    = '[Event "X"]\n[Result "*"]\n\n1. e4 {Hello world} e5 *\n'
    assert canonical_hash(wrapped, "pgn") == canonical_hash(flat, "pgn")


# --- PGN: annotations affect identity (NO DATA LOSS) --------------------

def test_pgn_with_and_without_comment_hash_unequal():
    plain    = '[Event "X"]\n[Result "*"]\n\n1. e4 e5 *\n'
    annotated = '[Event "X"]\n[Result "*"]\n\n1. e4 {nice} e5 *\n'
    assert canonical_hash(plain, "pgn") != canonical_hash(annotated, "pgn")


def test_pgn_with_different_comments_hash_unequal():
    a = '[Event "X"]\n[Result "*"]\n\n1. e4 {one} e5 *\n'
    b = '[Event "X"]\n[Result "*"]\n\n1. e4 {two} e5 *\n'
    assert canonical_hash(a, "pgn") != canonical_hash(b, "pgn")


# --- PGN: clk and other [%cmd] preserved --------------------------------

def test_pgn_clk_annotation_preserved_in_hash():
    a = '[Event "X"]\n[Result "*"]\n\n1. e4 {[%clk 0:05:00]} e5 *\n'
    b = '[Event "X"]\n[Result "*"]\n\n1. e4 e5 *\n'
    assert canonical_hash(a, "pgn") != canonical_hash(b, "pgn")


# --- FEN: defaults filled in -------------------------------------------

def test_fen_short_equals_full():
    short = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"
    full  = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    assert canonical_hash(short, "fen") == canonical_hash(full, "fen")


def test_fen_extra_whitespace_around_fields():
    a = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    b = "  rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR  w  KQkq  -  0  1  "
    # FEN parser tolerates internal multi-spaces too -- whichever python-chess
    # does, our canonical form must be identical.
    assert canonical_hash(a, "fen") == canonical_hash(b, "fen")


# --- Errors -------------------------------------------------------------

def test_unknown_format_raises():
    with pytest.raises(ValueError):
        canonical_hash("anything", "epd")


def test_invalid_fen_raises():
    with pytest.raises(ValueError):
        canonical_hash("not a fen", "fen")


def test_empty_pgn_raises():
    with pytest.raises(ValueError):
        canonical_hash("", "pgn")


# --- Real PGN fixtures: source-formatting variants hash equal -----------

@pytest.mark.parametrize("filename", REAL_PGN_FIXTURES)
def test_real_pgn_crlf_hash_equal(filename):
    """Same PGN with CRLF vs LF line endings must hash equal."""
    lf = (FIXTURES / filename).read_text(encoding="utf-8")
    crlf = lf.replace("\n", "\r\n")
    assert canonical_hash(lf, "pgn") == canonical_hash(crlf, "pgn")


@pytest.mark.parametrize("filename", REAL_PGN_FIXTURES)
def test_real_pgn_trailing_whitespace_hash_equal(filename):
    """Trailing whitespace on header lines must not affect the hash."""
    text = (FIXTURES / filename).read_text(encoding="utf-8")
    padded = re.sub(r"^(\[.+\])$", r"\1   ", text, flags=re.MULTILINE)
    assert canonical_hash(text, "pgn") == canonical_hash(padded, "pgn")


@pytest.mark.parametrize("filename", REAL_PGN_FIXTURES)
def test_real_pgn_header_reorder_hash_equal(filename):
    """Reversing header order must not affect the hash (sorted-for-hash)."""
    text = (FIXTURES / filename).read_text(encoding="utf-8")
    lines = text.split("\n")
    header_lines = [ln for ln in lines if ln.startswith("[")]
    body_lines = [ln for ln in lines if not ln.startswith("[")]
    shuffled = "\n".join(list(reversed(header_lines)) + body_lines)
    assert canonical_hash(text, "pgn") == canonical_hash(shuffled, "pgn")


@pytest.mark.parametrize("filename", REAL_PGN_FIXTURES)
def test_real_pgn_trailing_newlines_hash_equal(filename):
    """Trailing newlines at EOF must not affect the hash."""
    text = (FIXTURES / filename).read_text(encoding="utf-8")
    bloated = text.rstrip("\n") + "\n\n\n\n\n"
    assert canonical_hash(text, "pgn") == canonical_hash(bloated, "pgn")


@pytest.mark.parametrize("filename", REAL_PGN_FIXTURES)
def test_real_pgn_bom_hash_equal(filename):
    """UTF-8 BOM at start of file must not affect the hash."""
    text = (FIXTURES / filename).read_text(encoding="utf-8")
    with_bom = "﻿" + text
    assert canonical_hash(text, "pgn") == canonical_hash(with_bom, "pgn")


def _split_headers_and_moves(text: str) -> tuple[str, str]:
    """Split a PGN into (headers_block, moves_block).

    A PGN may have blank lines between headers (e.g. before a late
    Opening tag), so partition('\\n\\n') is unreliable. Walk lines and
    find the first one that is neither a header nor blank.
    """
    lines = text.splitlines(keepends=True)
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if stripped and not stripped.startswith("["):
            return "".join(lines[:i]), "".join(lines[i:])
    return text, ""


@pytest.mark.parametrize("filename", REAL_PGN_FIXTURES)
def test_real_pgn_extra_inter_move_whitespace_hash_equal(filename):
    """Extra spaces between move tokens must not affect the hash."""
    text = (FIXTURES / filename).read_text(encoding="utf-8")
    head, body = _split_headers_and_moves(text)
    bloated = head + body.replace(" ", "  ")
    assert canonical_hash(text, "pgn") == canonical_hash(bloated, "pgn")


@pytest.mark.parametrize("filename", REAL_PGN_FIXTURES)
def test_real_pgn_tabs_in_move_text_hash_equal(filename):
    """Tabs between move tokens must not affect the hash."""
    text = (FIXTURES / filename).read_text(encoding="utf-8")
    head, body = _split_headers_and_moves(text)
    bloated = head + body.replace(" ", "\t")
    assert canonical_hash(text, "pgn") == canonical_hash(bloated, "pgn")
