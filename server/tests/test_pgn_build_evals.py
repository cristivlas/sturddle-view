"""Tests for the cutechess/fastchess eval-history path in build_pgn().

Covers the unit, round-trip and edge cases from docs/pgn-export.md
(TODO: Engine Evaluations in Exported PGN).
"""
from __future__ import annotations

import io

import chess
import chess.pgn
import pytest

from sturddle_view.chess.pgn_build import build_pgn
from sturddle_view.play.import_position import _parse_pgn_eval


def _read_nodes(text: str) -> list[chess.pgn.ChildNode]:
    game = chess.pgn.read_game(io.StringIO(text))
    assert game is not None
    return list(game.mainline())


def _build(moves: list[str], evals: list[dict | None] | None, **kw) -> str:
    kw.setdefault("headers", {"Event": "T"})
    return build_pgn(
        start_fen=None,
        moves_uci=moves,
        eval_history=evals,
        **kw,
    )


def test_white_cp_no_flip():
    text = _build(["e2e4"], [{"cp": 34, "depth": 12}])
    nodes = _read_nodes(text)
    assert "+0.34/12" in nodes[0].comment


def test_black_cp_flip():
    # White's eval after black plays is from black's POV; white-POV in memory
    # -> on a black-to-move ply we flip the sign at write time.
    text = _build(["e2e4", "e7e5"], [None, {"cp": 34, "depth": 8}])
    nodes = _read_nodes(text)
    assert "-0.34/8" in nodes[1].comment


def test_white_mate():
    text = _build(["e2e4"], [{"mate": 5}])
    nodes = _read_nodes(text)
    assert "+M5" in nodes[0].comment


def test_black_mate_flip():
    text = _build(["e2e4", "e7e5"], [None, {"mate": 5}])
    nodes = _read_nodes(text)
    assert "-M5" in nodes[1].comment


def test_missing_eval_emits_time_only():
    # When eval is None on a ply but clock_history covers it, we emit only
    # the time token. No bare "/<depth>" or stray slash.
    text = _build(
        ["e2e4", "e7e5"],
        [None, None],
        clock_history=[(300.0, 300.0), (299.0, 300.0)],
        final_clocks=(299.0, 298.0),
        time_control=(300, 0),
    )
    nodes = _read_nodes(text)
    # The token includes a 's' suffix; no slash means no eval/depth fragment.
    for node in nodes:
        assert "/" not in (node.comment or "")
        assert "s" in (node.comment or "")


def test_depth_omitted_when_missing():
    text = _build(["e2e4"], [{"cp": 34}])
    nodes = _read_nodes(text)
    # "+0.34" with no slash-depth.
    assert "+0.34" in nodes[0].comment
    assert "+0.34/" not in nodes[0].comment


def test_eval_history_none_equivalent_to_today():
    moves = ["e2e4", "e7e5"]
    clocks = [(300.0, 300.0), (299.0, 300.0), (299.0, 298.0)]
    without = build_pgn(
        start_fen=None,
        moves_uci=moves,
        clock_history=clocks,
        headers={"Event": "T"},
        result="*",
        termination="unterminated",
    )
    with_none = build_pgn(
        start_fen=None,
        moves_uci=moves,
        clock_history=clocks,
        headers={"Event": "T"},
        result="*",
        termination="unterminated",
        eval_history=None,
    )
    assert without == with_none
    # Sanity: %clk tags are present when eval_history is None.
    assert "%clk" in without


def test_eval_history_present_drops_clk_tags():
    text = _build(
        ["e2e4"],
        [{"cp": 30, "depth": 10}],
        clock_history=[(300.0, 300.0)],
        final_clocks=(298.5, 300.0),
        time_control=(300, 0),
    )
    assert "%clk" not in text


def test_length_mismatch_raises():
    with pytest.raises(ValueError):
        build_pgn(
            start_fen=None,
            moves_uci=["e2e4", "e7e5"],
            eval_history=[{"cp": 10}],  # short
            headers={"Event": "T"},
            result="*",
            termination="unterminated",
        )
    with pytest.raises(ValueError):
        build_pgn(
            start_fen=None,
            moves_uci=["e2e4"],
            eval_history=[{"cp": 10}, {"cp": 20}],  # long
            headers={"Event": "T"},
            result="*",
            termination="unterminated",
        )


def test_elapsed_uses_clock_delta_plus_increment():
    # White plays, started with 300, ends with 299; increment 2 -> elapsed = 3.0.
    text = _build(
        ["e2e4"],
        [{"cp": 30, "depth": 5}],
        clock_history=[(300.0, 300.0)],
        final_clocks=(299.0, 300.0),
        time_control=(300, 2),
    )
    nodes = _read_nodes(text)
    # Token form: "+0.30/5 3.0s"
    assert "3.0s" in nodes[0].comment


def test_round_trip_white_pov_preserved():
    # Build with mixed white-POV memory values; re-parse; values match.
    originals: list[dict | None] = [
        {"cp": 25, "depth": 8},
        {"cp": -40, "depth": 9},
        {"mate": 3, "depth": 7},
        None,
        {"cp": 0, "depth": 6},
    ]
    moves = ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4"]
    text = _build(moves, originals)
    game = chess.pgn.read_game(io.StringIO(text))
    assert game is not None
    replay = chess.Board()
    parsed: list[dict | None] = []
    for node in game.mainline():
        mover_white = (replay.turn == chess.WHITE)
        parsed.append(_parse_pgn_eval(node.comment, mover_white))
        replay.push(node.move)
    # Compare just cp/mate (depth round-trip is bonus; the doc only requires
    # the magnitude+sign equivalence).
    def _key(s: dict | None) -> tuple | None:
        if s is None:
            return None
        if "mate" in s:
            return ("mate", s["mate"])
        return ("cp", s["cp"])
    assert [_key(s) for s in parsed] == [_key(s) for s in originals]


def test_all_none_eval_history_emits_no_eval_tokens():
    text = _build(
        ["e2e4", "e7e5"],
        [None, None],
    )
    # No eval/depth fragment in any comment.
    nodes = _read_nodes(text)
    for node in nodes:
        c = node.comment or ""
        assert "/" not in c
        # And no %clk either (eval_history was provided, even if all-None).
    assert "%clk" not in text


def test_empty_game_with_eval_history_succeeds():
    # No moves, empty eval_history -> length match, no movetext-level tokens.
    text = build_pgn(
        start_fen=None,
        moves_uci=[],
        eval_history=[],
        headers={"Event": "T"},
        result="*",
        termination="unterminated",
    )
    game = chess.pgn.read_game(io.StringIO(text))
    assert game is not None
    assert list(game.mainline()) == []
