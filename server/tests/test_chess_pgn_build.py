"""Tests for chess/pgn_build.py (R6b / P5).

All tests are red until chess/pgn_build.py is implemented.
"""
from __future__ import annotations

import pathlib

import chess
import chess.pgn
import pytest

# -- module under test (will fail ImportError until implemented) --
from sturddle_view.chess.pgn_build import build_pgn

SNAPSHOTS = pathlib.Path(__file__).parent / "fixtures" / "pgn_autosave_snapshots"

_STABLE_HEADERS_SKIP = {"[Date ", "[White \"", "[Black \""}


def _strip_run_varying(pgn_text: str) -> str:
    lines = pgn_text.splitlines()
    return "\n".join(ln for ln in lines if not any(ln.startswith(p) for p in _STABLE_HEADERS_SKIP))


# ---------------------------------------------------------------------------
# Minimal / structural tests
# ---------------------------------------------------------------------------


def test_build_minimal_pgn_from_startpos_no_moves():
    text = build_pgn(
        start_fen=None,
        moves_uci=[],
        headers={"Event": "Test", "Site": "?"},
        result="*",
        termination="unterminated",
    )
    game = chess.pgn.read_game(__import__("io").StringIO(text))
    assert game is not None
    assert game.headers["Result"] == "*"
    assert list(game.mainline()) == []


def test_build_with_moves_includes_san_movetext():
    text = build_pgn(
        start_fen=None,
        moves_uci=["e2e4", "e7e5", "g1f3"],
        headers={"Event": "T"},
        result="*",
        termination="unterminated",
    )
    assert "e4" in text
    assert "e5" in text
    assert "Nf3" in text


def test_build_round_trips_through_chess_pgn_parser():
    moves = ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4"]
    text = build_pgn(
        start_fen=None,
        moves_uci=moves,
        headers={"Event": "T", "White": "A", "Black": "B"},
        result="1-0",
        termination="checkmate",
    )
    game = chess.pgn.read_game(__import__("io").StringIO(text))
    assert [node.move.uci() for node in game.mainline()] == moves
    assert game.headers["Result"] == "1-0"
    assert game.headers["Termination"] == "checkmate"


def test_build_from_custom_fen_emits_fen_and_setup_headers():
    fen = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2"
    text = build_pgn(
        start_fen=fen,
        moves_uci=["g1f3"],
        headers={"Event": "T"},
        result="*",
        termination="unterminated",
    )
    assert "[SetUp \"1\"]" in text
    assert "[FEN " in text


def test_build_attaches_clk_per_ply():
    clock_history = [
        (300.0, 300.0),
        (299.0, 300.0),
        (299.0, 298.0),
    ]
    text = build_pgn(
        start_fen=None,
        moves_uci=["e2e4", "e7e5"],
        clock_history=clock_history,
        headers={"Event": "T"},
        result="*",
        termination="unterminated",
    )
    game = chess.pgn.read_game(__import__("io").StringIO(text))
    nodes = list(game.mainline())
    # ply 0 mover is White; clock AFTER ply 0 = clock_history[1][0] = 299.0
    assert nodes[0].clock() == pytest.approx(299.0)
    # ply 1 mover is Black; clock AFTER ply 1 = clock_history[2][1] = 298.0
    assert nodes[1].clock() == pytest.approx(298.0)


def test_build_attaches_clk_for_final_clocks_when_history_short():
    # history covers plies 0..N-1; last ply uses final_clocks
    clock_history = [
        (300.0, 300.0),
        (299.0, 300.0),
    ]
    text = build_pgn(
        start_fen=None,
        moves_uci=["e2e4", "e7e5", "g1f3"],
        clock_history=clock_history,
        final_clocks=(298.5, 300.0),
        headers={"Event": "T"},
        result="*",
        termination="unterminated",
    )
    game = chess.pgn.read_game(__import__("io").StringIO(text))
    nodes = list(game.mainline())
    # ply 2 mover is White; last ply uses final_clocks[0]
    assert nodes[2].clock() == pytest.approx(298.5)


def test_build_includes_eco_and_opening_headers_when_provided():
    text = build_pgn(
        start_fen=None,
        moves_uci=["e2e4"],
        headers={"Event": "T"},
        opening=("B00", "King's Pawn"),
        result="*",
        termination="unterminated",
    )
    assert "[ECO \"B00\"]" in text
    assert "[Opening \"King's Pawn\"]" in text


def test_build_omits_eco_when_opening_is_none():
    text = build_pgn(
        start_fen=None,
        moves_uci=["e2e4"],
        headers={"Event": "T"},
        opening=None,
        result="*",
        termination="unterminated",
    )
    assert "ECO" not in text
    assert "Opening" not in text


def test_build_result_and_termination_round_trip():
    for result, termination in [
        ("1-0", "checkmate"),
        ("0-1", "resignation"),
        ("1/2-1/2", "agreement"),
        ("*", "unterminated"),
    ]:
        text = build_pgn(
            start_fen=None,
            moves_uci=[],
            headers={"Event": "T"},
            result=result,
            termination=termination,
        )
        game = chess.pgn.read_game(__import__("io").StringIO(text))
        assert game.headers["Result"] == result
        assert game.headers["Termination"] == termination


def test_build_timecontrol_header_formats_int_seconds():
    text = build_pgn(
        start_fen=None,
        moves_uci=[],
        headers={"Event": "T"},
        result="*",
        termination="unterminated",
        time_control=(300, 2),
    )
    assert "[TimeControl \"300+2\"]" in text


# ---------------------------------------------------------------------------
# Equivalence against P1 autosave snapshots
# ---------------------------------------------------------------------------


def _load_snapshot(name: str) -> str:
    return (SNAPSHOTS / name).read_text(encoding="utf-8")


def test_build_pgn_matches_startpos_snapshot():
    # Matches test_autosave_startpos_short_game_matches_snapshot in P1.
    # Seed: e2e4 e7e5 (engine). Human plays g1f3, then resigns (0-1).
    # eval_history is all-None: human-only game; cutechess output emits
    # time-only tokens per ply (and drops [%clk]).
    text = build_pgn(
        start_fen=None,
        moves_uci=["e2e4", "e7e5", "g1f3"],
        clock_history=[
            (300.0, 300.0),
            (300.0, 300.0),
            (300.0, 300.0),
        ],
        final_clocks=(302.0, 300.0),
        headers={
            "Event": "Sturddle View -- Human vs Engine",
            "Site": "Sturddle View",
        },
        result="0-1",
        termination="resignation",
        time_control=(300, 2),
        eval_history=[None, None, None],
    )
    actual = _strip_run_varying(text)
    expected = _load_snapshot("startpos_short_game.pgn")
    assert actual == expected


def test_build_pgn_matches_custom_fen_snapshot():
    fen = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2"
    text = build_pgn(
        start_fen=fen,
        moves_uci=["g1f3", "b8c6", "f1c4"],
        clock_history=[
            (300.0, 300.0),
            (300.0, 300.0),
            (300.0, 300.0),
        ],
        final_clocks=(302.0, 300.0),
        headers={
            "Event": "Sturddle View -- Human vs Engine",
            "Site": "Sturddle View",
        },
        result="0-1",
        termination="resignation",
        time_control=(300, 2),
        eval_history=[None, None, None],
    )
    actual = _strip_run_varying(text)
    expected = _load_snapshot("custom_fen_game.pgn")
    assert actual == expected


def test_build_pgn_matches_seeded_clock_snapshot():
    # Seed: e2e4 e7e5 g1f3 b8c6. Human plays f1c4 then resigns.
    # clock_history has 4 seeded entries + one snapshot taken before the
    # live f1c4 move (matches what _maybe_save_pgn sees at autosave time).
    clock_history = [
        (300.0, 300.0),
        (300.0, 298.0),
        (297.0, 298.0),
        (297.0, 295.0),
        (297.0, 295.0),
    ]
    text = build_pgn(
        start_fen=None,
        moves_uci=["e2e4", "e7e5", "g1f3", "b8c6", "f1c4"],
        clock_history=clock_history,
        final_clocks=(299.0, 295.0),
        headers={
            "Event": "Sturddle View -- Human vs Engine",
            "Site": "Sturddle View",
        },
        result="0-1",
        termination="resignation",
        time_control=(300, 2),
        eval_history=[None] * 5,
    )
    actual = _strip_run_varying(text)
    expected = _load_snapshot("seeded_clock_history_game.pgn")
    assert actual == expected