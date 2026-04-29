"""Engine registry: persistent list of UCI engines the user has registered.

Storage: a single JSON file under the OS-appropriate user config dir
(`platformdirs.user_config_dir("sturddle-view")`).

The registry is in-memory once loaded; mutations are written back atomically.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from platformdirs import user_config_dir


def default_registry_path() -> Path:
    return Path(user_config_dir("sturddle-view")) / "engines.json"


@dataclass
class Engine:
    id: str
    name: str
    path: str
    options: dict[str, str | int | bool] = field(default_factory=dict)

    @staticmethod
    def new(name: str, path: str, options: dict | None = None) -> "Engine":
        return Engine(id=uuid.uuid4().hex[:12], name=name, path=path, options=options or {})


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
            )
            engines[e.id] = e
        self._engines = engines
        self._selected_id = data.get("selected_id")
        if self._selected_id and self._selected_id not in self._engines:
            self._selected_id = None
        self._loaded = True

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "engines": [asdict(e) for e in self._engines.values()],
            "selected_id": self._selected_id,
        }
        # Atomic write: tmp file in same dir, then replace.
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".engines.", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def list(self) -> list[Engine]:
        self._ensure_loaded()
        return list(self._engines.values())

    def get(self, engine_id: str) -> Engine:
        self._ensure_loaded()
        try:
            return self._engines[engine_id]
        except KeyError as e:
            raise EngineNotFoundError(engine_id) from e

    def add(self, name: str, path: str, options: dict | None = None) -> Engine:
        self._ensure_loaded()
        # Treat (name, path) pair as the uniqueness key. Same binary at the same
        # path with the same display name is a duplicate; same binary with two
        # different names (e.g. different UCI options) is allowed.
        for e in self._engines.values():
            if e.name == name and e.path == path:
                raise DuplicateEngineError(f"engine already registered: {name} ({path})")
        engine = Engine.new(name=name, path=path, options=options)
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
    ) -> Engine:
        self._ensure_loaded()
        engine = self.get(engine_id)
        if name is not None:
            engine.name = name
        if path is not None:
            engine.path = path
        if options is not None:
            engine.options = options
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
