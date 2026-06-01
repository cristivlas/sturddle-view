"""Per-tournament PGN delta tailer.

Polls ``games.pgn`` once per second; on growth, reads from the
last-known offset and yields ``PgnGameRecord`` instances to a
callback. Cross-platform via ``Path.stat()``.
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

from ..chess.results import DECISIVE_RESULTS
from ..env_utils import env_float as _env_float

log = logging.getLogger(__name__)

# Same flag the reconcile queue uses; one opt-in for the whole subsystem.
_DEBUG = os.environ.get("SV_DEBUG_RECONCILE", "0") == "1"

# Operator knob (env-overridable). 1Hz is the default; lower for
# faster matching at the cost of more stat() calls.
PGN_TAIL_POLL_S = _env_float("SV_PGN_TAIL_POLL_S", 1.0)

# Cap bytes parsed per poll so each call stays bounded -- matters on stop,
# where the tailer task must finish promptly. Backlog drains across multiple
# polls (the run loop skips its sleep while more delta is pending).
_MAX_DELTA_BYTES_PER_POLL = 256 * 1024


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
        # True when the most recent poll_once consumed only part of the
        # available delta (cap hit). The run loop uses this to skip its
        # sleep so backlog drains promptly.
        self._has_more = False
        # One-shot guard: avoid logging the oversized-game warning on
        # every poll while the same giant game is in flight.
        self._warned_oversized = False
        # Finalize mode: set by finalize() to tell the run loop to exit
        # as soon as it has caught up to file EOF. Used at teardown
        # when the writer process is known to be gone -- no more data
        # will ever arrive, so a clean caught-up state is terminal.
        self._finalize = False

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
        # Reset the finalize flag so a tailer reused across runs
        # doesn't exit on its first poll because of a stale flag.
        self._finalize = False
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(), name=f"pgn-tail:{self._path.name}",
        )

    async def finalize(self) -> None:
        """Drain the PGN to EOF and exit. Caller is responsible for
        ensuring no more data will be written to the file (typically:
        the writer process has been reaped via ``proc.wait()``).

        Idempotent. Returns when the run loop has exited; if no run
        loop is active, runs a single ``poll_once`` to drain anything
        the gated tailer missed while paused, then returns."""
        if not self.is_running():
            # Tailer was paused (no subscribers). Drain in one shot
            # from the caller's context -- the gate kept us off so
            # nothing is in flight from the run loop.
            try:
                await self.poll_once()
                while self._has_more:
                    await self.poll_once()
            except Exception:
                log.error("PgnTailer finalize (paused) failed", exc_info=True)
            return
        self._finalize = True
        task, self._task = self._task, None
        await task

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
        # Pre-existing PGN content parsed without waiting (e.g. mid-game
        # subscriber attach after the runner has already produced games).
        try:
            await self.poll_once()
        except Exception:
            log.error("PgnTailer initial poll failed for %s", self._path, exc_info=True)
        if self._finalize and not self._has_more:
            return
        while not self._stop_event.is_set():
            if self._has_more:
                # Backlog pending from a capped poll: yield once so other
                # tasks run, then drain immediately without the poll sleep.
                await asyncio.sleep(0)
                if self._stop_event.is_set():
                    return
            else:
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
                log.error("PgnTailer poll failed for %s", self._path, exc_info=True)
            # In finalize mode the writer is gone -- once we've caught
            # up to the current EOF (``not _has_more``) no further data
            # will arrive, so exit cleanly.
            if self._finalize and not self._has_more:
                return

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
            self._has_more = False
            return 0

        # Cap how much we parse per call so the worker thread can't run
        # for many seconds during a stop sequence -- backlog drains across
        # subsequent polls (run loop skips its sleep when ``_has_more``).
        # Snap the cap down to a game boundary so chess.pgn never sees a
        # mid-game-truncated buffer (which logs noisy "illegal san" errors).
        end = min(st.st_size, self._offset + _MAX_DELTA_BYTES_PER_POLL)
        if end < st.st_size:
            end = await asyncio.to_thread(
                self._snap_to_boundary, self._offset, end, st.st_size,
            )
        delta_bytes = end - self._offset
        records, new_offset = await asyncio.to_thread(
            self._parse_delta, self._offset, end,
        )
        if _DEBUG:
            log.debug(
                "PgnTailer parsed delta=%dB games=%d new_offset=%d",
                delta_bytes, len(records), new_offset,
            )
        if not records:
            # Delta has no complete game yet -- in-flight bytes; retry next pass.
            self._has_more = end < st.st_size
            return 0

        emitted = 0
        for rec in records:
            self._game_n += 1
            rec.game_n = self._game_n
            try:
                await self._on_record(rec)
                emitted += 1
            except Exception:
                log.error(
                    "PgnTailer on_record callback raised for game_n=%d",
                    rec.game_n,
                    exc_info=True,
                )
        self._offset = new_offset
        self._has_more = self._offset < st.st_size
        return emitted

    def _snap_to_boundary(self, start: int, end: int, file_size: int) -> int:
        """Return ``end' <= file_size`` aligned to the last PGN game
        boundary in ``[start, end)``. PGN games are separated by
        ``\\n\\n[`` (blank line then a new tag block); cutting there
        leaves ``read_game`` with only complete games to parse.

        Fallback when no boundary is found in the window: return
        ``file_size`` (uncap this poll). Otherwise we'd repeatedly read
        the same mid-game-truncated window and never make progress.
        Triggers only on a single PGN game > the cap size, which doesn't
        happen in practice for chess tournaments -- log a warning if it
        ever does."""
        try:
            with self._path.open("rb") as f:
                f.seek(start)
                chunk = f.read(end - start)
        except OSError:
            return end
        sep = chunk.rfind(b"\n\n[")
        if sep < 0:
            if not self._warned_oversized:
                log.warning(
                    "PgnTailer: no game boundary in %d-byte window starting "
                    "at offset %d (single game > cap?); reading full delta",
                    end - start, start,
                )
                self._warned_oversized = True
            return file_size
        self._warned_oversized = False
        return start + sep + 2  # include the blank line; leave the `[` for next pass

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
            log.error("PgnTailer read failed for %s", self._path, exc_info=True)
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
                log.error("PgnTailer read_game raised", exc_info=True)
                break
            if game is None:
                break
            result = game.headers.get("Result", "*")
            if result not in DECISIVE_RESULTS:
                # `*` = in-flight bytes; retry this region next pass.
                break

            if game.errors:
                log.warning(
                    "PgnTailer: illegal move in PGN game_n~=%d; skipping",
                    self._game_n + len(records) + 1,
                )
                last_complete_bytes += advance_bytes()
                continue
            uci_moves: list[str] = [node.move.uci() for node in game.mainline()]

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
