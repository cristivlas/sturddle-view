"""Match queue and matching logic for PGN reconciliation.

Holds two sets of records — pending dissolutions (waiting for the
PGN to flush) and recently-parsed PGN games (waiting for a pair to
dissolve) — and tries to pair them by exact UCI move-list equality
each time either side gets a new entry.

Match key: full UCI move list. At ``MIN_PLIES_FOR_MATCH = 12``
plies any realistic tournament has zero collisions, so equality
suffices — no hashing or fuzzy match needed.

Bounded state. Pending entries older than ``RECONCILE_TIMEOUT_S``
are dropped silently (consumers fall back to the original
``game_finished`` event with ``result="*"``). Both queues are
capped at ``_QUEUE_MAX``; oldest evicted on overflow.

See ``docs/pgn-reconciliation.md``.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field

from .pgn_tail import PgnGameRecord


log = logging.getLogger(__name__)


# Below this ply count we don't even attempt a match — opening-prefix
# collisions across parallel slots are common and an early-aborted
# game (book line + immediate resignation) is rare. Keeps false
# matches at zero in practice.
MIN_PLIES_FOR_MATCH = 12

# Pending entries older than this are silently dropped. PGN flush
# latency is dominated by fastchess's per-game write — we've measured
# sub-second; 60s is generous for a slow disk + heavy concurrency.
RECONCILE_TIMEOUT_S = 60.0

# Cap on each queue. Bounds memory regardless of tournament size.
# At 256 pending matches a tournament would need 256 simultaneous
# unreconciled games — way past anything realistic.
_QUEUE_MAX = 256


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
    """The product of a successful match: dissolution side + PGN side
    joined. Emitted as a ``game_reconciled`` event."""
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
    """Two-queue matcher; not thread-safe (called from the orchestrator's
    asyncio loop only). All public methods return the *new* matches
    produced by the call so the orchestrator can emit events.
    """

    def __init__(
        self,
        timeout_s: float = RECONCILE_TIMEOUT_S,
        min_plies: int = MIN_PLIES_FOR_MATCH,
        queue_max: int = _QUEUE_MAX,
    ) -> None:
        self._timeout_s = timeout_s
        self._min_plies = min_plies
        self._pending: deque[PendingMatch] = deque(maxlen=queue_max)
        # Each entry: (PgnGameRecord, enqueued_at). PGN side keeps a
        # ring buffer (oldest evicted) so a record arriving before its
        # dissolution still has a window to be matched. After
        # ``timeout_s`` it's safe to drop.
        self._pgn: deque[tuple[PgnGameRecord, float]] = deque(maxlen=queue_max)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def pgn_buffer_count(self) -> int:
        return len(self._pgn)

    def add_pending(self, entry: PendingMatch) -> ReconciledMatch | None:
        """Register a dissolved pair. If a queued PGN record matches,
        returns the reconciled record (and removes both halves);
        otherwise returns ``None`` and the entry sits until a record
        arrives or the timeout sweep evicts it.

        Skipped when the move list is shorter than the min-plies
        floor — caller is expected to gate on this too, but the
        guard belongs here as well so the rule is enforced in one
        place."""
        if len(entry.uci_moves) < self._min_plies:
            return None
        match = self._try_match_pending(entry)
        if match is not None:
            return match
        self._pending.append(entry)
        return None

    def add_pgn_record(self, record: PgnGameRecord) -> ReconciledMatch | None:
        """Register a PGN record. If a pending dissolution matches,
        returns the reconciled record; else parks the record in the
        ring buffer."""
        if len(record.uci_moves) < self._min_plies:
            # Below floor: don't bother buffering. We won't try to
            # reconcile anyway.
            return None
        match = self._try_match_pgn(record)
        if match is not None:
            return match
        self._pgn.append((record, time.monotonic()))
        return None

    def sweep(self) -> list[PendingMatch]:
        """Drop pending entries older than ``timeout_s``; same for the
        PGN ring buffer. Returns the dropped pending entries so the
        caller can log them or emit a ``timed out`` event if desired
        — current orchestrator implementation doesn't, matching the
        spec's "no worse than today" fallback."""
        now = time.monotonic()
        dropped: list[PendingMatch] = []
        while self._pending and (now - self._pending[0].enqueued_at) > self._timeout_s:
            dropped.append(self._pending.popleft())
        while self._pgn and (now - self._pgn[0][1]) > self._timeout_s:
            self._pgn.popleft()
        return dropped

    def clear(self) -> None:
        """Drop all state. Called on terminal runner events."""
        self._pending.clear()
        self._pgn.clear()

    # ----- internals --------------------------------------------------------

    def _try_match_pending(self, entry: PendingMatch) -> ReconciledMatch | None:
        """Pop the first PGN record whose move list matches. Linear
        scan; both queues are bounded so this is microseconds."""
        for i, (rec, _ts) in enumerate(self._pgn):
            if rec.uci_moves == entry.uci_moves:
                del self._pgn[i]
                return _join(entry, rec)
        return None

    def _try_match_pgn(self, record: PgnGameRecord) -> ReconciledMatch | None:
        for i, entry in enumerate(self._pending):
            if entry.uci_moves == record.uci_moves:
                del self._pending[i]
                return _join(entry, record)
        return None


def _join(entry: PendingMatch, record: PgnGameRecord) -> ReconciledMatch:
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
