"""Slice 9b: pair_index — proxy → game correlation via UCI position lines."""
from __future__ import annotations

from sturddle_view.tournament.pair_index import (
    PairIndex,
    parent_key,
    parse_position_line,
)


# ---------------------------------------------------------------------------
# parse_position_line
# ---------------------------------------------------------------------------


def test_parse_startpos_no_moves():
    assert parse_position_line("position startpos") == ("startpos|", 0)


def test_parse_startpos_with_moves():
    key, ply = parse_position_line("position startpos moves e2e4 c7c5 g1f3")
    assert ply == 3
    assert key == "startpos|e2e4 c7c5 g1f3"


def test_parse_with_trailing_newline():
    assert parse_position_line("position startpos\n") == ("startpos|", 0)


def test_parse_fen_start():
    line = "position fen rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1 moves e2e4"
    key, ply = parse_position_line(line)
    assert ply == 1
    assert key == "fen rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1|e2e4"


def test_parse_non_position_line_returns_none():
    assert parse_position_line("ucinewgame") is None
    assert parse_position_line("go wtime 5000 btime 5000") is None
    assert parse_position_line("info depth 1 score cp 30") is None
    assert parse_position_line("bestmove e2e4") is None
    assert parse_position_line("") is None
    assert parse_position_line("position") is None


# ---------------------------------------------------------------------------
# parent_key
# ---------------------------------------------------------------------------


def test_parent_key_with_one_move():
    assert parent_key("startpos|e2e4") == "startpos|"


def test_parent_key_with_three_moves():
    assert parent_key("startpos|e2e4 c7c5 g1f3") == "startpos|e2e4 c7c5"


def test_parent_key_at_root_is_none():
    assert parent_key("startpos|") is None


# ---------------------------------------------------------------------------
# PairIndex
# ---------------------------------------------------------------------------


def test_no_pairing_with_one_proxy():
    idx = PairIndex()
    assert idx.observe("white", "position startpos moves e2e4") is None
    assert idx.game_id_for("white") is None


def test_pairs_at_ply_0_when_both_at_startpos():
    """Right at game start, fastchess sends `position startpos` to both
    engines (no moves yet) before either has moved. That's the only
    moment they share an exact key."""
    idx = PairIndex()
    assert idx.observe("white", "position startpos") is None
    gid = idx.observe("black", "position startpos")
    assert gid is not None
    assert idx.game_id_for("white") == gid
    assert idx.game_id_for("black") == gid


def test_pairs_via_parent_key_after_first_move():
    """Realistic flow: white observes the empty position (ply 0), plays.
    Black then observes startpos with 1 move (ply 1). Black's parent
    key matches white's current key → pair."""
    idx = PairIndex()
    # White's first observation: position startpos, ply 0.
    assert idx.observe("white", "position startpos") is None
    # White plays e2e4. fastchess sends to black:
    gid = idx.observe("black", "position startpos moves e2e4")
    assert gid is not None
    assert idx.proxies_for(gid) == ("black", "white") or \
           idx.proxies_for(gid) == ("white", "black")


def test_pairs_mid_game_when_proxy_first_observed_late():
    """If we miss the early observations (proxy started late), we can
    still pair later: when one proxy reaches a key whose parent matches
    the other proxy's last-known key."""
    idx = PairIndex()
    # White most recently saw move list of length 4.
    assert idx.observe("white", "position startpos moves e2e4 c7c5 g1f3 d7d6") is None
    # Black plays b1c3, fastchess sends to white the next position with
    # white now to move at ply 6. But mid-handover, black's most recent
    # observation is "...d7d6 b1c3" (ply 5), parent = "...d7d6" (ply 4).
    gid = idx.observe("black", "position startpos moves e2e4 c7c5 g1f3 d7d6 b1c3")
    assert gid is not None


def test_idempotent_on_paired_proxies():
    idx = PairIndex()
    idx.observe("white", "position startpos")
    gid = idx.observe("black", "position startpos")
    assert gid is not None

    # Further observations don't re-pair or change game_id.
    assert idx.observe("white", "position startpos moves e2e4") is None
    assert idx.observe("black", "position startpos moves e2e4 c7c5") is None
    assert idx.game_id_for("white") == gid
    assert idx.game_id_for("black") == gid


def test_does_not_pair_unrelated_games_with_same_opening():
    """Two concurrent games starting from the same opening. As long as
    they diverge at any point, the index correctly disambiguates."""
    idx = PairIndex()
    # Game 1: white-A, black-A play e4 c5
    idx.observe("wA", "position startpos moves e2e4")
    idx.observe("bA", "position startpos moves e2e4 c7c5")
    # Game 2: white-B, black-B play e4 e5
    idx.observe("wB", "position startpos moves e2e4")
    idx.observe("bB", "position startpos moves e2e4 e7e5")

    # Pairs should form correctly: bA pairs with wA (parent matches),
    # bB pairs with wB.
    assert idx.game_id_for("wA") is not None
    assert idx.game_id_for("bA") is not None
    assert idx.game_id_for("wA") == idx.game_id_for("bA")
    assert idx.game_id_for("wB") is not None
    assert idx.game_id_for("bB") is not None
    assert idx.game_id_for("wB") == idx.game_id_for("bB")
    # Different games.
    assert idx.game_id_for("wA") != idx.game_id_for("wB")


def test_forget_proxy_unpaired():
    idx = PairIndex()
    idx.observe("white", "position startpos moves e2e4")
    idx.forget_proxy("white")
    # No leak; subsequent pairings work cleanly.
    assert idx.observe("black", "position startpos moves e2e4") is None


def test_forget_proxy_paired_clears_both_sides():
    idx = PairIndex()
    idx.observe("white", "position startpos")
    gid = idx.observe("black", "position startpos")
    assert gid is not None

    idx.forget_proxy("white")
    # Both sides cleared (the game is over).
    assert idx.game_id_for("white") is None
    assert idx.game_id_for("black") is None
    assert idx.proxies_for(gid) is None


def test_reset():
    idx = PairIndex()
    idx.observe("white", "position startpos")
    idx.observe("black", "position startpos")
    idx.reset()
    assert idx.all_games() == {}
    assert idx.game_id_for("white") is None
