"""Tournament orchestrator — composes ``TournamentStore`` and a ``Runner``.

Owns:
- Single-active-tournament invariant (the store can't tell stale ``running``
  on disk from a real live process; the orchestrator can).
- Startup reconciliation: persisted ``running`` is reset to ``stopped`` on boot.
- Wiring runner events to the store and broadcast tap.
- Per-tournament proxy bookkeeping (engine names, subscribers, broadcast
  secret). Single-side observation only — pair detection deferred.

Web-agnostic: takes ids + a broadcast callback, so the CLI wrapper can reuse it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Awaitable, Callable

import chess

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


# Per-tournament event history depth. Big enough to cover all the
# fastchess startup chatter (engine init, opening probes) plus a few
# completed games, so a workspace opened mid-tournament still gets
# a useful tail.
_EVENT_HISTORY_MAX = 200


# Two engines of one game share a FEN at the rendezvous: the thinker
# (registered via ``position``) and the waiter (registered via
# ``bestmove`` at the resulting FEN). ``info`` from the thinker fans
# out to the waiter's subscribers — that's the opponent's PV arrow.
_DEBUG_PAIRING = os.environ.get("SV_DEBUG_PAIRING", "0") == "1"


def _opposite_side(side: str) -> str:
    return "black" if side == "white" else "white"


def _fanout(subs: set[asyncio.Queue], payload: dict) -> None:
    """Slow-consumer policy: drop oldest, keep newest."""
    for q in list(subs):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass


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
    """Composition wiring decided at construction. Kept tiny on purpose —
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
        self._proxy_subscribers: dict[str, set[asyncio.Queue]] = {}
        # Display name reported by each proxy on session start. Used to
        # label rows / buttons in the workspace UI. Cleared on session
        # end and on tournament teardown.
        self._proxy_engine_names: dict[str, str] = {}
        # Per-proxy snapshot of the most-recent stateful UCI lines:
        # ``position`` (current board), ``go`` (current clocks), and
        # ``info`` (current eval/depth/PV). Replayed to a subscriber on
        # connect so a window opened mid-game gets an instant snapshot
        # of the engine's state instead of waiting for the next event
        # (which under long time controls can be ≥10s away).
        self._proxy_snapshot: dict[str, dict[str, str]] = {}
        # Pairing detection. Map from FEN → list of ``(proxy_id, color)``
        # tuples. The thinker registers via ``position`` at F (its own
        # color); the waiter registers via ``bestmove`` at F-after-m
        # (still its own color — engine identity, not side-to-move).
        # Two engines of one game share a FEN with opposite colors.
        self._pairing_map: dict[str, list[tuple[str, str]]] = {}
        # ``(fen, my_color)``. my_color is the engine's color in the
        # current game (identity), not side-to-move at fen — that's
        # what makes the rendezvous lookup distinguish thinker from
        # waiter (same fen, opposite colors).
        self._pairing_state: dict[str, tuple[str, str] | None] = {}
        # Locked from side-to-move on the first ``position`` after
        # ``ucinewgame`` (that's the engine's first turn).
        self._pairing_color: dict[str, str | None] = {}
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
            raise TournamentBusyError(
                f"another tournament is running: {self._active_id!r}"
            )

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

        # Resource recheck — values were resolved + folded into the
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
            # Clear any prior last_error on (re)start — the user has
            # acted on the diagnostic by retrying.
            updated = self._store.update_status(
                t.id, STATUS_RUNNING, started_at=_now(), last_error=None,
            )
            await self._emit_status(updated)
            await self._runner.start(spec, self._on_runner_event)
        except Exception:
            # Roll back the active claim so a failed start doesn't lock
            # out the next attempt.
            self._active_id = None
            self._proxy_secret = None
            self._proxy_engine_names.clear()
            self._proxy_snapshot.clear()
            self._pairing_map.clear()
            self._pairing_state.clear()
            self._pairing_color.clear()
            self._store.update_status(t.id, STATUS_STOPPED, stopped_at=_now())
            raise

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
        last_error — server died mid-tournament; can't claim a clean
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
          - ``done``         → status=done, active_id cleared
          - ``stopped``      → status=stopped, active_id cleared
          - ``runner_crash`` → status=failed + last_error persisted,
                               active_id cleared (no Resume in Phase 1)
          - ``started``      → no status change (we set RUNNING in start())
          - others           → forwarded as-is to broadcast
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
                    self._active_id = None
                    # Slice 9b: tear down the live-observation state so
                    # stale proxies (if any survive past fastchess exit)
                    # can't post and any open WS subscribers get a clean
                    # "ended" signal.
                    self._proxy_secret = None
                    self._proxy_engine_names.clear()
                    self._proxy_snapshot.clear()
                    self._pairing_map.clear()
                    self._pairing_state.clear()
                    self._pairing_color.clear()
                    self._close_all_proxy_subscribers()

        # Always forward the runner event upstream — UI consumers want
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
                tid, deque(maxlen=_EVENT_HISTORY_MAX)
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
        the raw runner kind (``runner_log``, ``status_change``, …) and
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
        (always — independent of subscribers) and fans out to any
        subscribed WS clients. Drives the pairing map so that ``info``
        lines from the thinking engine also fan out to the
        opposite-color subscribers (the live opponent's view)."""
        snap = self._proxy_snapshot.setdefault(proxy_id, {})
        own_subs = self._proxy_subscribers.get(proxy_id)
        for line in lines:
            stripped = line.lstrip()
            parsed: dict | None = None
            paired_subs: set[asyncio.Queue] = set()
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
                        self._pairing_register(proxy_id, parsed["fen"], color)
            elif stripped.startswith("ucinewgame"):
                self._pairing_unregister(proxy_id)
                self._pairing_color[proxy_id] = None
            elif stripped.startswith("go "):
                snap["go"] = line
            elif stripped.startswith("bestmove "):
                parsed = parse_uci_line(stripped)
                self._pairing_apply_bestmove(proxy_id, parsed)
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
                }
                _fanout(paired_subs, payload)

    def _pairing_register(self, proxy_id: str, fen: str, side: str) -> None:
        """Atomic transition — a proxy is in the map at most once."""
        self._pairing_unregister(proxy_id)
        self._pairing_map.setdefault(fen, []).append((proxy_id, side))
        self._pairing_state[proxy_id] = (fen, side)
        if _DEBUG_PAIRING:
            self._pairing_assert_invariants()

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

    def _pairing_apply_bestmove(self, proxy_id: str, parsed: dict | None) -> None:
        """Re-register at post-move FEN (waiting for opponent). Same
        color — engine identity is fixed for the game."""
        state = self._pairing_state.get(proxy_id)
        if state is None or not parsed:
            return
        move_uci = parsed.get("move")
        if not move_uci or move_uci == "(none)":
            return
        fen, my_color = state
        try:
            board = chess.Board(fen)
            board.push_uci(move_uci)
        except (ValueError, chess.IllegalMoveError, chess.InvalidMoveError):
            return
        self._pairing_register(proxy_id, board.fen(), my_color)

    def _paired_subscribers(
        self, proxy_id: str, fen: str, side: str
    ) -> set[asyncio.Queue]:
        """WS queues of opposite-color proxies at ``fen`` (the waiter)."""
        bucket = self._pairing_map.get(fen)
        if not bucket:
            return set()
        target_side = _opposite_side(side)
        out: set[asyncio.Queue] = set()
        for pid, s in bucket:
            if pid == proxy_id or s != target_side:
                continue
            subs = self._proxy_subscribers.get(pid)
            if subs:
                out.update(subs)
        return out

    def _pairing_assert_invariants(self) -> None:
        """SV_DEBUG_PAIRING-gated. proxy unique across the map; color
        unique within a bucket."""
        seen: set[str] = set()
        for fen, bucket in self._pairing_map.items():
            colors_in_bucket: set[str] = set()
            for pid, color in bucket:
                assert pid not in seen, f"proxy {pid} registered at multiple FENs"
                seen.add(pid)
                assert color not in colors_in_bucket, (
                    f"two proxies with color={color} share fen={fen}"
                )
                colors_in_bucket.add(color)

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
        self._proxy_engine_names.pop(proxy_id, None)
        self._proxy_snapshot.pop(proxy_id, None)
        self._pairing_unregister(proxy_id)
        self._pairing_color.pop(proxy_id, None)
        subs = self._proxy_subscribers.pop(proxy_id, None)
        if subs:
            for q in subs:
                # Sentinel: signal end of stream to subscribers.
                try:
                    q.put_nowait({"proxy_id": proxy_id, "ended": True})
                except asyncio.QueueFull:
                    pass
        await self._emit("proxy_ended", {
            "tournament_id": self._active_id,
            "proxy_id": proxy_id,
        })

    def subscribe_to_proxy(self, proxy_id: str) -> asyncio.Queue:
        """WS handler calls this; returns a bounded queue that receives
        per-line dicts ``{proxy_id, line}`` and a final
        ``{proxy_id, ended: True}`` when the proxy session ends.

        Replays the proxy's current snapshot (latest ``position`` /
        ``go`` / ``info``) onto the queue so a window opened mid-game
        gets an instant render of the engine's state instead of having
        to wait for the engine's next event (≥10s under long TC)."""
        q: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._proxy_subscribers.setdefault(proxy_id, set()).add(q)
        snap = self._proxy_snapshot.get(proxy_id)
        if snap:
            for kind in ("position", "go", "info"):
                line = snap.get(kind)
                if line is None:
                    continue
                try:
                    q.put_nowait({"proxy_id": proxy_id, "line": line})
                except asyncio.QueueFull:
                    pass
        return q

    def unsubscribe_from_proxy(self, proxy_id: str, queue: asyncio.Queue) -> None:
        subs = self._proxy_subscribers.get(proxy_id)
        if subs is not None:
            subs.discard(queue)
            if not subs:
                self._proxy_subscribers.pop(proxy_id, None)

    def _close_all_proxy_subscribers(self) -> None:
        """End-of-tournament cleanup — wake all subscribers with the
        ended sentinel and drop the subscription table."""
        for proxy_id, subs in list(self._proxy_subscribers.items()):
            for q in subs:
                try:
                    q.put_nowait({"proxy_id": proxy_id, "ended": True})
                except asyncio.QueueFull:
                    pass
        self._proxy_subscribers.clear()
