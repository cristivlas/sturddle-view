"""Match queue + matching logic for PGN reconciliation.

Pairs dissolved on the orchestrator side and games parsed off the
PGN tailer are joined by UCI move-list comparison; see
``docs/pgn-reconciliation.md`` for design and edge cases.
"""
from __future__ import annotations

import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field

from .pgn_tail import PgnGameRecord


log = logging.getLogger(__name__)

# SV_DEBUG_RECONCILE=1 turns on per-record / per-match traces.
_DEBUG = os.environ.get("SV_DEBUG_RECONCILE", "0") == "1"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("ignoring non-numeric %s=%r; using default %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("ignoring non-numeric %s=%r; using default %s", name, raw, default)
        return default


# Min captured plies before we attempt a match -- guards against
# opening-prefix collisions across parallel slots.
MIN_PLIES_FOR_MATCH = 12

# Operator knobs (env-overridable). Defaults work for typical
# tournaments; bump under high concurrency or slow disk.
RECONCILE_TIMEOUT_S = _env_float("SV_RECONCILE_TIMEOUT_S", 60.0)
RECONCILE_LATE_WARNING_S = _env_float("SV_RECONCILE_LATE_WARNING_S", 5.0)
RECONCILE_QUEUE_MAX = _env_int("SV_RECONCILE_QUEUE_MAX", 256)

# Captured may overrun PGN by 1 ply when fastchess adjudicates after
# the engine has already emitted bestmove.
_MAX_CAPTURED_OVERRUN_PLIES = 1


def _moves_match(captured: list[str], pgn: list[str]) -> bool:
    """Compare captured (engine-side) and PGN move lists, walking
    back from the end. Captured is normally a prefix of PGN by
    1-2 plies (no follow-up ``position`` after the final
    ``bestmove``); may overrun PGN by 1 on adjudication."""
    n = len(captured)
    overrun = n - len(pgn)
    if overrun > _MAX_CAPTURED_OVERRUN_PLIES:
        return False
    cmp_end = n - 1 - max(0, overrun)
    for i in range(cmp_end, -1, -1):
        if captured[i] != pgn[i]:
            return False
    return True


@dataclass
class PendingMatch:
    """A dissolved pair waiting for its PGN counterpart."""
    pair_id: str
    white_proxy: str
    black_proxy: str
    white_engine: str | None
    black_engine: str | None
    uci_moves: list[str]
    enqueued_at: float = field(default_factory=time.monotonic)


@dataclass
class ReconciledMatch:
    """Successful match payload, emitted as ``game_reconciled``."""
    pair_id: str
    white_proxy: str
    black_proxy: str
    white_engine: str | None
    black_engine: str | None
    pgn_white: str
    pgn_black: str
    result: str
    termination: str
    game_n: int
    ply_count: int


class ReconciliationQueue:
    """Two-queue matcher. Not thread-safe -- orchestrator asyncio
    loop only. Public methods return any newly-formed match for the
    caller to emit."""

    def __init__(
        self,
        timeout_s: float = RECONCILE_TIMEOUT_S,
        min_plies: int = MIN_PLIES_FOR_MATCH,
        queue_max: int = RECONCILE_QUEUE_MAX,
    ) -> None:
        self._timeout_s = timeout_s
        self._min_plies = min_plies
        self._pending: deque[PendingMatch] = deque(maxlen=queue_max)
        self._pgn: deque[tuple[PgnGameRecord, float]] = deque(maxlen=queue_max)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def pgn_buffer_count(self) -> int:
        return len(self._pgn)

    def add_pending(self, entry: PendingMatch) -> ReconciledMatch | None:
        """Register a dissolved pair; return a match if a buffered
        PGN record matches, else park the entry."""
        if len(entry.uci_moves) < self._min_plies:
            return None
        if _DEBUG:
            log.debug(
                "reconcile pending pair=%s plies=%d white=%s black=%s",
                entry.pair_id[:8], len(entry.uci_moves),
                entry.white_engine, entry.black_engine,
            )
        match = self._try_match_pending(entry)
        if match is not None:
            return match
        self._pending.append(entry)
        return None

    def add_pgn_record(self, record: PgnGameRecord) -> ReconciledMatch | None:
        """Register a parsed PGN record; return a match if a pending
        dissolution matches, else park it in the ring buffer."""
        if len(record.uci_moves) < self._min_plies:
            return None
        if _DEBUG:
            log.debug(
                "reconcile pgn_record n=%d plies=%d white=%s black=%s result=%s",
                record.game_n, len(record.uci_moves),
                record.white, record.black, record.result,
            )
        match = self._try_match_pgn(record)
        if match is not None:
            return match
        self._pgn.append((record, time.monotonic()))
        return None

    def sweep(self) -> list[PendingMatch]:
        """Drop pending entries older than ``timeout_s`` (PGN never
        arrived -- likely a fastchess crash). PGN-side is not swept:
        idle end-of-slot pairs may wait until teardown. ``queue_max``
        bounds memory."""
        now = time.monotonic()
        dropped: list[PendingMatch] = []
        while self._pending and (now - self._pending[0].enqueued_at) > self._timeout_s:
            entry = self._pending.popleft()
            log.info(
                "reconcile timeout pair=%s plies=%d age=%.1fs",
                entry.pair_id[:8], len(entry.uci_moves),
                now - entry.enqueued_at,
            )
            dropped.append(entry)
        return dropped

    def clear(self) -> None:
        """Drop all state. Called on terminal runner events."""
        self._pending.clear()
        self._pgn.clear()

    # ----- internals --------------------------------------------------------

    def _try_match_pending(self, entry: PendingMatch) -> ReconciledMatch | None:
        return self._scan(
            container=self._pgn,
            moves_of=lambda item: item[0].uci_moves,
            matches=lambda item: _moves_match(entry.uci_moves, item[0].uci_moves),
            on_hit=lambda item: _join(entry, item[0]),
            query_plies=len(entry.uci_moves),
            miss_log=("reconcile miss (pending) pair=%s plies=%d closest_pgn_plies=%d",
                      entry.pair_id[:8]),
        )

    def _try_match_pgn(self, record: PgnGameRecord) -> ReconciledMatch | None:
        return self._scan(
            container=self._pending,
            moves_of=lambda item: item.uci_moves,
            matches=lambda item: _moves_match(item.uci_moves, record.uci_moves),
            on_hit=lambda item: _join(item, record),
            query_plies=len(record.uci_moves),
            miss_log=("reconcile miss (pgn) n=%d plies=%d closest_pending_plies=%d",
                      record.game_n),
        )

    def _scan(self, container, moves_of, matches, on_hit, query_plies, miss_log):
        """Linear scan of `container`; pop+join on first match. Logs
        the closest-by-length miss when SV_DEBUG_RECONCILE is on."""
        for i, item in enumerate(container):
            if matches(item):
                del container[i]
                return on_hit(item)
        if _DEBUG and container:
            closest = min(container, key=lambda x: abs(len(moves_of(x)) - query_plies))
            fmt, *prefix_args = miss_log
            log.debug(fmt, *prefix_args, query_plies, len(moves_of(closest)))
        return None


def _join(entry: PendingMatch, record: PgnGameRecord) -> ReconciledMatch:
    age = time.monotonic() - entry.enqueued_at
    if age >= RECONCILE_LATE_WARNING_S:
        log.info(
            "reconcile late pair=%s plies=%d age=%.1fs",
            entry.pair_id[:8], len(entry.uci_moves), age,
        )
    return ReconciledMatch(
        pair_id=entry.pair_id,
        white_proxy=entry.white_proxy,
        black_proxy=entry.black_proxy,
        white_engine=entry.white_engine,
        black_engine=entry.black_engine,
        pgn_white=record.white,
        pgn_black=record.black,
        result=record.result,
        termination=record.termination,
        game_n=record.game_n,
        ply_count=len(record.uci_moves),
    )
