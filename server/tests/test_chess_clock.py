"""R4 ChessClock unit tests -- red until play/chess_clock.py exists.

Pure tests: no asyncio, no engine, fake monotonic via injected clock.
The ChessClock owns tc, white/black times, history, turn_started_at;
HVE composes it. `paused` lives on HVE (mode flag) but the clock's
pause/resume mirror the bake-elapsed step.
"""
from __future__ import annotations

import chess
import pytest

from sturddle_view.play.chess_clock import ChessClock, TimeControl

INITIAL = 60.0
INCREMENT = 5.0
TC = TimeControl(INITIAL, INCREMENT)
TC_NO_INC = TimeControl(INITIAL, 0.0)


class FakeClock:
    """Injectable monotonic source. Advance with .tick(seconds)."""
    def __init__(self, t0: float = 1000.0):
        self.t = t0

    def __call__(self) -> float:
        return self.t

    def tick(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def now():
    return FakeClock()


def test_initial_state_both_sides_at_initial_seconds(now):
    c = ChessClock(TC, monotonic=now)
    assert c.white_time == INITIAL
    assert c.black_time == INITIAL
    assert c.history == []
    assert c.turn_started_at is None


def test_consume_turn_subtracts_elapsed_and_adds_increment(now):
    c = ChessClock(TC, monotonic=now)
    c.start_turn()
    now.tick(1.5)
    c.consume_turn(chess.WHITE)
    assert c.white_time == pytest.approx(INITIAL - 1.5 + INCREMENT)
    assert c.black_time == INITIAL


def test_remaining_for_thinking_side_ticks_down(now):
    c = ChessClock(TC_NO_INC, monotonic=now)
    c.start_turn()
    r1 = c.remaining(chess.WHITE, stm=chess.WHITE, game_over=False)
    now.tick(2.0)
    r2 = c.remaining(chess.WHITE, stm=chess.WHITE, game_over=False)
    assert r1 == INITIAL
    assert r2 == pytest.approx(INITIAL - 2.0)


def test_remaining_for_idle_side_constant(now):
    c = ChessClock(TC_NO_INC, monotonic=now)
    c.start_turn()
    r1 = c.remaining(chess.BLACK, stm=chess.WHITE, game_over=False)
    now.tick(2.0)
    r2 = c.remaining(chess.BLACK, stm=chess.WHITE, game_over=False)
    assert r1 == r2 == INITIAL


def test_pause_freezes_elapsed_no_increment(now):
    c = ChessClock(TC, monotonic=now)
    c.start_turn()
    now.tick(1.0)
    c.pause(chess.WHITE)
    frozen = c.white_time
    assert frozen == pytest.approx(INITIAL - 1.0)  # no increment
    now.tick(5.0)
    # Paused: remaining must not tick further.
    r = c.remaining(chess.WHITE, stm=chess.WHITE, game_over=False)
    assert r == frozen
    assert c.turn_started_at is None


def test_resume_resets_turn_start(now):
    c = ChessClock(TC, monotonic=now)
    c.start_turn()
    now.tick(1.0)
    c.pause(chess.WHITE)
    now.tick(10.0)
    c.resume()
    assert c.turn_started_at == now.t
    now.tick(0.5)
    r = c.remaining(chess.WHITE, stm=chess.WHITE, game_over=False)
    assert r == pytest.approx(c.white_time - 0.5)


def test_snapshot_invariant_one_per_ply(now):
    c = ChessClock(TC, monotonic=now)
    c.start_turn()
    for _ in range(4):
        c.append_snapshot()
        c.consume_turn(chess.WHITE)
    assert len(c.history) == 4


def test_pop_snapshot_restores_prior_clocks(now):
    c = ChessClock(TC_NO_INC, monotonic=now)
    c.start_turn()
    c.append_snapshot()
    w0, b0 = c.white_time, c.black_time
    now.tick(3.0)
    c.consume_turn(chess.WHITE)
    assert c.white_time != w0
    c.pop_snapshot()
    assert c.white_time == w0
    assert c.black_time == b0
    assert c.history == []


def test_switch_sides_snaps_elapsed_no_increment(now):
    c = ChessClock(TC, monotonic=now)
    c.start_turn()
    now.tick(2.0)
    c.snap_for_switch(chess.WHITE)
    assert c.white_time == pytest.approx(INITIAL - 2.0)  # no increment
    assert c.turn_started_at == now.t


def test_flag_check_returns_loser_when_remaining_zero(now):
    c = ChessClock(TC_NO_INC, monotonic=now)
    c.start_turn()
    now.tick(INITIAL + 1.0)
    loser = c.flag_check(stm=chess.WHITE, game_over=False)
    assert loser == chess.WHITE


def test_flag_check_no_loser_while_paused(now):
    c = ChessClock(TC_NO_INC, monotonic=now)
    c.start_turn()
    now.tick(INITIAL + 1.0)
    c.pause(chess.WHITE)
    assert c.flag_check(stm=chess.WHITE, game_over=False) is None


def test_seed_clock_history_pads_missing_entries_with_initial(now):
    c = ChessClock(TC, monotonic=now)
    seed = [(50.0, 55.0)]  # length-mismatch: 1 entry for 2 plies
    c.reseed_from_pgn(n_plies=2, seed_history=seed, final_w=None, final_b=None)
    assert len(c.history) == 2
    for w, b in c.history:
        assert w == INITIAL
        assert b == INITIAL
    assert c.white_time == INITIAL
    assert c.black_time == INITIAL
