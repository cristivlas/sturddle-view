"""On-disk persistence for tournaments.

Owns the directory tree under ``<tournaments-root>/<id>/`` and the
``state.json`` schema. Pure persistence — no subprocess concept; the
single-active invariant lives in the orchestrator (see
``orchestrator.py``), which can distinguish stale ``running`` on disk
from a real running process.

Layout per tournament (see ``docs/tournament-spec.md``):

    <tournaments-root>/
      <id>/
        state.json
        config.json    ← fastchess-owned (resume artifact)
        games.pgn      ← fastchess-owned
        logs/
          wrapper.log
          fastchess.log
"""
from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import platformdirs

from .._atomic import atomic_write_json


# Allowed status values. Phase 1 has no `paused` (see tournament-spec.md).
STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_STOPPED = "stopped"
STATUS_DONE = "done"
_VALID_STATUSES = frozenset({STATUS_IDLE, STATUS_RUNNING, STATUS_STOPPED, STATUS_DONE})


class StoreError(Exception):
    """Base for store-layer errors."""


class CorruptStateError(StoreError):
    """state.json is missing, unparseable, or fails schema validation."""


class TournamentNotFoundError(StoreError):
    """No tournament with the given id under the configured root."""


@dataclass
class Tournament:
    """In-memory view of one tournament's state.json."""
    id: str
    name: str
    status: str
    created_at: str
    started_at: str | None = None
    stopped_at: str | None = None
    template: dict = field(default_factory=dict)
    engines: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def default_root() -> Path:
    """Default tournaments root using platformdirs (cross-platform)."""
    return Path(platformdirs.user_data_dir("sturddle-view")) / "tournaments"


def _now() -> str:
    # Microsecond resolution so back-to-back creates sort deterministically.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _validate_state(payload: dict) -> None:
    required = {"id", "name", "status", "created_at"}
    missing = required - payload.keys()
    if missing:
        raise CorruptStateError(f"state.json missing required fields: {sorted(missing)}")
    if payload["status"] not in _VALID_STATUSES:
        raise CorruptStateError(f"invalid status: {payload['status']!r}")


class TournamentStore:
    """Typed view over ``<tournaments-root>/<id>/`` directories.

    Auto-creates the root on first use. Atomic writes via the existing
    ``atomic_write_json`` helper.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def set_root(self, root: Path) -> None:
        """Repoint the store at a different root. Affects future
        operations only; existing on-disk tournaments under the old
        root are not migrated."""
        self._root = Path(root)

    def _ensure_root(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)

    def dir_for(self, tournament_id: str) -> Path:
        """Path to the tournament's directory (may not exist yet)."""
        return self._root / tournament_id

    # Backwards-compat alias used by the orchestrator and tests.
    _dir = dir_for

    def _state_path(self, tournament_id: str) -> Path:
        return self._dir(tournament_id) / "state.json"

    def pgn_path(self, tournament_id: str) -> Path:
        return self._dir(tournament_id) / "games.pgn"

    def config_path(self, tournament_id: str) -> Path:
        return self._dir(tournament_id) / "config.json"

    def logs_dir(self, tournament_id: str) -> Path:
        return self._dir(tournament_id) / "logs"

    def create(self, name: str, template: dict, engines: list) -> Tournament:
        """Create a new tournament directory and persist its initial state.json."""
        self._ensure_root()
        tournament_id = uuid.uuid4().hex
        d = self._dir(tournament_id)
        d.mkdir(parents=True, exist_ok=False)
        (d / "logs").mkdir(parents=True, exist_ok=True)

        t = Tournament(
            id=tournament_id,
            name=name,
            status=STATUS_IDLE,
            created_at=_now(),
            template=dict(template),
            engines=list(engines),
        )
        atomic_write_json(self._state_path(tournament_id), t.to_dict(), indent=2)
        return t

    def get(self, tournament_id: str) -> Tournament:
        path = self._state_path(tournament_id)
        if not path.exists():
            raise TournamentNotFoundError(tournament_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise CorruptStateError(f"state.json unparseable for {tournament_id}: {e}") from e
        _validate_state(payload)
        return Tournament(**payload)

    def list(self) -> list[Tournament]:
        """Return all tournaments under the root, sorted by ``created_at``.

        Skips directories without a parseable ``state.json`` (logs a
        ``CorruptStateError`` would be too aggressive at list time;
        callers can call ``get(id)`` directly to surface the corruption).
        """
        if not self._root.exists():
            return []
        out: list[Tournament] = []
        for child in self._root.iterdir():
            if not child.is_dir():
                continue
            if not (child / "state.json").exists():
                continue
            try:
                out.append(self.get(child.name))
            except CorruptStateError:
                continue
        out.sort(key=lambda t: t.created_at)
        return out

    def remove(self, tournament_id: str) -> None:
        d = self._dir(tournament_id)
        if not d.exists():
            raise TournamentNotFoundError(tournament_id)
        shutil.rmtree(d)

    def update_status(
        self,
        tournament_id: str,
        status: str,
        *,
        started_at: str | None = None,
        stopped_at: str | None = None,
    ) -> Tournament:
        if status not in _VALID_STATUSES:
            raise ValueError(f"invalid status: {status!r}")
        t = self.get(tournament_id)
        t.status = status
        if started_at is not None:
            t.started_at = started_at
        if stopped_at is not None:
            t.stopped_at = stopped_at
        atomic_write_json(self._state_path(tournament_id), t.to_dict(), indent=2)
        return t

    def find_by_status(self, status: str) -> list[Tournament]:
        """All tournaments currently in the given status (helper for orchestrator
        startup reconciliation)."""
        return [t for t in self.list() if t.status == status]
