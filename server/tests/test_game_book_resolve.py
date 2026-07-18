"""/game/new book resolution (_resolve_book): EPD seeds vs PGN refs, and
the cursor-advance rules (per game, sequential only, never on a miss)."""
from __future__ import annotations

import pytest

from sturddle_view.api.game import _resolve_book
from sturddle_view.config import BOOK_ORDER_RANDOM, Settings
from sturddle_view.play.opening_lines import BookRef

_EPD_LINE = "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -\n"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """Settings with the book toggle on and cursor persistence redirected
    away from the real user config file."""
    monkeypatch.setenv("SV_SETTINGS_FILE", str(tmp_path / "settings.json"))
    return Settings(token="t", auth_disabled=True, hve_use_opening_book=True)


async def test_toggle_off_yields_nothing(settings, tmp_path):
    settings.hve_use_opening_book = False
    p = tmp_path / "b.pgn"
    p.write_text("1. e4 e5 *\n", encoding="utf-8")
    settings.engine_default_book_path = str(p)
    assert await _resolve_book(settings) == (None, None)


async def test_missing_book_yields_nothing_and_keeps_cursor(settings, tmp_path):
    settings.engine_default_book_path = str(tmp_path / "missing.pgn")
    settings.engine_default_book_cursor = 3
    assert await _resolve_book(settings) == (None, None)
    assert settings.engine_default_book_cursor == 3


async def test_pgn_yields_ref_with_cursor_anchor_and_advances(settings, tmp_path):
    p = tmp_path / "b.pgn"
    p.write_text("1. e4 e5 *\n", encoding="utf-8")
    settings.engine_default_book_path = str(p)
    settings.engine_default_book_cursor = 3
    seed_fen, book = await _resolve_book(settings)
    assert seed_fen is None
    assert book == BookRef(path=str(p), plies=None, order=None, anchor=3)
    assert settings.engine_default_book_cursor == 4


async def test_epd_yields_seed_no_ref_and_advances(settings, tmp_path):
    p = tmp_path / "b.epd"
    p.write_text(_EPD_LINE, encoding="utf-8")
    settings.engine_default_book_path = str(p)
    seed_fen, book = await _resolve_book(settings)
    assert seed_fen is not None
    assert book is None
    assert settings.engine_default_book_cursor == 1


async def test_random_order_never_advances_cursor(settings, tmp_path):
    p = tmp_path / "b.pgn"
    p.write_text("1. e4 e5 *\n", encoding="utf-8")
    settings.engine_default_book_path = str(p)
    settings.engine_default_book_order = BOOK_ORDER_RANDOM
    _, book = await _resolve_book(settings)
    assert book is not None
    assert settings.engine_default_book_cursor == 0
