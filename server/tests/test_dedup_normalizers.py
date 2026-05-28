"""Tests for tool-arg normalizers used by the single-slot dedup cache
in `play.ai_analysis`. Pin behavior at the function level so cache
correctness doesn't drift when tool schemas evolve."""
from __future__ import annotations

import chess

from sturddle_view.play.ai_analysis import _NORMALIZERS


def _key(tool: str, input_: dict, board: chess.Board | None):
    normalizer = _NORMALIZERS[tool]
    return normalizer(input_, board)


# ---------- recommend_move / validate_move (share _norm_move_arg) -----


def test_recommend_move_uci_and_san_collapse_to_same_key():
    board = chess.Board()
    # "Nf3" SAN == "g1f3" UCI on startpos.
    k1 = _key("recommend_move", {"move": "Nf3"}, board)
    k2 = _key("recommend_move", {"move": "g1f3"}, board)
    assert k1 == k2
    assert k1 is not None


def test_recommend_move_same_move_same_depth_same_key():
    board = chess.Board()
    k1 = _key("recommend_move", {"move": "Nf3", "depth": 20}, board)
    k2 = _key("recommend_move", {"move": "Nf3", "depth": 20}, board)
    assert k1 == k2


def test_recommend_move_different_depth_different_key():
    # Regression: depth is part of the dedup key so the model can
    # legitimately re-verify the same move at a deeper search.
    board = chess.Board()
    k1 = _key("recommend_move", {"move": "Nf3", "depth": 20}, board)
    k2 = _key("recommend_move", {"move": "Nf3", "depth": 15}, board)
    assert k1 != k2


def test_recommend_move_depth_absent_and_default_distinct():
    # Depth omitted (server picks default) is wire-distinct from any
    # explicit depth value -- the model's intent differs and we should
    # not collapse.
    board = chess.Board()
    k_none = _key("recommend_move", {"move": "Nf3"}, board)
    k_20 = _key("recommend_move", {"move": "Nf3", "depth": 20}, board)
    assert k_none != k_20


def test_validate_move_unaffected_by_depth_key_field():
    # validate_move shares the normalizer but doesn't accept depth;
    # the normalizer must still cope with depth-less inputs cleanly.
    board = chess.Board()
    k1 = _key("validate_move", {"move": "Nf3"}, board)
    k2 = _key("validate_move", {"move": "g1f3"}, board)
    assert k1 == k2
    assert k1 is not None


def test_recommend_move_illegal_returns_none():
    board = chess.Board()
    assert _key("recommend_move", {"move": "Nf6"}, board) is None


def test_recommend_move_no_board_returns_none():
    assert _key("recommend_move", {"move": "Nf3"}, None) is None


# ---------- top_moves (_norm_top_moves) -------------------------------


def test_top_moves_all_parseable_collapse_uci_san():
    board = chess.Board()
    k1 = _key("top_moves", {"moves": ["Nf3", "Nc3"]}, board)
    k2 = _key("top_moves", {"moves": ["g1f3", "b1c3"]}, board)
    assert k1 == k2
    assert k1 is not None


# Mid-game position from the live repro: white to move, g4 is illegal
# as a SAN pawn push (g-pawn already advanced past g4).
_REPRO_FEN = "5r2/2qbbppk/rpn4n/2p2p1p/P1PpP3/3P1NPP/3BN1BK/R2Q1R2 w - - 0 20"


def test_top_moves_partial_unparseable_falls_back_to_raw():
    # Regression: live-observed two identical top_moves calls with one
    # candidate that doesn't parse on the live board (g4 illegal here)
    # both returned key=None, so dedup couldn't fire. Fallback ("raw"
    # kind) collapses them to the same key.
    board = chess.Board(_REPRO_FEN)
    same = {"moves": ["e2f4", "g4"]}
    k1 = _key("top_moves", same, board)
    k2 = _key("top_moves", dict(same), board)
    assert k1 is not None
    assert k1 == k2


def test_top_moves_canonical_and_raw_paths_never_collide():
    # All-parseable input should not produce the same key as an input
    # whose raw forms happen to match the UCIs -- the kind tag separates
    # the two paths.
    board = chess.Board(_REPRO_FEN)
    k_canonical = _key("top_moves", {"moves": ["e2f4"]}, board)
    k_raw = _key("top_moves", {"moves": ["e2f4", "g4"]}, board)
    assert k_canonical != k_raw


def test_top_moves_raw_path_normalizes_whitespace_case():
    board = chess.Board(_REPRO_FEN)
    k1 = _key("top_moves", {"moves": ["e2f4", "  G4 "]}, board)
    k2 = _key("top_moves", {"moves": ["e2f4", "g4"]}, board)
    assert k1 == k2


def test_top_moves_depth_in_raw_path_key():
    board = chess.Board(_REPRO_FEN)
    k1 = _key("top_moves", {"moves": ["e2f4", "g4"], "depth": 20}, board)
    k2 = _key("top_moves", {"moves": ["e2f4", "g4"], "depth": 15}, board)
    assert k1 != k2
