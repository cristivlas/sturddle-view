from sturddle_view.chess.results import (
    BLACK_WIN,
    WHITE_WIN,
    loser_result,
    winner_result,
)


def test_winner_result_white():
    assert winner_result(True) == WHITE_WIN


def test_winner_result_black():
    assert winner_result(False) == BLACK_WIN


def test_loser_result_white():
    assert loser_result(True) == BLACK_WIN


def test_loser_result_black():
    assert loser_result(False) == WHITE_WIN


def test_pgn_tail_and_pgn_stats_share_constant_identity():
    from sturddle_view.tournament import pgn_stats, pgn_tail
    assert pgn_tail.DECISIVE_RESULTS is pgn_stats.DECISIVE_RESULTS
