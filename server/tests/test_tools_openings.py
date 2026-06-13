"""Unit tests for the `related_openings` tool wrapper.

The proximity/family ranking itself lives in OpeningBook and is covered in
test_openings.py; here we exercise the tool's input handling (family parse,
position derivation) and error branches. Pure lookup, no engine -- the
`cancel_token` is accepted but unused, so None is fine.
"""
from __future__ import annotations

import chess
import pytest

from sturddle_view.openings import OpeningBook
from sturddle_view.play.tools_openings import (
    RELATED_OPENINGS_MAX_N,
    make_related_openings_tool,
)


_SICILIAN_MODERN = ("e2e4", "c7c5", "g1f3", "d7d6")  # 1.e4 c5 2.Nf3 d6
_MODERN_PREFIX = "1. e4 c5 2. Nf3 d6"


@pytest.fixture
def book():
    return OpeningBook.load()


def _board(*ucis: str) -> chess.Board:
    b = chess.Board()
    for u in ucis:
        b.push_uci(u)
    return b


# --- derive-from-position (no family) -------------------------------------


@pytest.mark.asyncio
async def test_derives_neighbors_from_current_line(book):
    tool = make_related_openings_tool(lambda: book, lambda: _board(*_SICILIAN_MODERN))
    out = await tool({}, cancel_token=None)
    assert "family" not in out  # no explicit filter requested
    rows = out["openings"]
    assert rows and out["count"] == len(rows) <= RELATED_OPENINGS_MAX_N
    # Move-tree neighbors of the played line, not B20 sidelines.
    assert all(o["pgn"].startswith(_MODERN_PREFIX) for o in rows), rows
    # Wire shape: eco/name/pgn/ply only (internal `moves` not leaked).
    assert set(rows[0]) == {"eco", "name", "pgn", "ply"}


@pytest.mark.asyncio
async def test_blank_family_falls_back_to_position(book):
    tool = make_related_openings_tool(lambda: book, lambda: _board(*_SICILIAN_MODERN))
    out = await tool({"family": "   "}, cancel_token=None)  # whitespace-only ignored
    assert "family" not in out
    assert all(o["pgn"].startswith(_MODERN_PREFIX) for o in out["openings"])


# --- explicit family filter -----------------------------------------------


@pytest.mark.asyncio
async def test_explicit_family_filters_pool(book):
    # A Sicilian position, but ask for Caro-Kann explicitly.
    tool = make_related_openings_tool(lambda: book, lambda: _board("e2e4", "c7c5"))
    out = await tool(
        {"family": "Caro-Kann Defense: Advance Variation"}, cancel_token=None
    )
    assert out["family"] == "Caro-Kann Defense"
    assert out["openings"]
    assert all(
        OpeningBook.family_of(o["name"]) == "Caro-Kann Defense"
        for o in out["openings"]
    )


@pytest.mark.asyncio
async def test_family_whitespace_and_subname_parse(book):
    tool = make_related_openings_tool(lambda: book, lambda: _board("e2e4"))
    out = await tool(
        {"family": "  Sicilian Defense: Najdorf Variation  "}, cancel_token=None
    )
    assert out["family"] == "Sicilian Defense"


@pytest.mark.asyncio
async def test_family_resolves_without_a_live_board(book):
    # No live position, but an explicit family still works.
    tool = make_related_openings_tool(lambda: book, lambda: None)
    out = await tool({"family": "Caro-Kann Defense"}, cancel_token=None)
    assert out["family"] == "Caro-Kann Defense"
    assert out["openings"]


# --- error branches -------------------------------------------------------


@pytest.mark.asyncio
async def test_no_opening_book_when_empty():
    tool = make_related_openings_tool(lambda: OpeningBook(), lambda: _board("e2e4"))
    assert await tool({}, cancel_token=None) == {"error": "no_opening_book"}


@pytest.mark.asyncio
async def test_no_opening_book_when_none():
    tool = make_related_openings_tool(lambda: None, lambda: _board("e2e4"))
    assert await tool({}, cancel_token=None) == {"error": "no_opening_book"}


@pytest.mark.asyncio
async def test_no_live_position_and_no_family(book):
    tool = make_related_openings_tool(lambda: book, lambda: None)
    assert await tool({}, cancel_token=None) == {"error": "no_live_position"}


@pytest.mark.asyncio
async def test_non_string_family_falls_back_to_position(book):
    tool = make_related_openings_tool(lambda: book, lambda: _board(*_SICILIAN_MODERN))
    out = await tool({"family": 42}, cancel_token=None)  # ignored, not a crash
    assert "family" not in out
    assert out["openings"]
