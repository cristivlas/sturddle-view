"""Per-tournament PGN delta tailer.

Polls ``games.pgn`` once per second; on growth, reads from the
last-known offset and parses newly-completed games into
``PgnGameRecord``s. Records are handed to a callback supplied at
construction; the orchestrator (slice 3) routes them into the
reconciliation match queue.

Cross-platform: ``Path.stat()`` returns ``st_mtime_ns`` and ``st_size``
on Linux + Windows alike. No filesystem-events dependency. Cost is
one syscall per second per running tournament; PGN parse is bounded
by the delta size, not the full file.

PGN truncation (``size < last_offset``) resets the offset to 0 and
reparses the file. fastchess uses ``-pgnout append=true`` so this is
defensive — it shouldn't happen in normal operation.

See ``docs/pgn-reconciliation.md``.
"""
from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import chess
import chess.pgn


log = logging.getLogger(__name__)


# Decisive result tags (anything else, including ``*``, is treated as
# "no result" and the game is skipped).
_DECISIVE_RESULTS = frozenset({"1-0", "0-1", "1/2-1/2"})

# Default poll interval. Overridable via the constructor for tests
# that want to drive the loop manually with ``poll_once``.
_DEFAULT_POLL_S = 1.0


@dataclass
class PgnGameRecord:
    """One completed game parsed out of the PGN delta.

    ``uci_moves`` is the move list converted from PGN SAN to UCI.
    ``game_n`` is the 1-based index across the PGN (matches fastchess's
    ``Started game N``); since we count games in PGN order, this is the
    cumulative game count up to and including this one.
    """
    white: str
    black: str
    result: str
    termination: str
    uci_moves: list[str]
    game_n: int
    round_tag: str


# Callback handed records as the tailer parses them. Async so the
# orchestrator can ``await`` event emission inside.
RecordCallback = Callable[[PgnGameRecord], Awaitable[None]]


class PgnTailer:
    """Owns one tail loop on a single PGN file.

    Lifecycle:

    - ``start()`` spawns the poll task. Idempotent — calling twice is
      a no-op (logs a warning).
    - ``stop()`` cancels the task and awaits its exit. Idempotent.
    - ``poll_once()`` runs one tail-and-parse pass synchronously.
      Tests use it to drive the loop deterministically; the real
      tail loop calls it once per ``poll_interval``.
    """

    def __init__(
        self,
        pgn_path: Path,
        on_record: RecordCallback,
        poll_interval: float = _DEFAULT_POLL_S,
    ) -> None:
        self._path = pgn_path
        self._on_record = on_record
        self._poll_interval = poll_interval
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        # Last byte we've finished parsing through. Survives across
        # poll passes; reset to 0 on truncation detection.
        self._offset = 0
        # Cumulative completed-game counter. Stamped on each record so
        # consumers can correlate with fastchess's ``Started game N``
        # line ordering. Reset alongside ``_offset`` on truncation.
        self._game_n = 0
        # Cached stat() result; the inner work is skipped when neither
        # mtime nor size has changed since the last pass.
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
        # One immediate pass so a tournament with a pre-existing PGN
        # (resume) gets parsed without waiting for the first interval.
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
                # Never let a parse error kill the tailer; log and keep
                # polling. A malformed PGN tail should not break the
                # tournament.
                log.exception("PgnTailer poll failed for %s", self._path)

    async def poll_once(self) -> int:
        """Run one tail pass. Returns the number of newly-emitted
        records (0 if the file did not grow or did not contain
        any complete new games)."""
        try:
            st = self._path.stat()
        except FileNotFoundError:
            # Pre-creation window; orchestrator may start the tailer
            # before fastchess writes its first PGN line. No-op.
            return 0

        # Truncation guard: defensive — fastchess always appends, but
        # tests, manual edits, or a future runner could shrink the file.
        if st.st_size < self._offset:
            log.info(
                "PgnTailer: %s shrank (size=%d offset=%d); resetting",
                self._path, st.st_size, self._offset,
            )
            self._offset = 0
            self._game_n = 0

        # Fast skip: nothing changed since the last pass. Cheaper than
        # opening the file even when no new games landed.
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

        records, new_offset = self._parse_delta(self._offset, st.st_size)
        if not records:
            # Delta exists but contained no completed game (e.g.
            # fastchess flushed a header block but the moves haven't
            # been written yet). Leave _offset at the last *complete*
            # game boundary so we re-read the in-flight bytes next pass.
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
        """Read bytes ``[start, end)`` from the PGN, parse complete
        games, and return ``(records, new_offset)``. ``new_offset`` is
        the byte position after the last complete game we parsed; any
        in-flight trailing bytes are deliberately left for next pass.
        """
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
        # Byte offset (relative to ``start``) of the end of the last
        # successfully-parsed game. ``StringIO.tell()`` is a *character*
        # offset; we re-encode the consumed prefix to get a byte count
        # that lines up with file offsets for non-ASCII headers.
        last_complete_bytes = 0

        while True:
            try:
                game = chess.pgn.read_game(f)
            except Exception:
                # Malformed game in the delta. Stop here; we'll retry
                # the same byte range next pass once more bytes land.
                log.exception("PgnTailer read_game raised")
                break
            if game is None:
                break
            result = game.headers.get("Result", "*")
            if result not in _DECISIVE_RESULTS:
                # `*` = unfinished. fastchess writes the final result
                # tag *with* the rest of the game (atomic per-game
                # append), so an unfinished tag here means we're
                # looking at trailing in-flight bytes. Leave the offset
                # at the last complete game so we re-read on next pass.
                break

            uci_moves: list[str] = []
            board = game.board()
            try:
                for node in game.mainline():
                    uci_moves.append(node.move.uci())
                    board.push(node.move)
            except (ValueError, chess.IllegalMoveError, chess.InvalidMoveError):
                log.warning(
                    "PgnTailer: illegal move in PGN game_n≈%d; skipping",
                    self._game_n + len(records) + 1,
                )
                # Skip the bad game but advance past it so we don't
                # re-parse forever.
                last_complete_bytes = len(text[: f.tell()].encode("utf-8"))
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
            last_complete_bytes = len(text[: f.tell()].encode("utf-8"))

        return records, start + last_complete_bytes
