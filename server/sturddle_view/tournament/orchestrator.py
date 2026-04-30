"""Tournament orchestrator — composes ``TournamentStore`` and a ``Runner``.

Owns:
  - The single-active-tournament invariant (the store can't tell stale
    ``running`` on disk from a real running process; the orchestrator
    can, because it owns the runner).
  - Startup reconciliation: any tournament whose persisted status is
    ``running`` is marked ``stopped`` on server boot (Phase 1: no
    Resume).
  - Wiring runner events back to the store and to the broadcast tap.

Public surface is web-agnostic (takes ids and a broadcast callback) so
the same orchestrator drives the Phase 1.5 CLI wrapper without HTTP
coupling.

Stub for Slice 0 of the tournament implementation plan; concrete
implementation lands in Slice 4.
"""
from __future__ import annotations

from .runner import Runner
from .store import TournamentStore


class Orchestrator:
    def __init__(self, store: TournamentStore, runner: Runner) -> None:
        self._store = store
        self._runner = runner

    async def start(self, tournament_id: str) -> None:
        raise NotImplementedError

    async def stop(self, tournament_id: str) -> None:
        raise NotImplementedError

    def reconcile_on_startup(self) -> None:
        raise NotImplementedError
