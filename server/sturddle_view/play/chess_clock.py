"""ChessClock: tc, per-side time, per-ply snapshots, pause/resume, flag check.

Owns clock state only. Mode (paused/viewing/editing) lives on HVE; the
clock's pause/resume mirror the bake-elapsed step but do not track mode.
Pure: no asyncio, no board, no engine. monotonic source is injectable
so tests run with a fake clock.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

import chess


@dataclass
class TimeControl:
    initial_seconds: float
    increment_seconds: float = 0.0


class ChessClock:
    def __init__(
        self,
        tc: TimeControl,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.tc = tc
        self._now = monotonic
        self.white_time: float = tc.initial_seconds
        self.black_time: float = tc.initial_seconds
        # One snapshot per ply BEFORE the move at that ply. Take-back invariant.
        # None entries mark "no recorded clock at this ply" (e.g.
        # seeded plies inherited from a parent PGN with no timing).
        self.history: list[tuple[float | None, float | None]] = []
        self.turn_started_at: float | None = None

    def start_turn(self) -> None:
        self.turn_started_at = self._now()

    def stop_turn(self) -> None:
        self.turn_started_at = None

    def consume_turn(self, side_just_moved: chess.Color) -> None:
        """Debit elapsed from side_just_moved, credit increment, restart turn."""
        if self.turn_started_at is None:
            return
        elapsed = self._now() - self.turn_started_at
        if side_just_moved == chess.WHITE:
            self.white_time = max(0.0, self.white_time - elapsed) + self.tc.increment_seconds
        else:
            self.black_time = max(0.0, self.black_time - elapsed) + self.tc.increment_seconds
        self.turn_started_at = self._now()

    def remaining(
        self, side: chess.Color, *, stm: chess.Color, game_over: bool,
    ) -> float:
        """Live remaining for side; ticks down only when it's that side's turn."""
        base = self.white_time if side == chess.WHITE else self.black_time
        if (
            not game_over
            and stm == side
            and self.turn_started_at is not None
        ):
            base = max(0.0, base - (self._now() - self.turn_started_at))
        return base

    def pause(self, stm: chess.Color) -> None:
        """Bake elapsed into stm's clock without crediting increment; stop turn."""
        if self.turn_started_at is not None:
            elapsed = self._now() - self.turn_started_at
            if stm == chess.WHITE:
                self.white_time = max(0.0, self.white_time - elapsed)
            else:
                self.black_time = max(0.0, self.black_time - elapsed)
        self.turn_started_at = None

    def resume(self) -> None:
        self.turn_started_at = self._now()

    def snap_for_switch(self, stm: chess.Color) -> None:
        """Switch-sides: bake elapsed, no increment, restart turn immediately."""
        if self.turn_started_at is None:
            return
        elapsed = self._now() - self.turn_started_at
        if stm == chess.WHITE:
            self.white_time = max(0.0, self.white_time - elapsed)
        else:
            self.black_time = max(0.0, self.black_time - elapsed)
        self.turn_started_at = self._now()

    def append_snapshot(self) -> None:
        """Push (white, black) BEFORE consume_turn so take-back restores prior state."""
        self.history.append((self.white_time, self.black_time))

    def pop_snapshot(self) -> None:
        wt, bt = self.history.pop()
        # None markers in history indicate "no recorded clock" (e.g.
        # seeded plies inherited from a parent PGN with no per-ply
        # timing). Coerce to initial_seconds so the live clock stays
        # a float for downstream consumers.
        init = self.tc.initial_seconds
        self.white_time = wt if wt is not None else init
        self.black_time = bt if bt is not None else init

    def reseed_from_pgn(
        self,
        *,
        n_plies: int,
        seed_history: list[tuple[float | None, float | None]] | None,
        final_w: float | None,
        final_b: float | None,
    ) -> None:
        """Seed history + live clocks from imported PGN.

        Length mismatch on seed_history falls back to (initial, initial) per ply.
        Missing final clocks fall back to initial_seconds.
        """
        init = self.tc.initial_seconds
        if seed_history is not None and len(seed_history) == n_plies:
            self.history = [(w, b) for (w, b) in seed_history]
        else:
            # No / mismatched seed: leave the entries as None markers
            # so downstream build_pgn does NOT fabricate `0.0s` tokens
            # for plies the parent never timed (B9).
            self.history = [(None, None) for _ in range(n_plies)]
        self.white_time = final_w if final_w is not None else init
        self.black_time = final_b if final_b is not None else init

    def flag_check(self, *, stm: chess.Color, game_over: bool) -> chess.Color | None:
        """Return the flagged side (stm) when its remaining hits 0, else None.

        While paused (turn_started_at is None), no flag.
        """
        if self.turn_started_at is None or game_over:
            return None
        if self.remaining(stm, stm=stm, game_over=game_over) <= 0.0:
            return stm
        return None
