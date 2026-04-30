"""Tournament orchestrator — composes ``TournamentStore`` and a ``Runner``.

Owns:

  - The single-active-tournament invariant. The store can't tell stale
    ``running`` on disk from a real running process; the orchestrator
    can, because it owns the runner.
  - Startup reconciliation: any tournament whose persisted status is
    ``running`` is marked ``stopped`` on server boot (Phase 1: no
    Resume — see ``docs/tournament-spec.md``).
  - Wiring runner events back to the store and to the broadcast tap.

Public surface is web-agnostic (takes ids and a broadcast callback) so
the same orchestrator drives the Phase 1.5 CLI wrapper without HTTP
coupling.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

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

        spec = RunSpec(
            tournament=t,
            binary_path=getattr(self._runner, "binary_path", "") or "",
            work_dir=self._store._dir(t.id),
            pgn_path=self._store.pgn_path(t.id),
            config_path=self._store.config_path(t.id),
            log_path=self._store.logs_dir(t.id) / "fastchess.log",
        )

        # Mark active *before* spawning so a concurrent ``start`` call
        # racing against this one is rejected by the busy check above.
        self._active_id = t.id
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
