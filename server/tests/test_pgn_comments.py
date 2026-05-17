"""Unit tests for PGN comment sanitization (view-mode commentary)."""
from __future__ import annotations

from sturddle_view.play.import_position import _sanitize_comment, parse_pgn


def test_strips_clk_and_eval_brackets():
    s = _sanitize_comment(
        "[%clk 0:05:01] [%eval 0.34,12] Best move (alternative line) +0.34/12 5.2s"
    )
    assert s == "Best move"


def test_strips_cal_and_csl():
    s = _sanitize_comment(
        "Excellent positional play! [%cal Re4d5,Gg1f3] [%csl Yc4]"
    )
    assert s == "Excellent positional play!"


def test_only_machine_annotations_returns_none():
    assert _sanitize_comment("[%clk 0:01]") is None
    assert _sanitize_comment("[%eval 1.20,18]") is None


def test_empty_and_blank():
    assert _sanitize_comment(None) is None
    assert _sanitize_comment("") is None
    assert _sanitize_comment("   \n\n  ") is None


def test_preserves_paragraph_breaks():
    s = _sanitize_comment("First paragraph.\n\nSecond paragraph.")
    assert s == "First paragraph.\n\nSecond paragraph."


def test_collapses_intra_paragraph_whitespace():
    s = _sanitize_comment("Tactical    shot\n  here.")
    assert s == "Tactical shot here."


def test_strips_parenthesized_variations():
    s = _sanitize_comment("Sound move (1...e5 2.Nf3) but committal.")
    assert s == "Sound move but committal."


def test_strips_cutechess_trailing_token():
    s = _sanitize_comment("Engine preferred Rxd5 +1.20/22 3.4s")
    assert s == "Engine preferred Rxd5"


def test_pure_prose_unchanged():
    assert _sanitize_comment("A purely human comment.") == "A purely human comment."


# --- parse_pgn integration -------------------------------------------------


def test_parse_pgn_collects_per_ply_comments():
    pgn = """[Event "test"]
[White "A"]
[Black "B"]

1. e4 {[%clk 0:05:01] Strong center.} e5 {[%eval 0.10,10]} 2. Nf3 {Develops a piece.} *
"""
    pos = parse_pgn(pgn)
    assert pos.comments == ["Strong center.", None, "Develops a piece."]


def test_parse_pgn_comments_none_when_absent():
    pgn = """[Event "x"]

1. e4 e5 2. Nf3 *
"""
    pos = parse_pgn(pgn)
    assert pos.comments is None


def test_parse_pgn_root_comment_from_annotator_and_game_comment():
    pgn = """[Event "x"]
[Annotator "Magnus"]

{Pre-game thoughts here.} 1. e4 e5 *
"""
    pos = parse_pgn(pgn)
    assert pos.root_comment is not None
    assert "Annotator: Magnus" in pos.root_comment
    assert "Pre-game thoughts here." in pos.root_comment


def test_parse_pgn_root_comment_none_when_absent():
    pgn = """[Event "x"]

1. e4 e5 *
"""
    pos = parse_pgn(pgn)
    assert pos.root_comment is None


# --- HumanVsEngine view_payload plumbing ----------------------------------


import pytest
from unittest.mock import AsyncMock

from sturddle_view.config import Settings
from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine


class _StubEngine:
    def send_line(self, _line: str) -> None:
        pass

    async def quit(self) -> None:
        return None


@pytest.fixture
def hve(tmp_path):
    settings = Settings()
    settings.pgn_autosave = False
    settings.pgn_dir = tmp_path
    bus = EventBus()
    h = HumanVsEngine(engine_path="/nonexistent", bus=bus, settings=settings)

    async def fake_ensure_engine():
        h._engine = _StubEngine()
        return h._engine

    h._ensure_engine = fake_ensure_engine
    h._engine_to_move = AsyncMock()
    return h


async def test_view_payload_exposes_comment_at_cursor(hve):
    await hve.enter_view_mode(
        start_fen=None,
        moves_uci=["e2e4", "e7e5", "g1f3"],
        clock_history=None,
        comments=["Strong center.", None, "Develops a piece."],
    )
    await hve.view_last()
    evt = hve._board_event()
    view = evt.payload["view"]
    assert view["cursor"] == 3
    assert view["comment"] == "Develops a piece."
    assert view["has_comment"] is True

    await hve.view_back()
    view = hve._board_event().payload["view"]
    assert view["comment"] is None  # ply 2 had no comment
    assert view["has_comment"] is True


async def test_view_payload_root_comment_at_cursor_zero(hve):
    await hve.enter_view_mode(
        start_fen=None,
        moves_uci=["e2e4", "e7e5"],
        clock_history=None,
        comments=["After e4.", None],
        root_comment="Annotator: Magnus\n\nPre-game thoughts.",
    )
    await hve.view_first()
    view = hve._board_event().payload["view"]
    assert view["cursor"] == 0
    assert view["comment"] == "Annotator: Magnus\n\nPre-game thoughts."
    assert view["has_comment"] is True


async def test_view_payload_has_comment_false_when_no_comments(hve):
    await hve.enter_view_mode(
        start_fen=None,
        moves_uci=["e2e4", "e7e5"],
        clock_history=None,
    )
    view = hve._board_event().payload["view"]
    assert view["comment"] is None
    assert view["has_comment"] is False
