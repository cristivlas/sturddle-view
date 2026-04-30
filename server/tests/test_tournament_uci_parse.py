"""Slice 9c: UCI line parser used by the proxy WS handler."""
from __future__ import annotations

from sturddle_view.tournament.uci_parse import parse_uci_line


def test_position_startpos_returns_initial_fen():
    p = parse_uci_line("position startpos")
    assert p["kind"] == "position"
    assert p["fen"].startswith("rnbqkbnr/")
    assert p["ply"] == 0
    assert p["last_move"] is None
    assert p["side_to_move"] == "white"


def test_position_startpos_with_moves_reconstructs_fen():
    p = parse_uci_line("position startpos moves e2e4 c7c5 g1f3")
    assert p["kind"] == "position"
    assert p["ply"] == 3
    assert p["last_move"] == "g1f3"
    assert p["side_to_move"] == "black"
    # Black to move after 1.e4 c5 2.Nf3
    assert " b " in p["fen"]


def test_position_with_invalid_move_returns_partial_state():
    p = parse_uci_line("position startpos moves e2e4 totally-bogus")
    assert p is not None
    assert p["error"] == "illegal_move_in_position"
    # The fen reflects what we managed to apply (e2e4 only).
    assert " b " in p["fen"]


def test_info_parses_score_depth_pv():
    p = parse_uci_line("info depth 12 seldepth 18 score cp 42 nodes 100000 nps 1500000 pv e2e4 c7c5 g1f3")
    assert p["kind"] == "info"
    assert p["depth"] == 12
    assert p["seldepth"] == 18
    assert p["score_cp"] == 42
    assert p["nodes"] == 100000
    assert p["nps"] == 1500000
    assert p["pv"] == ["e2e4", "c7c5", "g1f3"]


def test_info_score_mate():
    p = parse_uci_line("info depth 8 score mate 3 pv e2e4 e7e5 d1h5")
    assert p["score_mate"] == 3
    assert "score_cp" not in p


def test_info_string_returns_none():
    """info string lines have no useful structured fields."""
    assert parse_uci_line("info string Hash usage 50%") is None


def test_bestmove():
    assert parse_uci_line("bestmove e2e4") == {"kind": "bestmove", "move": "e2e4"}
    # bestmove with ponder
    p = parse_uci_line("bestmove e2e4 ponder e7e5")
    assert p["move"] == "e2e4"


def test_go():
    p = parse_uci_line("go wtime 60000 btime 60000 winc 600 binc 600")
    assert p["kind"] == "go"
    assert p["wtime"] == 60000
    assert p["btime"] == 60000
    assert p["winc"] == 600
    assert p["binc"] == 600


def test_go_with_movetime():
    p = parse_uci_line("go movetime 5000")
    assert p["movetime"] == 5000


def test_unrecognized_returns_none():
    assert parse_uci_line("readyok") is None
    assert parse_uci_line("uciok") is None
    assert parse_uci_line("id name Sturddle 2.5.0") is None
    assert parse_uci_line("") is None
    assert parse_uci_line("   ") is None
