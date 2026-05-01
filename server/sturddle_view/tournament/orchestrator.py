"""Tournament orchestrator — composes ``TournamentStore`` and a ``Runner``.

Owns:

  - The single-active-tournament invariant. The store can't tell stale
    ``running`` on disk from a real running process; the orchestrator
    can, because it owns the runner.
  - Startup reconciliation: any tournament whose persisted status is
    ``running`` is marked ``stopped`` on server boot (Phase 1: no
    Resume — see ``docs/tournament-spec.md``).
  - Wiring runner events back to the store and to the broadcast tap.
  - The per-tournament ``PairIndex`` and proxy-secret/subscribers state
    (Slice 9b).

Public surface is web-agnostic (takes ids and a broadcast callback) so
the same orchestrator drives the Phase 1.5 CLI wrapper without HTTP
coupling.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from .pair_index import PairIndex
from .runner import EventCallback, RunSpec, Runner
from .store import (
    STATUS_DONE,
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

        # Slice 9b: live observation pipeline.
        # The ``PairIndex`` tracks which proxies are playing each other
        # by watching UCI ``position`` lines. Subscribers are WS clients
        # listening for one specific proxy's stream.
        self._pair_index: PairIndex = PairIndex()
        self._proxy_subscribers: dict[str, set[asyncio.Queue]] = {}
        # Per-tournament secret embedded in the proxy --broadcast-url so
        # only proxies belonging to the active tournament can post.
        # Cleared on tournament termination.
        self._proxy_secret: str | None = None
        # Broadcast URL the proxy POSTs to. Set by the FastAPI app on
        # construction; ``None`` means "no proxy wrapping" (tests, CLI).
        self._proxy_broadcast_url: str | None = None

    def set_proxy_broadcast_url(self, url: str | None) -> None:
        """Configure the URL proxies POST to. The orchestrator passes
        this into the ``RunSpec`` when starting fastchess so each
        engine is wrapped in the proxy script."""
        self._proxy_broadcast_url = url

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

        # Mark active *before* spawning so a concurrent ``start`` call
        # racing against this one is rejected by the busy check above.
        self._active_id = t.id
        # Fresh proxy secret + pair index per tournament run.
        self._proxy_secret = secrets.token_urlsafe(24)
        self._pair_index.reset()

        spec = RunSpec(
            tournament=t,
            binary_path=getattr(self._runner, "binary_path", "") or "",
            work_dir=self._store._dir(t.id),
            pgn_path=self._store.pgn_path(t.id),
            config_path=self._store.config_path(t.id),
            log_path=self._store.logs_dir(t.id) / "fastchess.log",
            proxy_broadcast_url=self._proxy_broadcast_url,
            proxy_secret=self._proxy_secret,
        )
        try:
            updated = self._store.update_status(
                t.id, STATUS_RUNNING, started_at=_now()
            )
            await self._emit_status(updated)
            await self._runner.start(spec, self._on_runner_event)
        except Exception:
            # Roll back the active claim so a failed start doesn't lock
            # out the next attempt.
            self._active_id = None
            self._proxy_secret = None
            self._pair_index.reset()
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
        """Mark any persisted ``running`` rows as ``stopped``.

        Phase 1 has no Resume; surviving the server crash is a
        reconciliation, not a recovery. Returns the list of tournaments
        whose status was changed (callers may want to log / surface
        them)."""
        stale = self._store.find_by_status(STATUS_RUNNING)
        out: list[Tournament] = []
        for t in stale:
            updated = self._store.update_status(
                t.id, STATUS_STOPPED, stopped_at=_now()
            )
            log.info(
                "reconcile: tournament %s was 'running' on disk; marking 'stopped'",
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
          - ``runner_crash`` → status=stopped, active_id cleared
                               (no Resume in Phase 1)
          - ``started``      → no status change (we set RUNNING in start())
          - others           → forwarded as-is to broadcast
        """
        active_id = self._active_id

        if kind in ("done", "stopped", "runner_crash"):
            if active_id is not None:
                terminal_status = STATUS_DONE if kind == "done" else STATUS_STOPPED
                try:
                    updated = self._store.update_status(
                        active_id, terminal_status, stopped_at=_now()
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
                    self._pair_index.reset()
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
        })

    async def _emit(self, kind: str, payload: dict) -> None:
        if self._broadcast is None:
            return
        try:
            await self._broadcast(kind, payload)
        except Exception:
            log.exception("broadcast callback raised for %s", kind)

    # ---- Slice 9b: live-observation pipeline -------------------------------

    def proxy_secret(self) -> str | None:
        """Per-tournament secret embedded in the proxy broadcast URL.
        ``None`` when no tournament is running."""
        return self._proxy_secret

    def pair_index_snapshot(self) -> dict[str, tuple[str, str]]:
        """Read-only view of the current proxy-pair mappings.

        Used by the API ``GET /api/tournaments/{id}`` so a workspace
        opening mid-tournament can seed its in-progress rows even if it
        missed the forward-going ``game_paired`` events."""
        return self._pair_index.all_games()

    def verify_proxy_secret(self, presented: str | None) -> bool:
        if self._proxy_secret is None or presented is None:
            return False
        # Constant-time comparison.
        import hmac
        return hmac.compare_digest(self._proxy_secret, presented)

    async def ingest_proxy_lines(
        self, proxy_id: str, lines: list[str]
    ) -> list[str]:
        """Called by the ``/internal/proxy`` endpoint with a batch of
        UCI lines from one proxy. Updates the pair index, fans out to
        any subscribed WS clients, and returns the list of newly-paired
        game ids (to be broadcast as ``game_paired`` events).
        """
        # Diagnostic: log only position lines (very low volume) so we
        # can see what the pair_index is being asked to match without
        # spamming the log with info chatter.
        position_lines = [line for line in lines if line.lstrip().startswith("position ")]
        if position_lines:
            log.debug(
                "proxy %s position lines: %r", proxy_id, position_lines
            )
        new_games: list[str] = []
        for line in lines:
            gid = self._pair_index.observe(proxy_id, line)
            if gid is not None:
                new_games.append(gid)
                log.info(
                    "pair_index locked game %s = (%s, %s)",
                    gid, proxy_id, self._pair_index.proxies_for(gid),
                )

        # Fan out to subscribers (if any).
        subs = self._proxy_subscribers.get(proxy_id)
        if subs:
            for line in lines:
                payload = {"proxy_id": proxy_id, "line": line}
                for q in list(subs):
                    try:
                        q.put_nowait(payload)
                    except asyncio.QueueFull:
                        # Slow consumer: drop oldest, keep newest.
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                        try:
                            q.put_nowait(payload)
                        except asyncio.QueueFull:
                            pass

        # Surface newly paired games on the broadcast bus so the
        # Schedule window can show "in progress" rows.
        for gid in new_games:
            pair = self._pair_index.proxies_for(gid)
            await self._emit("game_paired", {
                "tournament_id": self._active_id,
                "game_id": gid,
                "proxies": list(pair) if pair else [],
            })
        return new_games

    def proxy_session_started(self, proxy_id: str, engine_name: str) -> None:
        """Called when a proxy reports it has started up. Useful for
        Schedule's "engines that have spawned" indicator."""
        # No bookkeeping needed yet — kept as an API hook so the proxy
        # can announce itself before any UCI traffic flows.
        log.debug("proxy session started: %s (%s)", proxy_id, engine_name)

    async def proxy_session_ended(self, proxy_id: str) -> None:
        """Called when a proxy session has exited (clean or otherwise).

        Closes any subscribers, drops pair-index entries.
        """
        self._pair_index.forget_proxy(proxy_id)
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
        ``{proxy_id, ended: True}`` when the proxy session ends."""
        q: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._proxy_subscribers.setdefault(proxy_id, set()).add(q)
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
