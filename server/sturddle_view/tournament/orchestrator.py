"""Tournament orchestrator -- composes ``TournamentStore`` and a ``Runner``.

Owns:
- Single-active-tournament invariant (the store can't tell stale ``running``
  on disk from a real live process; the orchestrator can).
- Startup reconciliation: persisted ``running`` is reset to ``stopped`` on boot.
- Wiring runner events to the store and broadcast tap.
- Per-tournament proxy bookkeeping (engine names, subscribers, broadcast
  secret). Single-side observation only -- pair detection deferred.

Web-agnostic: takes ids + a broadcast callback, so the CLI wrapper can reuse it.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import secrets
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Awaitable, Callable

import chess

from .pgn_reconcile import (
    PendingMatch,
    ReconciledMatch,
    ReconciliationQueue,
)
from .pgn_stats import rewrite_drop_partial_pairs
from .pgn_tail import PgnGameRecord, PgnTailer
from .rescheck import RescheckError, check_template
from .runner import RunSpec, Runner
from .uci_parse import parse_uci_line

if TYPE_CHECKING:
    from ..config import Settings
from .store import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_STOPPED,
    Tournament,
    TournamentStore,
)


log = logging.getLogger(__name__)


# Broadcast events the orchestrator emits upstream. Mirrors the runner's
# event vocabulary plus a ``status_change`` event the WebSocket layer
# uses to refresh the per-row status badge.
BroadcastCallback = Callable[[str, dict], Awaitable[None]]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("ignoring non-numeric %s=%r; using default %s", name, raw, default)
        return default


# Per-tournament event history depth. Big enough to cover all the
# fastchess startup chatter (engine init, opening probes) plus a few
# completed games, so a workspace opened mid-tournament still gets
# a useful tail. Operator knob: bump if late-joiners observe
# evicted game_finished/game_reconciled rows in the event log.
EVENT_HISTORY_MAX = _env_int("SV_EVENT_HISTORY_MAX", 200)


# Two engines of one game share a FEN at the rendezvous: the thinker
# (registered via ``position``) and the waiter (registered via
# ``bestmove`` at the resulting FEN). ``info`` from the thinker fans
# out to the waiter's subscribers -- that's the opponent's PV arrow.
_DEBUG_PAIRING   = os.environ.get("SV_DEBUG_PAIRING",   "0") == "1"


def _opposite_side(side: str) -> str:
    return "black" if side == "white" else "white"


# Result/termination on the `game_finished` event. Pair dissolution is
# the sole game-end trigger; we don't parse fastchess stdout for game
# results anymore (correlation under concurrency was unreliable). The
# fields stay in the schema so a future implementation can populate
# them via a different signal without breaking consumers.
_RESULT_UNKNOWN = "*"
_TERMINATION_UNKNOWN = "unknown"


def _fanout(subs: "set[CoalescingQueue]", payload: dict) -> None:
    """Route a payload to all subscribers. `info` lines coalesce per
    proxy (latest wins, flushed on timer or on next non-info); other
    lines drain pending infos first, then enqueue with eviction-as-
    fallback so terminal frames can't be silently lost."""
    is_info = isinstance(payload.get("line"), str) and payload["line"].lstrip().startswith("info ")
    for q in list(subs):
        if is_info:
            q.put_info(payload)
        else:
            q.put_other(payload)


# Time-based info coalescing window. Slow TCs (engines emit info every
# few hundred ms) see every info because the timer flushes between
# arrivals. Fast TCs (sub-100ms info cadence) collapse to the latest,
# bounding queue pressure.
_INFO_COALESCE_MS = 100


class CoalescingQueue:
    """Per-subscriber wrapper around ``asyncio.Queue`` with per-proxy
    info coalescing. Non-info events flush pending info first to
    preserve order. The terminal sentinel uses ``put_sentinel`` which
    evicts oldest on QueueFull so it always lands."""

    __slots__ = ("_q", "_slots", "_timers", "_loop")

    def __init__(self, maxsize: int) -> None:
        self._q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._slots: dict[str, dict] = {}
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        return self._loop

    def _try_put(self, payload: dict) -> None:
        try:
            self._q.put_nowait(payload)
        except asyncio.QueueFull:
            pass

    def put_info(self, payload: dict) -> None:
        proxy_id = payload.get("proxy_id", "")
        self._slots[proxy_id] = payload
        if proxy_id in self._timers:
            return
        loop = self._ensure_loop()
        self._timers[proxy_id] = loop.call_later(
            _INFO_COALESCE_MS / 1000, self._flush_slot, proxy_id,
        )

    def _flush_slot(self, proxy_id: str) -> None:
        self._timers.pop(proxy_id, None)
        held = self._slots.pop(proxy_id, None)
        if held is not None:
            self._try_put(held)

    def _flush_all_slots(self) -> None:
        for proxy_id in list(self._slots.keys()):
            timer = self._timers.pop(proxy_id, None)
            if timer is not None:
                timer.cancel()
            held = self._slots.pop(proxy_id)
            self._try_put(held)

    def put_other(self, payload: dict) -> None:
        # Flush ALL pending infos first (across both own + paired
        # engines) so a non-info event like position/bestmove can't be
        # overtaken by a stale info that's still sitting in another
        # proxy's coalescing slot. See bug: stale paired-info arrows
        # redrawn after the board updated.
        self._flush_all_slots()
        self._try_put(payload)

    def put_sentinel(self, payload: dict) -> None:
        # Terminal frame: flush all pending, then enqueue with eviction
        # so the sentinel always lands.
        self._flush_all_slots()
        try:
            self._q.put_nowait(payload)
        except asyncio.QueueFull:
            try:
                self._q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    async def get(self) -> dict:
        return await self._q.get()

    def get_nowait(self) -> dict:
        return self._q.get_nowait()

    def empty(self) -> bool:
        return self._q.empty()

    def cancel_timers(self) -> None:
        for t in self._timers.values():
            t.cancel()
        self._timers.clear()
        self._slots.clear()


def wrap_event_for_bus(kind: str, payload: dict) -> dict:
    """Map an orchestrator raw event to its EventBus / WS shape.

    Used by both the live broadcast path (in ``app.py``) and the
    history-replay REST endpoint so the two stay in lock-step. The
    workspace consumes both in a single shape: ``tournament_status``
    or ``tournament_update`` with ``payload.kind`` carrying the
    runner's original event kind.
    """
    if kind == "status_change":
        return {"kind": "tournament_status", "payload": payload}
    return {"kind": "tournament_update", "payload": {"kind": kind, **payload}}


class TournamentBusyError(Exception):
    """Raised when ``start`` is called while another tournament is running."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass
class OrchestratorConfig:
    """Composition wiring decided at construction. Kept tiny on purpose --
    the orchestrator is a thin coordinator, not a god object."""
    store: TournamentStore
    runner: Runner


class Orchestrator:
    """Single-active coordinator over a ``TournamentStore`` + a ``Runner``."""

    def __init__(self, store: TournamentStore, runner: Runner) -> None:
        self._store = store
        self._runner = runner
        self._active_id: str | None = None
        self._broadcast: BroadcastCallback | None = None

        # Slice 9b: live observation pipeline. WS subscribers attach
        # per-proxy and receive that engine's UCI line stream.
        self._proxy_subscribers: dict[str, set[CoalescingQueue]] = {}
        # Display name reported by each proxy on session start. Used to
        # label rows / buttons in the workspace UI. Cleared on session
        # end and on tournament teardown.
        self._proxy_engine_names: dict[str, str] = {}
        # Per-proxy snapshot of the most-recent stateful UCI lines:
        # ``position`` (current board), ``go`` (current clocks), and
        # ``info`` (current eval/depth/PV). Replayed to a subscriber on
        # connect so a window opened mid-game gets an instant snapshot
        # of the engine's state instead of waiting for the next event
        # (which under long time controls can be >=10s away).
        self._proxy_snapshot: dict[str, dict[str, str]] = {}
        # Pairing detection. Map from FEN -> list of ``(proxy_id, color)``
        # tuples. The thinker registers via ``position`` at F (its own
        # color); the waiter registers via ``bestmove`` at F-after-m
        # (still its own color -- engine identity, not side-to-move).
        # Two engines of one game share a FEN with opposite colors.
        self._pairing_map: dict[str, list[tuple[str, str]]] = {}
        # ``(fen, my_color)``. my_color is the engine's color in the
        # current game (identity), not side-to-move at fen -- that's
        # what makes the rendezvous lookup distinguish thinker from
        # waiter (same fen, opposite colors).
        self._pairing_state: dict[str, tuple[str, str] | None] = {}
        # Locked from side-to-move on the first ``position`` after
        # ``ucinewgame`` (that's the engine's first turn).
        self._pairing_color: dict[str, str | None] = {}
        # Derived from _pairing_map: one frozenset per FEN bucket with >=2 entries.
        # Diffed on each update to detect new pairings and orphaned proxies.
        self._current_groups: set[frozenset] = set()
        # Game-level confirmed pairs: proxy_id -> peer_proxy_id (bidirectional).
        # Added when a new unambiguous (size-2) group first appears; removed
        # when a proxy becomes orphaned (absent from _pairing_state).
        self._confirmed_pairs: dict[str, str] = {}
        # Stable game identity for each confirmed pair. frozenset(pid_a, pid_b) -> uuid str.
        # A new UUID is minted at confirmation time so the client can key windows
        # to game identity rather than proxy identity (proxies can re-pair).
        self._pair_ids: dict[frozenset, str] = {}
        # Reverse: pair_id -> frozenset(pid_a, pid_b). Used for snapshot replay.
        self._pair_proxies: dict[str, frozenset] = {}
        # White proxy_id captured at confirmation time. Dissolution may
        # run after one side has unregistered (ucinewgame clears its
        # _pairing_state), so we can't rely on _pairing_state to recover
        # orientation at dissolve time.
        self._pair_white: dict[str, str] = {}
        # WS subscribers keyed by pair_id. Same lifecycle as proxy subscribers
        # but scoped to the game: sentinel sent when the pair is dissolved.
        self._game_subscribers: dict[str, set[CoalescingQueue]] = {}
        # UCI move list per confirmed pair, longest-prefix-extension
        # wins; handed to the reconciliation queue at dissolution.
        self._pair_moves: dict[str, list[str]] = {}
        self._pgn_tailer: PgnTailer | None = None
        self._reconcile_queue = ReconciliationQueue()
        # Per-tournament secret embedded in the proxy --broadcast-url so
        # only proxies belonging to the active tournament can post.
        # Cleared on tournament termination.
        self._proxy_secret: str | None = None
        # Broadcast URL the proxy POSTs to. Set by the FastAPI app on
        # construction; ``None`` means "no proxy wrapping" (tests, CLI).
        self._proxy_broadcast_url: str | None = None
        # Optional reference to the live Settings instance. Read at
        # ``start()`` time so per-tournament runs see the current values
        # (user may have edited the Defaults tab between starts).
        self._settings: Settings | None = None
        # Per-tournament ring buffer of recent events. Replayed by the
        # REST endpoint to a workspace opened after restart so the user
        # sees the fastchess startup chatter instead of an empty log.
        # Capture happens in ``_emit`` regardless of subscribers.
        self._event_history: dict[str, deque[dict]] = {}
        # Monotonic per-orchestrator sequence stamped on every emitted
        # event. Lets the workspace dedupe between REST backfill and the
        # WS firehose when both deliver the same event around open time.
        self._event_seq: int = 0

    def _reset_pairing_state(self) -> None:
        """Drop all per-tournament pairing/observation state. Used on
        start rollback and on terminal runner events."""
        self._proxy_engine_names.clear()
        self._proxy_snapshot.clear()
        self._pairing_map.clear()
        self._pairing_state.clear()
        self._pairing_color.clear()
        self._current_groups = set()
        self._confirmed_pairs.clear()
        self._pair_ids.clear()
        self._pair_proxies.clear()
        self._pair_white.clear()
        self._game_subscribers.clear()
        self._pair_moves.clear()
        self._reconcile_queue.clear()

    def set_proxy_broadcast_url(self, url: str | None) -> None:
        """Configure the URL proxies POST to. The orchestrator passes
        this into the ``RunSpec`` when starting fastchess so each
        engine is wrapped in the proxy script."""
        self._proxy_broadcast_url = url

    def set_settings(self, settings: Settings | None) -> None:
        """Provide the live ``Settings`` instance. Used only as a fallback
        for tournaments created before ``engine_defaults`` was snapshotted
        into ``state.json``; new tournaments use their frozen snapshot."""
        self._settings = settings

    # ---- public surface ----------------------------------------------------

    def set_broadcast(self, broadcast: BroadcastCallback | None) -> None:
        """Install (or clear) the upstream broadcast callback. Used by the
        REST/WS layer in Slice 5; tests pass ``None`` and inspect the
        store directly."""
        self._broadcast = broadcast

    def active_id(self) -> str | None:
        """Id of the currently-running tournament, or ``None``."""
        return self._active_id

    async def start(self, tournament_id: str) -> Tournament:
        """Start the given tournament. Raises:

          - ``TournamentNotFoundError`` if the id does not exist.
          - ``TournamentBusyError`` if another tournament is running.
        """
        if self._active_id is not None or self._runner.is_running():
            try:
                active = self._store.get(self._active_id) if self._active_id else None
            except Exception:
                active = None
            if active:
                raise TournamentBusyError(
                    f'another tournament is running: "{active.name}" ({active.id})'
                )
            raise TournamentBusyError("another tournament is running")

        # Confirms existence and gets the frozen template.
        t = self._store.get(tournament_id)

        # Prefer the tournament's frozen snapshot; for tournaments created
        # before snapshotting was introduced, fall back to live Settings so
        # existing dirs still launch.
        s = self._settings
        ed = t.engine_defaults or {}
        def _ed(key: str):
            if key in ed:
                return ed[key]
            return getattr(s, f"engine_default_{key}", None)

        # Resource recheck -- values were resolved + folded into the
        # template by the client at create time; we re-verify against
        # the current host (covers stale tournaments started after the
        # machine specs changed, and direct-API misuse).
        # Failure surfaces like a runner_crash: STATUS_FAILED + last_error.
        try:
            check_template(t.template)
        except RescheckError as e:
            last_error = {
                "rc": None,
                "stderr_tail": [str(e)],
                "rescheck": {"reason": e.reason, **e.details},
                "at": _now(),
            }
            failed = self._store.update_status(
                t.id, STATUS_FAILED, stopped_at=_now(), last_error=last_error,
            )
            await self._emit_status(failed)
            raise

        # Defense in depth: previous terminal event clears state; this
        # is a no-op in the normal stop -> start (resume) flow but
        # guards against any leak from prior runs.
        self._reset_pairing_state()
        # Mark active *before* spawning so a concurrent ``start`` call
        # racing against this one is rejected by the busy check above.
        self._active_id = t.id
        self._proxy_secret = secrets.token_urlsafe(24)
        spec = RunSpec(
            tournament=t,
            binary_path=getattr(self._runner, "binary_path", "") or "",
            work_dir=self._store._dir(t.id),
            pgn_path=self._store.pgn_path(t.id),
            config_path=self._store.config_path(t.id),
            log_path=self._store.logs_dir(t.id) / "fastchess.log",
            proxy_broadcast_url=self._proxy_broadcast_url,
            proxy_secret=self._proxy_secret,
            engine_default_threads=_ed("threads"),
            engine_default_hash_mb=_ed("hash_mb"),
            engine_default_syzygy_path=_ed("syzygy_path"),
            engine_default_book_path=_ed("book_path"),
            engine_default_book_plies=_ed("book_plies"),
            engine_default_book_order=_ed("book_order"),
        )
        try:
            # On resume, drop any partial pairs from a prior interrupted
            # Pause so fastchess's first emitted stats are honest. Threaded
            # so a multi-MB scan can't stall the event loop.
            try:
                pgn_size = (
                    spec.pgn_path.stat().st_size
                    if spec.pgn_path.exists() else 0
                )
                if pgn_size > 0:
                    log.info(
                        "tournament %s: scanning PGN for partial pairs (%.1f MB)",
                        t.id, pgn_size / (1024 * 1024),
                    )
                t0 = time.monotonic()
                ts = datetime.now()
                # Paired mode: any tour with games_per_round != 1 (default
                # is 2 -- color-flipped pairs). Single-game tours have no
                # pair concept, so the rewrite is a no-op.
                games_per_round = (t.template or {}).get("games_per_round", 2)
                paired = games_per_round != 1
                dropped, _deltas = await asyncio.to_thread(
                    functools.partial(
                        rewrite_drop_partial_pairs,
                        spec.pgn_path,
                        spec.config_path,
                        ts,
                        paired=paired,
                    ),
                )
                elapsed = time.monotonic() - t0
                if dropped:
                    stamp = ts.strftime("%Y-%m-%dT%H-%M-%S")
                    log.info(
                        "tournament %s: rewrote PGN, dropped %d game(s) "
                        "(partial pairs + resume dups) in %.1fs; "
                        "backup at %s.%s.bak.gz",
                        t.id, dropped, elapsed, spec.pgn_path.name, stamp,
                    )
                elif pgn_size > 0:
                    log.info(
                        "tournament %s: PGN clean (no partial pairs) in %.1fs",
                        t.id, elapsed,
                    )
            except Exception:
                log.exception("partial-pair rewrite failed for %s", t.id)
            # Clear any prior last_error on (re)start -- the user has
            # acted on the diagnostic by retrying.
            updated = self._store.update_status(
                t.id, STATUS_RUNNING, started_at=_now(), last_error=None,
            )
            await self._emit_status(updated)
            await self._runner.start(spec, self._on_runner_event)
        except Exception:
            # If start() raised AFTER spawning the subprocess (e.g.
            # post-spawn assign_to_job or event-emit failure), the runner
            # is left "running" and will orphan the process unless we
            # stop it explicitly. stop() is idempotent for the
            # never-spawned case.
            try:
                await self._runner.stop()
            except Exception:
                log.exception("rollback: runner.stop() failed")
            # Roll back the active claim so a failed start doesn't lock
            # out the next attempt.
            self._active_id = None
            self._proxy_secret = None
            self._reset_pairing_state()
            self._store.update_status(t.id, STATUS_STOPPED, stopped_at=_now())
            raise

        # PGN tailer for reconciliation. Constructed here but its poll
        # loop is gated on _game_subscribers: started on first watcher,
        # stopped when the last watcher leaves. Saves 1Hz file I/O +
        # SAN->UCI parse when no one is watching individual games.
        self._pgn_tailer = PgnTailer(spec.pgn_path, self._on_pgn_record)

        return updated

    async def stop(self, tournament_id: str) -> Tournament:
        """Stop the given tournament if it's the active one. No-op if
        it isn't. Idempotent."""
        if self._active_id == tournament_id and self._runner.is_running():
            await self._runner.stop()
        # If it isn't the active one, nothing to do here; the store
        # status is already terminal.
        return self._store.get(tournament_id)

    def reconcile_on_startup(self) -> list[Tournament]:
        """Mark persisted ``running`` rows as ``failed`` with a synthetic
        last_error -- server died mid-tournament; can't claim a clean
        stop. Resume via Start (config.json still on disk)."""
        stale = self._store.find_by_status(STATUS_RUNNING)
        out: list[Tournament] = []
        for t in stale:
            last_error = {
                "rc": None,
                "stderr_tail": [
                    "Server was killed or crashed while this tournament was running. "
                    "fastchess and any engine processes have been reaped; press Start to resume."
                ],
                "at": _now(),
            }
            updated = self._store.update_status(
                t.id, STATUS_FAILED, stopped_at=_now(), last_error=last_error,
            )
            log.info(
                "reconcile: tournament %s was 'running' on disk; marking 'failed'",
                t.id,
            )
            out.append(updated)
        return out

    # ---- runner event sink -------------------------------------------------

    async def _on_runner_event(self, kind: str, payload: dict) -> None:
        """Receives events from the Runner.

        Maps:
          - ``done``         -> status=done, active_id cleared
          - ``stopped``      -> status=stopped, active_id cleared
          - ``runner_crash`` -> status=failed + last_error persisted,
                               active_id cleared (no Resume in Phase 1)
          - ``started``      -> no status change (we set RUNNING in start())
          - others           -> forwarded as-is to broadcast
        """
        active_id = self._active_id

        if kind in ("done", "stopped", "runner_crash"):
            if active_id is not None:
                if kind == "done":
                    terminal_status = STATUS_DONE
                elif kind == "runner_crash":
                    terminal_status = STATUS_FAILED
                else:
                    terminal_status = STATUS_STOPPED
                last_error = None
                if kind == "runner_crash":
                    last_error = {
                        "rc": payload.get("rc"),
                        "stderr_tail": payload.get("stderr_tail", []),
                        "at": _now(),
                    }
                try:
                    updated = self._store.update_status(
                        active_id, terminal_status, stopped_at=_now(),
                        last_error=last_error,
                    )
                    await self._emit_status(updated)
                except Exception:
                    log.exception("failed to persist terminal status for %s", active_id)
                finally:
                    self._proxy_secret = None
                    # Dissolve pairs first so their pending entries are
                    # parked in the reconcile queue. Then drive the
                    # tailer to EOF: it reads the remaining PGN, fires
                    # _on_pgn_record per game, which matches the parked
                    # entries and emits ``game_reconciled``. Fastchess
                    # has been reaped via proc.wait() so the file is
                    # complete and bounded -- no timeout needed.
                    await self._dissolve_all_pairs()
                    if self._pgn_tailer is not None:
                        try:
                            await self._pgn_tailer.finalize()
                        except Exception:
                            log.exception("PGN tailer finalize failed")
                        self._pgn_tailer = None
                    self._active_id = None
                    self._reset_pairing_state()
                    self._close_all_proxy_subscribers()

        # Always forward the runner event upstream -- UI consumers want
        # ``runner_crash`` etc. distinct from a plain status change.
        await self._emit(kind, {"tournament_id": active_id, **payload})

    async def _emit_status(self, t: Tournament) -> None:
        await self._emit("status_change", {
            "tournament_id": t.id,
            "status": t.status,
            "started_at": t.started_at,
            "stopped_at": t.stopped_at,
            "last_error": t.last_error,
        })

    async def _emit(self, kind: str, payload: dict) -> None:
        # Stamp + record before broadcasting so the history captures
        # events even when no WS subscriber is attached at the moment
        # of emit (workspace opened late, restart race, etc.).
        # ``_seq`` lets the workspace dedupe between live + backfill;
        # ``_ts`` is the server-side emission time used for display.
        self._event_seq += 1
        payload = {**payload, "_seq": self._event_seq, "_ts": _now()}
        tid = payload.get("tournament_id")
        if tid:
            hist = self._event_history.setdefault(
                tid, deque(maxlen=EVENT_HISTORY_MAX)
            )
            hist.append({"kind": kind, "payload": payload})
        if self._broadcast is None:
            return
        try:
            await self._broadcast(kind, payload)
        except Exception:
            log.exception("broadcast callback raised for %s", kind)

    def event_history(self, tournament_id: str) -> list[dict]:
        """Recent emitted events for a tournament, oldest first.

        Each item: ``{"kind": str, "payload": dict}`` where ``kind`` is
        the raw runner kind (``runner_log``, ``status_change``, ...) and
        ``payload`` carries ``_seq`` + ``_ts`` stamps.
        """
        return list(self._event_history.get(tournament_id, []))

    def clear_event_history(self, tournament_id: str) -> None:
        """Drop the buffered history for a tournament. Called by the
        DELETE endpoint so torn-down tournaments don't leak history."""
        self._event_history.pop(tournament_id, None)

    # ---- Slice 9b: live-observation pipeline -------------------------------

    def proxy_secret(self) -> str | None:
        """Per-tournament secret embedded in the proxy broadcast URL.
        ``None`` when no tournament is running."""
        return self._proxy_secret

    def active_proxies(self) -> list[dict]:
        """Snapshot of currently-active proxies (engine processes wrapped
        by the broadcast tap). Used by the API ``GET /api/tournaments/{id}``
        so a workspace opening mid-tournament can seed its Schedule rows
        even if it missed the forward-going ``proxy_started`` events.

        Each entry: ``{"proxy_id": str, "engine_name": str | None}``.
        Sorted by ``proxy_id`` for stable ordering across calls."""
        return [
            {"proxy_id": pid, "engine_name": name}
            for pid, name in sorted(self._proxy_engine_names.items())
        ]

    def active_pairings(self) -> list[dict]:
        """Snapshot of currently-confirmed proxy pairs. Same shape as the
        ``proxy_paired`` WS event so the workspace can seed ``livePairings``
        on mount without having to have caught all prior WS events.

        Each entry: ``{proxy_a, engine_a, side_a, proxy_b, engine_b, side_b}``.
        Deduped (bidirectional map -> one entry per pair)."""
        seen: set[frozenset] = set()
        out: list[dict] = []
        for pid_a, pid_b in self._confirmed_pairs.items():
            key = frozenset((pid_a, pid_b))
            if key in seen:
                continue
            seen.add(key)
            white_pid, black_pid = self._white_black_for_group(key)
            out.append({
                "pair_id":  self._pair_ids.get(key, ""),
                "proxy_a":  white_pid,
                "engine_a": self._proxy_engine_names.get(white_pid),
                "side_a":   "white",
                "proxy_b":  black_pid,
                "engine_b": self._proxy_engine_names.get(black_pid),
                "side_b":   "black",
            })
        return out

    def engine_name_for(self, proxy_id: str) -> str | None:
        """Display name reported by a proxy on its session start, or
        ``None`` if the proxy never announced (or has ended)."""
        return self._proxy_engine_names.get(proxy_id)

    def verify_proxy_secret(self, presented: str | None) -> bool:
        if self._proxy_secret is None or presented is None:
            return False
        # Constant-time comparison.
        import hmac
        return hmac.compare_digest(self._proxy_secret, presented)

    async def ingest_proxy_lines(
        self, proxy_id: str, lines: list[str]
    ) -> None:
        """Called by the ``/internal/proxy`` endpoint with a batch of
        UCI lines from one proxy. Updates the per-proxy snapshot
        (always -- independent of subscribers) and fans out to any
        subscribed WS clients. Drives the pairing map so that ``info``
        lines from the thinking engine also fan out to the
        opposite-color subscribers (the live opponent's view)."""
        snap = self._proxy_snapshot.setdefault(proxy_id, {})
        own_subs = self._proxy_subscribers.get(proxy_id)
        for line in lines:
            stripped = line.lstrip()
            parsed: dict | None = None
            paired_subs: set[CoalescingQueue] = set()
            thinking_side: str | None = None

            if stripped.startswith("position "):
                snap["position"] = line
                snap.pop("info", None)
                parsed = parse_uci_line(stripped)
                if parsed is not None and "fen" in parsed and "side_to_move" in parsed:
                    if self._pairing_color.get(proxy_id) is None:
                        self._pairing_color[proxy_id] = parsed["side_to_move"]
                    color = self._pairing_color[proxy_id]
                    if color is not None:
                        new_pairs, orphaned = self._pairing_register(proxy_id, parsed["fen"], color)
                        await self._emit_group_events(new_pairs, orphaned)
                self._update_pair_moves(proxy_id, parsed)
            elif stripped.startswith("ucinewgame"):
                # Dissolve confirmed pair directly: pair-FEN rendezvous
                # is transient, so orphan detection misses most exits.
                peer_before = self._confirmed_pairs.get(proxy_id)
                if peer_before is not None:
                    pair_id = self._pair_ids.get(
                        frozenset((proxy_id, peer_before)), ""
                    )
                    if pair_id:
                        await self._dissolve_pair(
                            pair_id, _RESULT_UNKNOWN, _TERMINATION_UNKNOWN,
                        )
                self._pairing_unregister(proxy_id)
                new_pairs, orphaned = self._recompute_groups()
                self._pairing_color[proxy_id] = None
                await self._emit_group_events(new_pairs, orphaned)
            elif stripped.startswith("go "):
                snap["go"] = line
            elif stripped.startswith("bestmove "):
                parsed = parse_uci_line(stripped)
                new_pairs, orphaned = self._pairing_apply_bestmove(proxy_id, parsed)
                await self._emit_group_events(new_pairs, orphaned)
            elif stripped.startswith("info "):
                if " score " in stripped or " pv " in stripped:
                    snap["info"] = line
                state = self._pairing_state.get(proxy_id)
                if state is not None:
                    fen, my_color = state
                    paired_subs = self._paired_subscribers(proxy_id, fen, my_color)
                    thinking_side = my_color

            if own_subs:
                payload: dict = {"proxy_id": proxy_id, "line": line}
                if parsed is not None:
                    payload["parsed"] = parsed
                _fanout(own_subs, payload)
            if paired_subs:
                payload = {
                    "proxy_id": proxy_id,
                    "line": line,
                    "paired": True,
                    "thinking_side": thinking_side,
                    "engine_name": self._proxy_engine_names.get(proxy_id),
                }
                _fanout(paired_subs, payload)
            # Fan out to game subscribers (keyed by pair_id, not proxy_id).
            peer = self._confirmed_pairs.get(proxy_id)
            if peer:
                pair_id = self._pair_ids.get(frozenset((proxy_id, peer)))
                if pair_id:
                    game_subs = self._game_subscribers.get(pair_id)
                    if game_subs:
                        state = self._pairing_state.get(proxy_id)
                        game_payload: dict = {
                            "proxy_id":     proxy_id,
                            "line":         line,
                            "thinking_side": state[1] if state else None,
                            "engine_name":  self._proxy_engine_names.get(proxy_id),
                        }
                        if parsed is not None:
                            game_payload["parsed"] = parsed
                        _fanout(game_subs, game_payload)

    def _recompute_groups(self) -> tuple[set[frozenset], set[str]]:
        """Derive the current set of pairing groups from ``_pairing_map``.

        A group is the frozenset of all proxy_ids sharing one FEN bucket
        (requires >= 2 entries). Updates ``_current_groups`` in place and
        returns ``(new_pairs, orphaned)``:

        - ``new_pairs``: unambiguous (size-2) groups that are newly confirmed
          this game -- callers should emit ``proxy_paired`` for each.
        - ``orphaned``: proxies that were in a now-removed group but are no
          longer registered at *any* FEN (i.e. absent from ``_pairing_state``).
          Between-move transitions keep the proxy registered at a new FEN, so
          they produce no orphans and no ``proxy_unpaired`` event.
        """
        new_groups: set[frozenset] = set()
        for bucket in self._pairing_map.values():
            if len(bucket) >= 2:
                new_groups.add(frozenset(pid for pid, _ in bucket))

        added   = new_groups - self._current_groups
        removed = self._current_groups - new_groups
        self._current_groups = new_groups

        new_pairs: set[frozenset] = set()
        for group in added:
            if len(group) == 2:
                pid_a, pid_b = tuple(group)
                state_a = self._pairing_state.get(pid_a)
                state_b = self._pairing_state.get(pid_b)
                name_a = self._proxy_engine_names.get(pid_a)
                name_b = self._proxy_engine_names.get(pid_b)
                # Require opposite colors and neither proxy already confirmed.
                # Reject same-engine-name pairs as phantoms from book-line
                # collisions (4-bucket decay leaving two same-engine proxies
                # that aren't actually playing each other in fastchess).
                # TODO: lift this when self-play is supported -- see spec
                # sec. "Self-play (deferred)". Self-play needs orchestrator-
                # level engine-name disambiguation before reaching fastchess.
                if (state_a and state_b and state_a[1] != state_b[1]
                        and pid_a not in self._confirmed_pairs
                        and pid_b not in self._confirmed_pairs
                        and name_a and name_b and name_a != name_b):
                    self._confirmed_pairs[pid_a] = pid_b
                    self._confirmed_pairs[pid_b] = pid_a
                    pair_id = str(uuid.uuid4())
                    self._pair_ids[group] = pair_id
                    self._pair_proxies[pair_id] = group
                    self._pair_white[pair_id] = (
                        pid_a if state_a[1] == "white" else pid_b
                    )
                    self._pair_moves[pair_id] = []
                    new_pairs.add(group)
                    if _DEBUG_PAIRING:
                        log.debug(
                            "pair confirmed tag=%s white=%s(%s) black=%s(%s) pairs=%d",
                            pair_id[:8],
                            self._proxy_engine_names.get(
                                pid_a if state_a[1] == "white" else pid_b, "?"),
                            (pid_a if state_a[1] == "white" else pid_b),
                            self._proxy_engine_names.get(
                                pid_b if state_a[1] == "white" else pid_a, "?"),
                            (pid_b if state_a[1] == "white" else pid_a),
                            len(self._pair_proxies),
                        )
                elif name_a and name_b and name_a == name_b and _DEBUG_PAIRING:
                    log.debug(
                        "pair candidate rejected (same engine name): "
                        "%s(%s) vs %s(%s) -- phantom from book-line collision",
                        name_a, pid_a, name_b, pid_b,
                    )
            elif _DEBUG_PAIRING:
                fen = (self._pairing_state.get(next(iter(group))) or ("?",))[0]
                log.debug(
                    "pairing: ambiguous size=%d fen=%.30s %s",
                    len(group),
                    fen,
                    [(p, self._proxy_engine_names.get(p, "?")) for p in group],
                )

        orphaned: set[str] = set()
        for group in removed:
            for pid in group:
                if pid not in self._pairing_state:
                    orphaned.add(pid)
        if orphaned and _DEBUG_PAIRING:
            log.debug(
                "pairing: orphaned %s",
                [(p, self._proxy_engine_names.get(p, "?")) for p in orphaned],
            )

        return new_pairs, orphaned

    def _pairing_register(self, proxy_id: str, fen: str, side: str) -> tuple[set[frozenset], set[str]]:
        """Re-register proxy at a new FEN, then recompute pairing groups.

        Returns ``(new_pairs, orphaned)`` from ``_recompute_groups``."""
        self._pairing_unregister(proxy_id)
        bucket = self._pairing_map.setdefault(fen, [])
        bucket.append((proxy_id, side))
        self._pairing_state[proxy_id] = (fen, side)
        if _DEBUG_PAIRING:
            self._pairing_assert_invariants()
            if len(bucket) > 2:
                log.warning(
                    "pairing: bucket >2 fen=%s entries=%s",
                    fen,
                    [(pid, s) for pid, s in bucket],
                )
        return self._recompute_groups()

    def _pairing_unregister(self, proxy_id: str) -> None:
        prev = self._pairing_state.pop(proxy_id, None)
        if prev is None:
            return
        fen, _side = prev
        bucket = self._pairing_map.get(fen)
        if bucket is None:
            return
        bucket[:] = [(pid, s) for (pid, s) in bucket if pid != proxy_id]
        if not bucket:
            del self._pairing_map[fen]

    def _pairing_apply_bestmove(
        self, proxy_id: str, parsed: dict | None
    ) -> tuple[set[frozenset], set[str]]:
        """Re-register at post-move FEN (waiting for opponent)."""
        state = self._pairing_state.get(proxy_id)
        if state is None or not parsed:
            return set(), set()
        move_uci = parsed.get("move")
        if not move_uci or move_uci == "(none)":
            return set(), set()
        fen, my_color = state
        try:
            board = chess.Board(fen)
            board.push_uci(move_uci)
        except (ValueError, chess.IllegalMoveError, chess.InvalidMoveError):
            return set(), set()
        return self._pairing_register(proxy_id, board.fen(), my_color)

    async def _emit_group_events(
        self, new_pairs: set[frozenset], orphaned: set[str]
    ) -> None:
        """Emit ``proxy_paired`` for new pairs; dissolve orphans.

        Pair dissolution is the sole game-end signal: when one of a
        confirmed pair's proxies leaves its FEN bucket (typically via
        ``ucinewgame``), that pair's game is over. Result/termination
        are not derivable from UCI alone -- we report UNKNOWN and let
        consumers fill them in from another signal if/when available."""
        for group in new_pairs:
            white_pid, black_pid = self._white_black_for_group(group)
            await self._emit("proxy_paired", {
                "tournament_id": self._active_id,
                "pair_id": self._pair_ids.get(group, ""),
                # `_a` is always white; `_b` is always black. See
                # `_white_black_for_group` -- frozenset iteration is
                # nondeterministic so we sort by side explicitly.
                "proxy_a": white_pid,
                "engine_a": self._proxy_engine_names.get(white_pid, white_pid),
                "side_a": "white",
                "proxy_b": black_pid,
                "engine_b": self._proxy_engine_names.get(black_pid, black_pid),
                "side_b": "black",
            })
        # Orphan path is a backstop: ucinewgame and proxy_session_ended
        # dissolve confirmed pairs directly. This catches edge cases
        # where a confirmed pair's bucket disappears via some other path.
        seen: set[str] = set()
        for pid in orphaned:
            if pid in seen:
                continue
            peer = self._confirmed_pairs.get(pid)
            if peer is None:
                continue
            seen.update((pid, peer))
            pair_id = self._pair_ids.get(frozenset((pid, peer)), "")
            if pair_id:
                await self._dissolve_pair(
                    pair_id, _RESULT_UNKNOWN, _TERMINATION_UNKNOWN,
                )

    def _white_black_for_group(self, group: frozenset) -> tuple[str, str]:
        """Return (white_pid, black_pid) for a confirmed pair. Frozenset
        iteration order is nondeterministic; resolve orientation from
        `_pair_white` (cached at confirmation), falling back to
        `_pairing_state` for groups not yet confirmed."""
        pids = tuple(group)
        if len(pids) != 2:
            return pids[0] if pids else "", pids[1] if len(pids) > 1 else ""
        pid_a, pid_b = pids
        pair_id = self._pair_ids.get(group)
        white = self._pair_white.get(pair_id) if pair_id else None
        if white in (pid_a, pid_b):
            return (white, pid_b if white == pid_a else pid_a)
        side_a = (self._pairing_state.get(pid_a) or ("", "?"))[1]
        return (pid_a, pid_b) if side_a == "white" else (pid_b, pid_a)

    async def _on_pgn_record(self, record: PgnGameRecord) -> None:
        """Tailer hook: route into the reconciliation queue and emit
        ``game_reconciled`` on a hit. Sweeps stale entries each tick."""
        self._reconcile_queue.sweep()
        reconciled = self._reconcile_queue.add_pgn_record(record)
        if reconciled is not None:
            await self._emit_reconciled(reconciled)
        # Drain check: if no watchers and the pending queue cleared,
        # stop the tailer. This is the deferred stop the dissolution
        # gate skipped while pending was non-empty.
        self._schedule_tailer_stop("pending queue drained, no watchers")

    async def _maybe_start_tailer(self, reason: str) -> None:
        """Start the tailer if conditions still hold. Re-checks at task
        run time so a stop scheduled by an earlier transition that
        hasn't executed yet doesn't leave a subscriber tailer-less."""
        if (
            self._game_subscribers
            and self._pgn_tailer is not None
            and not self._pgn_tailer.is_running()
        ):
            log.info("PGN tailer starting: %s", reason)
            try:
                await self._pgn_tailer.start()
            except Exception:
                log.exception("PGN tailer start failed")

    async def _maybe_stop_tailer(self, reason: str) -> None:
        """Stop the tailer if conditions still hold. Re-checks at task
        run time so a subscribe that arrived between schedule and run
        keeps the tailer alive."""
        if (
            not self._game_subscribers
            and self._reconcile_queue.pending_count == 0
            and self._pgn_tailer is not None
            and self._pgn_tailer.is_running()
        ):
            log.info("PGN tailer stopping: %s", reason)
            try:
                await self._pgn_tailer.stop()
            except Exception:
                log.exception("PGN tailer stop failed")

    def _schedule_tailer_start(self, reason: str) -> None:
        asyncio.create_task(self._maybe_start_tailer(reason))

    def _schedule_tailer_stop(self, reason: str) -> None:
        asyncio.create_task(self._maybe_stop_tailer(reason))

    async def _emit_reconciled(self, m: ReconciledMatch) -> None:
        log.info(
            "reconciled pair=%s game_n=%d result=%s termination=%s plies=%d",
            m.pair_id[:8], m.game_n, m.result,
            m.termination or "<none>", m.ply_count,
        )
        await self._emit("game_reconciled", {
            "tournament_id": self._active_id,
            "pair_id": m.pair_id,
            "game_n": m.game_n,
            "white": m.pgn_white,
            "black": m.pgn_black,
            "result": m.result,
            "termination": m.termination,
            "matched": True,
            "ply_count": m.ply_count,
        })

    def _update_pair_moves(self, proxy_id: str, parsed: dict | None) -> None:
        """Capture the cumulative UCI move list of a confirmed pair.
        Longest-prefix-extension wins; book-prefix collisions and
        stale shorter frames are ignored."""
        if parsed is None or parsed.get("kind") != "position":
            return
        moves = parsed.get("moves")
        if not isinstance(moves, list):
            return
        peer = self._confirmed_pairs.get(proxy_id)
        if peer is None:
            return
        pair_id = self._pair_ids.get(frozenset((proxy_id, peer)))
        if not pair_id:
            return
        current = self._pair_moves.get(pair_id)
        if current is None:
            return
        # Longest-prefix-extension wins. Equal lengths: keep current.
        if len(moves) > len(current) and moves[: len(current)] == current:
            self._pair_moves[pair_id] = list(moves)

    async def _dissolve_pair(
        self,
        pair_id: str,
        result: str,
        termination: str | None,
        terminal: bool = False,
    ) -> None:
        """Drops pair bookkeeping, flushes WS sentinel, emits
        ``proxy_unpaired`` + ``game_finished``. Idempotent.

        ``terminal=True`` for tournament Stop/Done/Failed teardown:
        skip the reconciliation push since the PGN won't ever have
        these games (fastchess was killed mid-flight).
        """
        proxies = self._pair_proxies.pop(pair_id, None)
        if proxies is None:
            return
        white_pid, black_pid = self._white_black_for_group(proxies)
        self._confirmed_pairs.pop(white_pid, None)
        self._confirmed_pairs.pop(black_pid, None)
        self._pair_ids.pop(proxies, None)
        self._pair_white.pop(pair_id, None)
        moves = self._pair_moves.pop(pair_id, None)
        log.debug(
            "dissolve pair=%s plies=%d terminal=%s white=%s black=%s",
            pair_id[:8],
            len(moves) if moves else 0,
            terminal,
            self._proxy_engine_names.get(white_pid, "?"),
            self._proxy_engine_names.get(black_pid, "?"),
        )
        reconciled: ReconciledMatch | None = None
        if moves:
            entry = PendingMatch(
                pair_id=pair_id,
                white_proxy=white_pid,
                black_proxy=black_pid,
                white_engine=self._proxy_engine_names.get(white_pid),
                black_engine=self._proxy_engine_names.get(black_pid),
                uci_moves=moves,
            )
            # Park the entry on both terminal and non-terminal paths.
            # Terminal teardown finalizes the tailer after dissolves,
            # so parked entries get a chance to match the freshly-read
            # PGN before _reset_pairing_state wipes the queue.
            reconciled = self._reconcile_queue.add_pending(entry)
        game_subs = self._game_subscribers.pop(pair_id, None)
        # Try the stop transition; the helper re-checks subs+pending at
        # task run time. Skip in terminal mode -- teardown owns its own
        # explicit poll_once()+stop() sequence.
        if not terminal:
            self._schedule_tailer_stop("last watched pair dissolved")
        if _DEBUG_PAIRING:
            log.debug(
                "pair dissolved tag=%s result=%s termination=%s game_subs=%d",
                pair_id[:8], result, termination,
                len(game_subs) if game_subs else 0,
            )
        if game_subs:
            sentinel = {
                "proxy_id": white_pid,
                "ended": True,
                "result": result,
                "termination": termination,
            }
            for q in game_subs:
                q.put_sentinel(sentinel)
                q.cancel_timers()
        # TODO: `proxy_unpaired` is redundant with `game_finished` -- same
        # trigger, overlapping payload. Kept for now as a debug signal.
        await self._emit("proxy_unpaired", {
            "tournament_id": self._active_id,
            "pair_id": pair_id,
            "proxy_id": white_pid,
            "peer_id": black_pid,
        })
        await self._emit("game_finished", {
            "tournament_id": self._active_id,
            "pair_id": pair_id,
            # game_n stays null here; reconciliation surfaces it on game_reconciled.
            "game_n": None,
            "proxy_a": white_pid,
            "proxy_b": black_pid,
            "engine_a": self._proxy_engine_names.get(white_pid),
            "engine_b": self._proxy_engine_names.get(black_pid),
            "result": result,
            "termination": termination,
        })
        if reconciled is not None:
            await self._emit_reconciled(reconciled)

    async def _dissolve_all_pairs(self) -> None:
        """Dissolve any pair still open at terminal teardown. Normally
        empty -- remaining pairs mean fastchess exited mid-game."""
        pending_ids = list(self._pair_ids.values())
        if pending_ids:
            log.warning("terminal teardown found %d undissolved pair(s)", len(pending_ids))
        for pair_id in pending_ids:
            await self._dissolve_pair(
                pair_id, _RESULT_UNKNOWN, _TERMINATION_UNKNOWN,
                terminal=True,
            )

    def _paired_subscribers(
        self, proxy_id: str, fen: str, side: str
    ) -> "set[CoalescingQueue]":
        """WS queues of opposite-color proxies at ``fen`` (the waiter)."""
        bucket = self._pairing_map.get(fen)
        if not bucket:
            return set()
        target_side = _opposite_side(side)
        out: set[CoalescingQueue] = set()
        for pid, s in bucket:
            if pid == proxy_id or s != target_side:
                continue
            subs = self._proxy_subscribers.get(pid)
            if subs:
                out.update(subs)
        return out

    def _pairing_assert_invariants(self) -> None:
        """SV_DEBUG_PAIRING-gated. Only proxy-uniqueness is invariant:
        with concurrency > 1 fastchess runs multiple game-pairs and
        two same-color proxies from different pairs can legitimately
        share a FEN. Logs + continues rather than crashing ingest."""
        seen: set[str] = set()
        for _fen, bucket in self._pairing_map.items():
            for pid, _color in bucket:
                if pid in seen:
                    log.error("pairing invariant: proxy %s registered at multiple FENs; map=%s", pid, self._pairing_map)
                    return
                seen.add(pid)

    async def proxy_session_started(self, proxy_id: str, engine_name: str) -> None:
        """Called when a proxy reports it has started up. Records the
        engine's display name and broadcasts a ``proxy_started`` event
        so the workspace's Schedule can add a row for it."""
        self._proxy_engine_names[proxy_id] = engine_name
        log.debug("proxy session started: %s (%s)", proxy_id, engine_name)
        await self._emit("proxy_started", {
            "tournament_id": self._active_id,
            "proxy_id": proxy_id,
            "engine_name": engine_name,
        })

    async def proxy_session_ended(self, proxy_id: str) -> None:
        """Called when a proxy session has exited (clean or otherwise).
        Closes any subscribers and drops bookkeeping for this proxy."""
        # See ucinewgame handler: dissolve directly, not via orphan path.
        peer = self._confirmed_pairs.get(proxy_id)
        if peer is not None:
            pair_id = self._pair_ids.get(frozenset((proxy_id, peer)), "")
            if pair_id:
                await self._dissolve_pair(
                    pair_id, _RESULT_UNKNOWN, _TERMINATION_UNKNOWN,
                )
        self._proxy_engine_names.pop(proxy_id, None)
        self._proxy_snapshot.pop(proxy_id, None)
        self._pairing_unregister(proxy_id)
        new_pairs, orphaned = self._recompute_groups()
        self._pairing_color.pop(proxy_id, None)
        subs = self._proxy_subscribers.pop(proxy_id, None)
        if subs:
            for q in subs:
                q.put_sentinel({"proxy_id": proxy_id, "ended": True})
                q.cancel_timers()
        await self._emit("proxy_ended", {
            "tournament_id": self._active_id,
            "proxy_id": proxy_id,
        })
        await self._emit_group_events(new_pairs, orphaned)

    def subscribe_to_proxy(self, proxy_id: str) -> CoalescingQueue:
        """WS handler calls this; returns a bounded queue that receives
        per-line dicts ``{proxy_id, line}`` and a final
        ``{proxy_id, ended: True}`` when the proxy session ends.

        Replays the proxy's current snapshot (latest ``position`` /
        ``go`` / ``info``) onto the queue so a window opened mid-game
        gets an instant render of the engine's state instead of having
        to wait for the engine's next event (>=10s under long TC)."""
        q = CoalescingQueue(maxsize=512)
        self._proxy_subscribers.setdefault(proxy_id, set()).add(q)
        snap = self._proxy_snapshot.get(proxy_id)
        if snap:
            for kind in ("position", "go", "info"):
                line = snap.get(kind)
                if line is None:
                    continue
                # Snapshot replay bypasses coalescing -- these are all
                # the latest values already, no benefit to slotting.
                payload = {"proxy_id": proxy_id, "line": line}
                if kind == "info":
                    q.put_info(payload)
                else:
                    q.put_other(payload)
        return q

    def unsubscribe_from_proxy(self, proxy_id: str, queue: CoalescingQueue) -> None:
        subs = self._proxy_subscribers.get(proxy_id)
        if subs is not None:
            subs.discard(queue)
            if not subs:
                self._proxy_subscribers.pop(proxy_id, None)
        queue.cancel_timers()

    def subscribe_to_game(self, pair_id: str) -> CoalescingQueue:
        """WS handler calls this for game-scoped subscriptions.

        The queue receives the same ``{proxy_id, line}`` payloads as the
        proxy subscriber but is closed (via ``{ended: True}`` sentinel)
        when the pair dissolves, not when the proxy session ends."""
        q = CoalescingQueue(maxsize=512)
        proxies = self._pair_proxies.get(pair_id)
        if not proxies:
            # Pair already dissolved before this subscriber attached
            # (race: user clicks Watch as the game ends). Push the
            # sentinel immediately so the WS handler closes cleanly
            # instead of leaving a stuck window.
            q.put_sentinel({
                "proxy_id": "",
                "ended": True,
                "result": _RESULT_UNKNOWN,
                "termination": _TERMINATION_UNKNOWN,
            })
            return q
        self._game_subscribers.setdefault(pair_id, set()).add(q)
        # Wake the tailer so reconciliation is live while a watcher is
        # attached. The helper re-checks at task run time, so a stop
        # task scheduled by a prior unsubscribe in the same tick won't
        # leave us tailer-less.
        self._schedule_tailer_start("game subscriber attached")
        # Replay snapshot for both proxies so a late subscriber gets
        # instant board state without waiting for the next UCI event.
        for pid in proxies:
            snap = self._proxy_snapshot.get(pid)
            if snap:
                for kind in ("position", "go", "info"):
                    raw = snap.get(kind)
                    if raw:
                        payload = {"proxy_id": pid, "line": raw,
                                   "parsed": parse_uci_line(raw)}
                        if kind == "info":
                            q.put_info(payload)
                        else:
                            q.put_other(payload)
        return q

    def unsubscribe_from_game(self, pair_id: str, queue: CoalescingQueue) -> None:
        subs = self._game_subscribers.get(pair_id)
        if subs is not None:
            subs.discard(queue)
            if not subs:
                self._game_subscribers.pop(pair_id, None)
        self._schedule_tailer_stop("last game subscriber detached")
        queue.cancel_timers()

    def _close_all_proxy_subscribers(self) -> None:
        """End-of-tournament cleanup -- wake all subscribers with the
        ended sentinel and drop the subscription table."""
        for proxy_id, subs in list(self._proxy_subscribers.items()):
            for q in subs:
                q.put_sentinel({"proxy_id": proxy_id, "ended": True})
        self._proxy_subscribers.clear()
