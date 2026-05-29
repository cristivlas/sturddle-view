"""On-disk tournament persistence under ``<tournaments-root>/<id>/``.

Pure persistence — the single-active invariant lives in the orchestrator,
which distinguishes a stale ``running`` on disk from a real live process.
"""
from __future__ import annotations

import json
import secrets
import shutil
import threading
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import platformdirs

from .. import APP_NAME
from .._atomic import atomic_write_json


# Allowed status values. Phase 1 has no `paused` (see tournament-spec.md).
# `failed` is distinct from `stopped` — the latter is user-initiated, the
# former is a runner crash with diagnostics in `last_error`.
STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_STOPPED = "stopped"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
_VALID_STATUSES = frozenset({
    STATUS_IDLE, STATUS_RUNNING, STATUS_STOPPED, STATUS_DONE, STATUS_FAILED,
})


class StoreError(Exception):
    """Base for store-layer errors."""


class CorruptStateError(StoreError):
    """state.json is missing, unparseable, or fails schema validation."""


class TournamentNotFoundError(StoreError):
    """No tournament with the given id under the configured root."""


class DuplicateNameError(StoreError):
    """A tournament with the requested name already exists."""


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
    # Snapshot of global engine_default_* settings at creation time.
    # Frozen so Stop/Resume can't drift if Settings change mid-run.
    engine_defaults: dict = field(default_factory=dict)
    # Diagnostic for the most recent runner_crash. None when the
    # tournament has never failed (or was cleared on a successful start).
    # Shape: {"rc": int, "stderr_tail": list[str], "at": iso8601}.
    last_error: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def default_root() -> Path:
    """Default tournaments root using platformdirs (cross-platform)."""
    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False)) / "tournaments"


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
        self._create_lock = threading.Lock()

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

    def create(self, name: str, template: dict, engines: list, engine_defaults: dict | None = None) -> Tournament:
        """Create a new tournament directory and persist its initial state.json.

        Raises ``DuplicateNameError`` if another tournament already has
        the same name. The duplicate check and the directory creation
        run under a single lock so concurrent callers cannot both
        succeed with the same name.
        """
        self._ensure_root()
        with self._create_lock:
            if any(t.name == name for t in self.list()):
                raise DuplicateNameError(name)
            tournament_id = uuid.uuid4().hex
            d = self._dir(tournament_id)
            d.mkdir(parents=True, exist_ok=False)
            (d / "logs").mkdir(parents=True, exist_ok=True)

            # Pin a seed for fastchess so opening-book shuffle (and
            # anything else fastchess seeds from -srand) is stable
            # across Stop/Resume cycles. Caller-supplied seed wins so
            # tests/fixtures can be deterministic.
            frozen_template = dict(template)
            frozen_template.setdefault("seed", secrets.randbits(63))

            t = Tournament(
                id=tournament_id,
                name=name,
                status=STATUS_IDLE,
                created_at=_now(),
                template=frozen_template,
                engines=list(engines),
                engine_defaults=dict(engine_defaults or {}),
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
        # Drop fields that no longer exist on the dataclass (forward-compat
        # with old state.json files written by prior versions).
        known = {f.name for f in fields(Tournament)}
        payload = {k: v for k, v in payload.items() if k in known}
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

    _UNSET = object()

    def update_status(
        self,
        tournament_id: str,
        status: str,
        *,
        started_at: str | None = None,
        stopped_at: str | None = None,
        last_error: Any = _UNSET,
    ) -> Tournament:
        if status not in _VALID_STATUSES:
            raise ValueError(f"invalid status: {status!r}")
        t = self.get(tournament_id)
        t.status = status
        if started_at is not None:
            t.started_at = started_at
        if stopped_at is not None:
            t.stopped_at = stopped_at
        if last_error is not self._UNSET:
            t.last_error = last_error
        atomic_write_json(self._state_path(tournament_id), t.to_dict(), indent=2)
        return t

    def update(
        self,
        tournament_id: str,
        *,
        name: str,
        template: dict,
        engines: list,
        engine_defaults: dict | None = None,
    ) -> "Tournament":
        """Replace name/template/engines and reset the tournament to idle.

        ``engine_defaults`` re-freezes the global engine_default_* snapshot
        (mirroring ``create``); pass ``None`` to keep the existing snapshot.

        Wipes the tournament directory clean before writing the fresh
        state.json: any edit (template, engines, or rename) invalidates
        prior PGN results, fastchess config/backups, logs, and any
        stray files. They were produced under potentially different
        conditions and must not leak into future runs. Caller (API
        layer) is responsible for confirming with the user first.
        Filesystem errors during wipe propagate -- a locked file
        (AV scan, dangling handle) leaves the tournament in a usable
        state on disk and the API returns 5xx.
        """
        with self._create_lock:
            t = self.get(tournament_id)
            if name != t.name and any(
                x.name == name for x in self.list() if x.id != tournament_id
            ):
                raise DuplicateNameError(name)
            self._wipe_dir_contents(tournament_id)

            t.name = name
            t.template = dict(template)
            t.engines = list(engines)
            if engine_defaults is not None:
                t.engine_defaults = dict(engine_defaults)
            t.status = STATUS_IDLE
            t.started_at = None
            t.stopped_at = None
            t.last_error = None
            atomic_write_json(self._state_path(tournament_id), t.to_dict(), indent=2)
            return t

    def wipe_for_restart(self, tournament_id: str) -> "Tournament":
        """Wipe the tournament directory and reset runtime state, keeping
        name/template/engines/engine_defaults intact. Used when restarting
        from a stopped/failed tournament -- fastchess's resume contract is
        too fragile across stop/resume cycles, so we always start fresh.
        """
        with self._create_lock:
            t = self.get(tournament_id)
            self._wipe_dir_contents(tournament_id)
            t.status = STATUS_IDLE
            t.started_at = None
            t.stopped_at = None
            t.last_error = None
            atomic_write_json(self._state_path(tournament_id), t.to_dict(), indent=2)
            return t

    def _wipe_dir_contents(self, tournament_id: str) -> None:
        """Delete every entry inside the tournament dir, keeping the dir
        itself. Iterates children so a concurrent process can't claim the
        path between rmtree + recreate."""
        d = self._dir(tournament_id)
        if not d.exists():
            return
        for child in d.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()

    def find_by_status(self, status: str) -> list[Tournament]:
        """All tournaments currently in the given status (helper for orchestrator
        startup reconciliation)."""
        return [t for t in self.list() if t.status == status]
