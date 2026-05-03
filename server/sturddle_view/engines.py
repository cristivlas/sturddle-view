"""Engine registry: persistent list of UCI engines the user has registered.

Storage: a single JSON file under the OS-appropriate user config dir
(`platformdirs.user_config_dir("sturddle-view")`).

The registry is in-memory once loaded; mutations are written back atomically.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

import chess.engine
from platformdirs import user_config_dir

from . import APP_NAME
from ._atomic import atomic_write_json

log = logging.getLogger(__name__)


def default_registry_path() -> Path:
    return Path(user_config_dir(APP_NAME, appauthor=False)) / "engines.json"


# UCI options the engine manages itself; rendering them in our dialog is
# either pointless or actively harmful. Lower-case for case-insensitive match.
_HIDDEN_OPTIONS = {"multipv", "ponder", "uci_chess960", "uci_variant", "uci_analysemode"}


async def probe_engine(
    engine_path: str,
) -> tuple[str | None, dict[str, dict], str | None]:
    """Briefly spawn the engine; return (uci_id_name, option_schema, error).

    `option_schema` is a {name: {type, default, min?, max?, vars?}} dict,
    skipping engine-managed options (multipv, ponder, etc.). `uci_id_name`
    is what the engine announces via UCI `id name`, or None if unavailable.
    `error` is None on success, or a short human-readable failure reason —
    callers can surface it to the UI so a half-broken registry entry is
    not silently presented as "engine reported no options".
    Best-effort: on any failure logs and returns (None, {}, error) so the
    engine can still be registered.
    """
    try:
        transport, engine = await chess.engine.popen_uci(engine_path)
    except Exception as e:
        log.exception("could not spawn %s for probe", engine_path)
        return None, {}, f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
    try:
        uci_name = engine.id.get("name") or None
        schema: dict[str, dict] = {}
        for name, opt in engine.options.items():
            if name.lower() in _HIDDEN_OPTIONS:
                continue
            entry: dict = {"type": opt.type, "default": opt.default}
            if opt.min is not None:
                entry["min"] = opt.min
            if opt.max is not None:
                entry["max"] = opt.max
            if opt.var:
                entry["vars"] = list(opt.var)
            schema[name] = entry
        return uci_name, schema, None
    finally:
        try:
            await engine.quit()
        except (chess.engine.EngineTerminatedError, RuntimeError, BrokenPipeError):
            pass
        transport.close()


@dataclass
class Engine:
    id: str
    name: str
    path: str
    # User-overridden UCI options. Only entries that differ from the engine's
    # advertised default are stored, so the per-engine dialog's "Defaults"
    # button can be honored unambiguously.
    options: dict[str, str | int | bool] = field(default_factory=dict)
    # Cached UCI option list captured at registration. Schema entries:
    #   {name: {type, default, min?, max?, vars?}}
    # type ∈ "spin" | "combo" | "check" | "string" | "button"
    option_schema: dict[str, dict] = field(default_factory=dict)

    @staticmethod
    def new(
        name: str,
        path: str,
        options: dict | None = None,
        option_schema: dict | None = None,
    ) -> "Engine":
        return Engine(
            id=uuid.uuid4().hex[:12],
            name=name,
            path=path,
            options=options or {},
            option_schema=option_schema or {},
        )


class EngineNotFoundError(KeyError):
    pass


class DuplicateEngineError(ValueError):
    pass


class EngineRegistry:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_registry_path()
        self._engines: dict[str, Engine] = {}
        self._selected_id: str | None = None
        self._loaded = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def selected_id(self) -> str | None:
        self._ensure_loaded()
        # Stale id (engine removed externally) -> clear it.
        if self._selected_id and self._selected_id not in self._engines:
            self._selected_id = None
        return self._selected_id

    def select(self, engine_id: str | None) -> None:
        self._ensure_loaded()
        if engine_id is not None and engine_id not in self._engines:
            raise EngineNotFoundError(engine_id)
        self._selected_id = engine_id
        self._save()

    def load(self) -> None:
        if not self._path.exists():
            self._engines = {}
            self._selected_id = None
            self._loaded = True
            return
        with self._path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        engines = {}
        for entry in data.get("engines", []):
            e = Engine(
                id=entry["id"],
                name=entry["name"],
                path=entry["path"],
                options=entry.get("options", {}),
                option_schema=entry.get("option_schema", {}),
            )
            engines[e.id] = e
        self._engines = engines
        self._selected_id = data.get("selected_id")
        if self._selected_id and self._selected_id not in self._engines:
            self._selected_id = None
        self._loaded = True

    def _save(self) -> None:
        payload = {
            "engines": [asdict(e) for e in self._engines.values()],
            "selected_id": self._selected_id,
        }
        atomic_write_json(self._path, payload, indent=2)

    def list(self) -> list[Engine]:
        self._ensure_loaded()
        return list(self._engines.values())

    def get(self, engine_id: str) -> Engine:
        self._ensure_loaded()
        try:
            return self._engines[engine_id]
        except KeyError as e:
            raise EngineNotFoundError(engine_id) from e

    def add(
        self,
        name: str,
        path: str,
        options: dict | None = None,
        option_schema: dict | None = None,
    ) -> Engine:
        self._ensure_loaded()
        # Treat (name, path) pair as the uniqueness key. Same binary at the same
        # path with the same display name is a duplicate; same binary with two
        # different names (e.g. different UCI options) is allowed.
        for e in self._engines.values():
            if e.name == name and e.path == path:
                raise DuplicateEngineError(f"engine already registered: {name} ({path})")
        engine = Engine.new(
            name=name, path=path, options=options, option_schema=option_schema
        )
        self._engines[engine.id] = engine
        self._save()
        return engine

    def update(
        self,
        engine_id: str,
        *,
        name: str | None = None,
        path: str | None = None,
        options: dict | None = None,
        option_schema: dict | None = None,
    ) -> Engine:
        self._ensure_loaded()
        engine = self.get(engine_id)
        if name is not None:
            engine.name = name
        if path is not None:
            engine.path = path
        if options is not None:
            engine.options = options
        if option_schema is not None:
            engine.option_schema = option_schema
        self._save()
        return engine

    def remove(self, engine_id: str) -> None:
        self._ensure_loaded()
        if engine_id not in self._engines:
            raise EngineNotFoundError(engine_id)
        del self._engines[engine_id]
        if self._selected_id == engine_id:
            self._selected_id = None
        self._save()

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()


def resolve_selected(
    registry: EngineRegistry, settings,
) -> tuple[str | None, str | None, dict | None]:
    """(path, display_name, options) for the active engine.

    Tries the registry's selected entry first; falls back to
    `settings.engine_path` (the legacy --engine flag). Returns
    (None, None, None) when nothing is configured. The name/options are
    None for the fallback path so HumanVsEngine derives a name from the
    UCI handshake on first launch.
    """
    if registry.selected_id:
        try:
            e = registry.get(registry.selected_id)
            return e.path, e.name, dict(e.options or {})
        except EngineNotFoundError:
            pass
    if settings.engine_path:
        return str(settings.engine_path), None, None
    return None, None, None
