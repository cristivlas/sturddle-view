"""Unit tests for the per-ply PGN eval parser.

Three formats supported, all normalized to white POV on output:
1. ``[%eval CP,DEPTH]`` -- integer cp + depth, STM POV (GBSelect/ChessBase).
2. ``[%eval VALUE]`` -- Lichess float pawns or ``#N``, white POV.
3. Cutechess/fastchess trailing ``<eval>/<depth>`` -- float pawns or M<n>, STM POV.
"""
from __future__ import annotations

from sturddle_view.play.import_position import _parse_pgn_eval


# --- Cutechess / fastchess: STM POV, float pawns, eval/depth time ---

def test_cutechess_white_positive_eval():
    # White just moved; +0.35 STM means white thinks it's good for white.
    s = _parse_pgn_eval("(Ng5) 0.35/19 17", mover_white=True)
    assert s == {"cp": 35, "depth": 19}


def test_cutechess_black_negative_eval_is_white_winning():
    # Black just moved with -1.35/28; STM POV means -1.35 = bad for black =
    # good for white. After flip, white POV cp = +135.
    s = _parse_pgn_eval("(exd5 ... blah) -1.35/28 31", mover_white=False)
    assert s == {"cp": 135, "depth": 28}


def test_cutechess_black_positive_eval_is_black_winning():
    # Black just moved with +0.50/26; STM POV means +0.50 = good for black =
    # bad for white. After flip, white POV cp = -50.
    s = _parse_pgn_eval("(h6 ...) 0.50/26 27", mover_white=False)
    assert s == {"cp": -50, "depth": 26}


def test_cutechess_mate_white_winning():
    # White moves with +M5 -- white mates in 5 (white POV).
    s = _parse_pgn_eval("(Qxh7+) +M5/30 1.4", mover_white=True)
    assert s == {"mate": 5, "depth": 30}


def test_cutechess_mate_black_to_move_being_mated():
    # Black just moved, eval -M3 STM means "I (black) get mated in 3".
    # After flip to white POV: white mates in 3.
    s = _parse_pgn_eval("(Kg8) -M3/22 0.8", mover_white=False)
    assert s == {"mate": 3, "depth": 22}


def test_cutechess_no_time_field():
    # Time field is optional in the regex.
    s = _parse_pgn_eval("(Ng5) 0.35/19", mover_white=True)
    assert s == {"cp": 35, "depth": 19}


# --- Bracket [%eval CP,DEPTH] -- GBSelect / ChessBase variant ---

def test_bracket_int_cp_white_positive():
    # White moved, +20 cp STM == good for white. White POV: +20.
    s = _parse_pgn_eval("[%eval 20,27] [%emt 00:00:36]", mover_white=True)
    assert s == {"cp": 20, "depth": 27}


def test_bracket_int_cp_black_negative_is_white_winning():
    # Real sample from kiwi.pgn line 21: black moved fxg4, eval -130,21.
    # STM POV: -130 = bad for black (= good for white). White POV: +130.
    s = _parse_pgn_eval("[%eval -130,21] [%emt 00:01:01]", mover_white=False)
    assert s == {"cp": 130, "depth": 21}


def test_bracket_int_cp_black_positive_is_black_winning():
    # Real sample: 48...Kxf6 [%eval 273,31] -- black just moved, +273
    # STM POV means good for black. White POV: -273.
    s = _parse_pgn_eval("(Kxf6) [%eval 273,31] [%emt 00:00:12]", mover_white=False)
    assert s == {"cp": -273, "depth": 31}


def test_bracket_mate_white():
    # White mates in 4: written as "#4" (positive STM = good for mover).
    s = _parse_pgn_eval("[%eval #4,52]", mover_white=True)
    assert s == {"mate": 4, "depth": 52}


def test_bracket_mate_black_being_mated():
    # Black moves and writes "-#14" -- "I (black) get mated in 14".
    # White POV: white mates in 14.
    s = _parse_pgn_eval("[%eval -#14,30]", mover_white=False)
    assert s == {"mate": 14, "depth": 30}


# --- Bracket [%eval VALUE] (no comma) -- Lichess style, white POV ---

def test_lichess_float_pawns_already_white_pov():
    # No comma -- Lichess style, value already white POV. Even though black
    # just moved, we don't flip.
    s = _parse_pgn_eval("[%eval -1.85]", mover_white=False)
    assert s == {"cp": -185}


def test_lichess_float_white_pov_positive():
    s = _parse_pgn_eval("[%eval 0.34]", mover_white=True)
    assert s == {"cp": 34}


def test_lichess_mate_white_pov():
    # White POV, no flip: white mates in 5.
    s = _parse_pgn_eval("[%eval #5]", mover_white=False)
    assert s == {"mate": 5}


# --- Edge cases ---

def test_no_eval_returns_none():
    assert _parse_pgn_eval(None, mover_white=True) is None
    assert _parse_pgn_eval("", mover_white=True) is None
    assert _parse_pgn_eval("(book)", mover_white=True) is None
    assert _parse_pgn_eval("just some prose", mover_white=True) is None


def test_garbled_eval_returns_none():
    # Bracket with garbage body.
    assert _parse_pgn_eval("[%eval abc]", mover_white=True) is None
    # Mate without a digit.
    assert _parse_pgn_eval("[%eval #]", mover_white=True) is None


def test_mixed_pv_paren_and_bracket_eval():
    # Real sample shape: cutechess paren PV followed by [%eval]. Bracket
    # wins (it appears first in our regex order).
    s = _parse_pgn_eval("(Bh4) [%eval 110,24] [%emt 00:00:25]", mover_white=True)
    assert s == {"cp": 110, "depth": 24}
