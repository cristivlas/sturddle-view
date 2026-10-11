"""Unit tests for PGN comment sanitization (view-mode commentary)."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sturddle_view.chess.pgn_build import build_pgn
from sturddle_view.chess.score import SCORE_CP, SCORE_DEPTH, SCORE_MATE
from sturddle_view.config import Settings
from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine, ViewModeParams
from sturddle_view.play.import_position import _sanitize_comment, parse_pgn


def test_strips_clk_and_eval_brackets():
    s = _sanitize_comment(
        "[%clk 0:05:01] [%eval 0.34,12] Best move +0.34/12 5.2s"
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


def test_keeps_parenthesized_text():
    """Parens are the annotator's own text: prose asides and quoted lines."""
    prose = "The knight (on f3) holds; Rxd5 wins material (25.Rxd5 Qxd5)."
    assert _sanitize_comment(prose) == prose


@pytest.mark.parametrize("comment", [
    "(Ng5) 0.35/19 17",
    "(Kxf6) [%eval 273,31] [%emt 00:00:12]",
    "(O-O+) +1.20/18 3.0s",
    "(12...exd5 13.Nxd5) -0.40/21",
    "(Book)",
])
def test_drops_engine_pv_only_remainder(comment):
    """Cutechess-style PV in parens with nothing else readable is machine output."""
    assert _sanitize_comment(comment) is None


@pytest.mark.parametrize("comment", ["(only move)", "(forced)"])
def test_keeps_lone_human_parenthetical(comment):
    assert _sanitize_comment(comment) == comment
    assert _sanitize_comment(f"{comment} [%clk 0:04:58]") == comment


def test_keeps_lowercase_san_lead():
    assert _sanitize_comment("e4 is strong.") == "e4 is strong."
    assert _sanitize_comment("[%clk 0:05:01] exd5 recaptures.") == "exd5 recaptures."


def test_keeps_trailing_unsigned_integer_fraction():
    """Only engine-shaped evals (signed, or unsigned decimal) are tokens."""
    assert _sanitize_comment("Holds the draw, 1/2") == "Holds the draw, 1/2"


@pytest.mark.parametrize("token", ["0.00/20", "+2/10", "-M3/30", "M5/30", "+0.35/20 1.2s"])
def test_strips_engine_shaped_eval_token(token):
    assert _sanitize_comment(f"Solid. {token}") == "Solid."


def test_strips_cutechess_trailing_token():
    s = _sanitize_comment("Engine preferred Rxd5 +1.20/22 3.4s")
    assert s == "Engine preferred Rxd5"


def test_strips_trailing_decimal_seconds_after_prose():
    """Cutechess/fastchess sometimes emit '{ prose <time>s }' with no
    eval/depth prefix. The decimal-seconds tail is a machine signature
    and gets stripped even without a preceding eval token."""
    s = _sanitize_comment("Good move! 3.2s")
    assert s == "Good move!"


def test_strips_trailing_milliseconds_after_prose():
    s = _sanitize_comment("Quick reply 250ms")
    assert s == "Quick reply"


def test_strips_trailing_zero_decimal_seconds():
    s = _sanitize_comment("Forced 0.5s")
    assert s == "Forced"


def test_keeps_trailing_bare_integer_seconds():
    """Human prose often ends in 'Ns' without a decimal point ('took 7s',
    'won in 30s'). The band-aid strip deliberately ignores bare integer
    seconds to avoid eating these. Accept the tradeoff: cutechess
    output configured to emit bare integer seconds is rare, and the
    proper fix is bracket-tag emit (see docs/pgn-comment-format.md)."""
    assert _sanitize_comment("took 7s") == "took 7s"
    assert _sanitize_comment("won in 30s") == "won in 30s"


def test_keeps_mid_string_time_token():
    """The strip is anchored at end-of-comment. A time-shaped substring
    in the middle of prose must not be touched."""
    s = _sanitize_comment("Spent 5.0s and then blundered")
    assert s == "Spent 5.0s and then blundered"


def test_repro_bug_two_plies_one_with_eval_one_without():
    """Bug repro from docs/pgn-comment-format.md: cutechess output where
    one ply has eval/depth and the next has bare time only. Both
    plies must shed their machine token, leaving only prose."""
    assert _sanitize_comment("Ok, black plays Sicilian... -0.24/22") == \
        "Ok, black plays Sicilian..."
    assert _sanitize_comment("Good move! 3.2s") == "Good move!"


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


_ROUND_TRIP_PROSE = [
    "e4 grabs the centre (and frees the bishop).",
    None,
    "Nf3 wins material (3.Nxe5 Qe7), but holds the draw, 1/2",
    "(forced)",
]


_ROUND_TRIP_MOVES = ["e2e4", "e7e5", "g1f3", "b8c6"]
# Pre-move (white, black) clocks; whole-second spends round-trip exactly.
_ROUND_TRIP_CLOCKS = [(300.0, 300.0), (298.0, 300.0), (298.0, 297.0), (296.0, 297.0)]
_ROUND_TRIP_FINAL = (296.0, 295.0)
_ROUND_TRIP_TC = (300, 0)


@pytest.mark.parametrize("eval_history", [
    None,  # [%clk] path
    [  # token path
        {SCORE_CP: 30, SCORE_DEPTH: 20}, None,
        {SCORE_CP: -15, SCORE_DEPTH: 22}, {SCORE_CP: 10, SCORE_DEPTH: 21},
    ],
])
def test_saved_comments_survive_reload(eval_history):
    """App-saved annotations come back unchanged (modulo whitespace)."""
    pgn = build_pgn(
        start_fen=None,
        moves_uci=_ROUND_TRIP_MOVES,
        clock_history=_ROUND_TRIP_CLOCKS,
        final_clocks=_ROUND_TRIP_FINAL,
        headers={},
        eval_history=eval_history,
        comments=_ROUND_TRIP_PROSE,
    )
    assert parse_pgn(pgn).comments == _ROUND_TRIP_PROSE


def test_annotated_plies_keep_spent_time_on_reload():
    """Token-path PGN: a commented ply's "<prose> <time>s" still yields its
    spent time, so the reloaded clocks match -- with and without an eval."""
    evals = [None, {SCORE_CP: -20, SCORE_DEPTH: 20}, None, {SCORE_CP: 10, SCORE_DEPTH: 21}]
    comments = ["Main line.", "Solid reply.", "Develops.", "Natural."]
    pgn = build_pgn(
        start_fen=None,
        moves_uci=_ROUND_TRIP_MOVES,
        clock_history=_ROUND_TRIP_CLOCKS,
        final_clocks=_ROUND_TRIP_FINAL,
        headers={},
        time_control=_ROUND_TRIP_TC,
        eval_history=evals,
        comments=comments,
    )
    pos = parse_pgn(pgn)
    assert pos.comments == comments
    assert pos.clock_history == _ROUND_TRIP_CLOCKS
    assert (pos.final_white_time, pos.final_black_time) == _ROUND_TRIP_FINAL


def test_depthless_evals_survive_reload():
    """Evals with no search depth (e.g. a Lichess import) come back as
    evals -- not as comment text -- with every ply's spent time intact."""
    evals = [
        {SCORE_CP: 30},  # White ply
        {SCORE_CP: -20},  # Black ply (white POV)
        {SCORE_MATE: 3},
        {SCORE_CP: 10, SCORE_DEPTH: 21},  # with depth: token format unchanged
    ]
    comments = ["Main line.", None, "Mate threat.", None]
    pgn = build_pgn(
        start_fen=None,
        moves_uci=_ROUND_TRIP_MOVES,
        clock_history=_ROUND_TRIP_CLOCKS,
        final_clocks=_ROUND_TRIP_FINAL,
        headers={},
        time_control=_ROUND_TRIP_TC,
        eval_history=evals,
        comments=comments,
    )
    pos = parse_pgn(pgn)
    assert pos.eval_history == evals
    assert pos.comments == comments
    assert pos.clock_history == _ROUND_TRIP_CLOCKS
    assert (pos.final_white_time, pos.final_black_time) == _ROUND_TRIP_FINAL


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
    await hve.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5", "g1f3"],
        clock_history=None,
        comments=["Strong center.", None, "Develops a piece."],
    ))
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
    await hve.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5"],
        clock_history=None,
        comments=["After e4.", None],
        root_comment="Annotator: Magnus\n\nPre-game thoughts.",
    ))
    await hve.view_first()
    view = hve._board_event().payload["view"]
    assert view["cursor"] == 0
    assert view["comment"] == "Annotator: Magnus\n\nPre-game thoughts."
    assert view["has_comment"] is True


async def test_view_payload_has_comment_false_when_no_comments(hve):
    await hve.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5"],
        clock_history=None,
    ))
    view = hve._board_event().payload["view"]
    assert view["comment"] is None
    assert view["has_comment"] is False
