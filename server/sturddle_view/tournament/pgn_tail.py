"""Per-tournament PGN delta tailer.

Polls ``games.pgn`` once per second; on growth, reads from the
last-known offset and yields ``PgnGameRecord`` instances to a
callback. Cross-platform via ``Path.stat()``. See
``docs/pgn-reconciliation.md``.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import chess
import chess.pgn


log = logging.getLogger(__name__)


_DECISIVE_RESULTS = frozenset({"1-0", "0-1", "1/2-1/2"})

# Same flag the reconcile queue uses; one opt-in for the whole subsystem.
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


# Operator knob (env-overridable). 1Hz is the default; lower for
# faster matching at the cost of more stat() calls.
PGN_TAIL_POLL_S = _env_float("SV_PGN_TAIL_POLL_S", 1.0)


@dataclass
class PgnGameRecord:
    """One completed game parsed out of the PGN delta.

    ``uci_moves`` is SAN-to-UCI converted; ``game_n`` is the 1-based
    cumulative count in PGN order (matches fastchess's ``Started
    game N``).
    """
    white: str
    black: str
    result: str
    termination: str
    uci_moves: list[str]
    game_n: int
    round_tag: str


RecordCallback = Callable[[PgnGameRecord], Awaitable[None]]


class PgnTailer:
    """Owns one tail loop on a single PGN file.

    ``start()`` spawns the poll task; ``stop()`` cancels it; both
    idempotent. ``poll_once()`` runs a single pass synchronously
    (used by tests).
    """

    def __init__(
        self,
        pgn_path: Path,
        on_record: RecordCallback,
        poll_interval: float = PGN_TAIL_POLL_S,
    ) -> None:
        self._path = pgn_path
        self._on_record = on_record
        self._poll_interval = poll_interval
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._offset = 0
        self._game_n = 0
        self._last_mtime_ns: int | None = None
        self._last_size: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def offset(self) -> int:
        return self._offset

    @property
    def game_n(self) -> int:
        return self._game_n

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_running():
            log.warning("PgnTailer already running for %s", self._path)
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(), name=f"pgn-tail:{self._path.name}",
        )

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        task, self._task = self._task, None
        if task is None:
            return
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:
            log.warning("PgnTailer task did not exit within 5s; cancelling")
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _run(self) -> None:
        assert self._stop_event is not None
        # Resume case: pre-existing PGN parsed without waiting.
        try:
            await self.poll_once()
        except Exception:
            log.exception("PgnTailer initial poll failed for %s", self._path)
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._poll_interval,
                )
                return  # stop requested
            except asyncio.TimeoutError:
                pass
            try:
                await self.poll_once()
            except Exception:
                # Parse error must not kill the tailer.
                log.exception("PgnTailer poll failed for %s", self._path)

    async def poll_once(self) -> int:
        """Run one tail pass; return number of records newly emitted."""
        try:
            st = self._path.stat()
        except FileNotFoundError:
            # Pre-creation window: tailer may start before fastchess writes.
            return 0

        # Truncation guard (defensive; fastchess always appends).
        if st.st_size < self._offset:
            log.info(
                "PgnTailer: %s shrank (size=%d offset=%d); resetting",
                self._path, st.st_size, self._offset,
            )
            self._offset = 0
            self._game_n = 0

        # Fast skip when nothing has changed since last pass.
        if (
            st.st_mtime_ns == self._last_mtime_ns
            and st.st_size == self._last_size
            and st.st_size == self._offset
        ):
            return 0
        self._last_mtime_ns = st.st_mtime_ns
        self._last_size = st.st_size

        if st.st_size == self._offset:
            return 0

        # Offload the synchronous parse + SAN->UCI replay to a thread
        # so a multi-MB delta can't stall the server's main loop.
        delta_bytes = st.st_size - self._offset
        records, new_offset = await asyncio.to_thread(
            self._parse_delta, self._offset, st.st_size,
        )
        if _DEBUG:
            log.debug(
                "PgnTailer parsed delta=%dB games=%d new_offset=%d",
                delta_bytes, len(records), new_offset,
            )
        if not records:
            # Delta has no complete game yet -- in-flight bytes; retry next pass.
            return 0

        emitted = 0
        for rec in records:
            self._game_n += 1
            rec.game_n = self._game_n
            try:
                await self._on_record(rec)
                emitted += 1
            except Exception:
                log.exception(
                    "PgnTailer on_record callback raised for game_n=%d",
                    rec.game_n,
                )
        self._offset = new_offset
        return emitted

    def _parse_delta(
        self, start: int, end: int,
    ) -> tuple[list[PgnGameRecord], int]:
        """Parse complete games from byte range ``[start, end)``.
        Returns ``(records, new_offset)``; in-flight trailing bytes
        are left for the next pass."""
        try:
            with self._path.open("rb") as f:
                f.seek(start)
                blob = f.read(end - start)
        except OSError:
            log.exception("PgnTailer read failed for %s", self._path)
            return [], start

        text = blob.decode("utf-8", errors="replace")
        f = io.StringIO(text)
        records: list[PgnGameRecord] = []
        # Running byte counter so we don't re-encode the consumed
        # prefix per game (was O(N^2) on a multi-MB delta).
        prev_char_pos = 0
        last_complete_bytes = 0

        def advance_bytes() -> int:
            nonlocal prev_char_pos
            cur = f.tell()
            n = len(text[prev_char_pos:cur].encode("utf-8"))
            prev_char_pos = cur
            return n

        while True:
            try:
                game = chess.pgn.read_game(f)
            except Exception:
                log.exception("PgnTailer read_game raised")
                break
            if game is None:
                break
            result = game.headers.get("Result", "*")
            if result not in _DECISIVE_RESULTS:
                # `*` = in-flight bytes; retry this region next pass.
                break

            uci_moves: list[str] = []
            board = game.board()
            try:
                for node in game.mainline():
                    uci_moves.append(node.move.uci())
                    board.push(node.move)
            except (ValueError, chess.IllegalMoveError, chess.InvalidMoveError):
                log.warning(
                    "PgnTailer: illegal move in PGN game_n~=%d; skipping",
                    self._game_n + len(records) + 1,
                )
                last_complete_bytes += advance_bytes()
                continue

            records.append(PgnGameRecord(
                white=game.headers.get("White", "?"),
                black=game.headers.get("Black", "?"),
                result=result,
                termination=game.headers.get("Termination", ""),
                uci_moves=uci_moves,
                game_n=0,  # filled in by the caller (cumulative)
                round_tag=game.headers.get("Round", ""),
            ))
            last_complete_bytes += advance_bytes()

        return records, start + last_complete_bytes
