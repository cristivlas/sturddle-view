from sturddle_view.chess.results import (
    loser_result,
    winner_result,
)


def test_winner_result_white():
    assert winner_result(True) == "1-0"


def test_winner_result_black():
    assert winner_result(False) == "0-1"


def test_loser_result_white():
    assert loser_result(True) == "0-1"


def test_loser_result_black():
    assert loser_result(False) == "1-0"


def test_pgn_tail_and_pgn_stats_share_constant_identity():
    from sturddle_view.tournament import pgn_stats, pgn_tail
    assert pgn_tail.DECISIVE_RESULTS is pgn_stats.DECISIVE_RESULTS
    assert pgn_tail.DECISIVE_RESULTS == frozenset({"1-0", "0-1", "1/2-1/2"})
