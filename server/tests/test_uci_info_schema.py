"""R7 unified UCI info schema -- shared by HVE serialize and tournament parse."""
from __future__ import annotations

import chess
import chess.engine
import pytest

from sturddle_view.chess.engine_info import parse_info_tokens, serialize_info


def _typed_info() -> chess.engine.InfoDict:
    pv = [
        chess.Move.from_uci("e2e4"),
        chess.Move.from_uci("e7e5"),
        chess.Move.from_uci("g1f3"),
    ]
    return {
        "depth": 12,
        "seldepth": 18,
        "time": 1234,
        "nodes": 100_000,
        "nps": 1_500_000,
        "hashfull": 450,
        "tbhits": 0,
        "score": chess.engine.PovScore(chess.engine.Cp(42), chess.WHITE),
        "pv": pv,
    }


def test_serialize_info_from_typed_dict_matches_schema():
    out = serialize_info(_typed_info(), board=chess.Board(), pov=chess.WHITE)
    assert out["depth"] == 12
    assert out["seldepth"] == 18
    assert out["time"] == 1234
    assert out["nodes"] == 100_000
    assert out["nps"] == 1_500_000
    assert out["hashfull"] == 450
    assert out["tbhits"] == 0
    assert out["score"] == {"cp": 42}
    assert out["pv_uci"] == ["e2e4", "e7e5", "g1f3"]
    assert out["pv"] == ["1. e4 e5 2. Nf3"]


def test_parse_info_from_raw_line_matches_schema():
    out = parse_info_tokens(
        "depth 12 seldepth 18 score cp 42 nodes 100000 nps 1500000"
        " pv e2e4 c7c5 g1f3"
    )
    assert out["depth"] == 12
    assert out["seldepth"] == 18
    assert out["score"] == {"cp": 42}
    assert out["nodes"] == 100_000
    assert out["nps"] == 1_500_000
    assert out["pv_uci"] == ["e2e4", "c7c5", "g1f3"]


def test_both_paths_produce_same_keys_for_score_and_depth():
    """When source data carries identical fields, key sets converge."""
    typed = serialize_info(
        {"depth": 8, "score": chess.engine.PovScore(chess.engine.Cp(15), chess.WHITE)},
        board=None, pov=chess.WHITE,
    )
    raw = parse_info_tokens("depth 8 score cp 15")
    assert typed.keys() == raw.keys()
    assert typed["score"] == raw["score"] == {"cp": 15}


def test_parse_info_returns_none_for_useless_info_strings():
    assert parse_info_tokens("string Hash usage 50%") is None
    assert parse_info_tokens("") is None


def test_serialize_info_omits_pv_san_when_no_board():
    out = serialize_info(_typed_info(), board=None, pov=chess.WHITE)
    assert "pv" not in out
    assert out["pv_uci"] == ["e2e4", "e7e5", "g1f3"]


def test_serialize_info_mate_score_round_trips_through_pov():
    info: chess.engine.InfoDict = {
        "depth": 6,
        "score": chess.engine.PovScore(chess.engine.Mate(3), chess.WHITE),
    }
    out = serialize_info(info, board=None, pov=chess.WHITE)
    assert out["score"] == {"mate": 3}


def test_parse_info_mate_score():
    out = parse_info_tokens("depth 8 score mate 3 pv e2e4 e7e5 d1h5")
    assert out["score"] == {"mate": 3}


@pytest.mark.parametrize(
    "white_cp,pov,expected_cp",
    [
        (50, chess.WHITE, 50),
        (50, chess.BLACK, -50),
    ],
)
def test_serialize_info_score_pov_flips_for_black(white_cp, pov, expected_cp):
    """``pov`` controls whose perspective the score is reported from."""
    info: chess.engine.InfoDict = {
        "score": chess.engine.PovScore(chess.engine.Cp(white_cp), chess.WHITE),
    }
    out = serialize_info(info, board=None, pov=pov)
    assert out["score"] == {"cp": expected_cp}


def test_serialize_info_falls_back_to_uci_pv_when_san_rejects():
    """A pv with illegal continuation should not crash; falls back to UCI list."""
    board = chess.Board()
    # Two legal first moves, then an illegal-from-resulting-position move.
    pv = [
        chess.Move.from_uci("e2e4"),
        chess.Move.from_uci("a1a8"),  # illegal: piece can't jump
    ]
    info: chess.engine.InfoDict = {"pv": pv}
    out = serialize_info(info, board=board, pov=chess.WHITE)
    assert out["pv_uci"] == ["e2e4", "a1a8"]
    assert out["pv"] == ["e2e4", "a1a8"]
