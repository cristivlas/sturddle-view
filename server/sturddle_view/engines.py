"""Engine registry: persistent list of UCI engines the user has registered.

Storage: a single JSON file under the OS-appropriate user config dir
(`platformdirs.user_config_dir("sturddle-view")`).

The registry is in-memory once loaded; mutations are written back atomically.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
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


def _classify_probe_exception(exc: BaseException) -> dict:
    """Map a spawn/handshake exception to a {code, message} pair, cross-platform.

    Classification is by exception type so the caller never has to parse
    platform-specific text like "WinError 193" or "Exec format error" --
    both reach us as plain OSError.
    """
    if isinstance(exc, FileNotFoundError):
        return {"code": "engine_path_not_found",
                "message": "Engine file not found."}
    if isinstance(exc, PermissionError):
        return {"code": "engine_permission_denied",
                "message": "Permission denied launching the engine."}
    if isinstance(exc, chess.engine.EngineError):
        return {"code": "engine_not_uci",
                "message": "Engine did not respond as a UCI engine."}
    if isinstance(exc, OSError):
        return {"code": "engine_not_launchable",
                "message": "Could not launch engine (file is not a runnable program for this system)."}
    return {"code": "engine_probe_failed",
            "message": "Could not probe engine."}


async def probe_engine(
    engine_path: str,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> tuple[str | None, dict[str, dict], dict | None]:
    """Briefly spawn the engine; return (uci_id_name, option_schema, error).

    `args` and `env` mirror the launch settings stored on the engine — we
    probe with the same launch profile that will run the engine in earnest,
    so option discovery reflects flags / env vars that gate UCI options.
    `env` is overlaid on top of the parent process environment (the user
    can override or add, never wipe inherited vars).

    `option_schema` is a {name: {type, default, min?, max?, vars?}} dict,
    skipping engine-managed options (multipv, ponder, etc.). `uci_id_name`
    is what the engine announces via UCI `id name`, or None if unavailable.
    `error` is None on success, or a structured ``{code, message}`` dict
    with a short user-facing reason -- callers surface ``message`` to the
    UI directly. Classification is by exception type, not platform text.
    Best-effort: on any failure logs and returns (None, {}, error).
    """
    command: str | list[str] = [engine_path, *args] if args else engine_path
    popen_kwargs: dict = {}
    if env:
        popen_kwargs["env"] = {**os.environ, **env}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        transport, engine = await chess.engine.popen_uci(command, **popen_kwargs)
    except Exception as e:
        err = _classify_probe_exception(e)
        # Classified failures are routine: legacy broken entries get re-probed
        # on every GET /engines, full tracebacks just spam the log. Unknown
        # exception types stay at ERROR -- those are real bug signals.
        if err["code"] == "engine_probe_failed":
            log.exception("could not spawn %s for probe", engine_path)
        else:
            log.warning("probe failed for %s: %s", engine_path, err["message"])
        return None, {}, err
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
    # Extra command-line argv passed to the engine on launch. One literal
    # argv element per list entry — no shell parsing.
    args: list[str] = field(default_factory=list)
    # Per-engine environment overrides. Overlaid on top of the parent
    # process env at spawn time (parent env is always inherited).
    env: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def new(
        name: str,
        path: str,
        options: dict | None = None,
        option_schema: dict | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> "Engine":
        return Engine(
            id=uuid.uuid4().hex[:12],
            name=name,
            path=path,
            options=options or {},
            option_schema=option_schema or {},
            args=list(args or []),
            env=dict(env or {}),
        )


class EngineNotFoundError(KeyError):
    pass


class DuplicateEngineError(ValueError):
    pass


class InvalidLaunchProfileError(ValueError):
    """Raised when ``args``/``env`` would be unspawnable (NUL bytes, blank
    or ``=``-bearing env keys, non-string values).

    Enforced at the registry boundary so a hand-edited ``engines.json``
    can't ship malformed values that only blow up at probe/spawn time.
    """


_ENV_KEY_FORBIDDEN_CHARS = ("=", "\x00", "\n", "\r")


def validate_launch_profile(
    args: list[str] | None, env: dict[str, str] | None,
) -> None:
    """Raise ``InvalidLaunchProfileError`` if args/env are unspawnable.

    Same checks as the API layer, hoisted here so direct registry use
    (CLI, tests, manual edits via ``add``/``update``) can't bypass them.
    """
    if args is not None:
        for a in args:
            if not isinstance(a, str):
                raise InvalidLaunchProfileError("each arg must be a string")
            if "\x00" in a:
                raise InvalidLaunchProfileError("arg contains NUL")
    if env is not None:
        for k, v in env.items():
            if not isinstance(k, str) or not k:
                raise InvalidLaunchProfileError(
                    "env key must be a non-empty string",
                )
            if any(c in k for c in _ENV_KEY_FORBIDDEN_CHARS):
                raise InvalidLaunchProfileError(
                    f"env key contains forbidden character: {k!r}",
                )
            if not isinstance(v, str):
                raise InvalidLaunchProfileError(
                    f"env value must be a string: {k}",
                )
            if "\x00" in v:
                raise InvalidLaunchProfileError(f"env value contains NUL: {k}")


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
        engines: dict[str, Engine] = {}
        seen: set[str] = set()
        mutated = False
        for entry in data.get("engines", []):
            name = entry["name"]
            unique = self._dedup_name(name, seen)
            if unique != name:
                mutated = True
            seen.add(unique.casefold())
            args = list(entry.get("args", []) or [])
            env = dict(entry.get("env", {}) or {})
            try:
                validate_launch_profile(args, env)
            except InvalidLaunchProfileError:
                # Don't refuse to load the whole registry over a bad
                # entry — drop the launch profile and keep the engine
                # otherwise usable. The user can re-edit via the dialog.
                log.warning(
                    "engine %s: invalid launch profile in registry; dropping args/env",
                    entry["id"],
                )
                args, env = [], {}
                mutated = True
            e = Engine(
                id=entry["id"],
                name=unique,
                path=entry["path"],
                options=entry.get("options", {}),
                option_schema=entry.get("option_schema", {}),
                args=args,
                env=env,
            )
            engines[e.id] = e
        self._engines = engines
        self._selected_id = data.get("selected_id")
        if self._selected_id and self._selected_id not in self._engines:
            self._selected_id = None
        self._loaded = True
        if mutated:
            self._save()

    @staticmethod
    def _dedup_name(desired: str, taken: set[str]) -> str:
        if desired.casefold() not in taken:
            return desired
        n = 2
        while f"{desired} ({n})".casefold() in taken:
            n += 1
        return f"{desired} ({n})"

    def _unique_name(self, desired: str, exclude_id: str | None = None) -> str:
        taken = {
            e.name.casefold()
            for eid, e in self._engines.items()
            if eid != exclude_id
        }
        return self._dedup_name(desired, taken)

    def _name_in_use(self, name: str, exclude_id: str | None = None) -> bool:
        cf = name.casefold()
        return any(
            e.name.casefold() == cf
            for eid, e in self._engines.items()
            if eid != exclude_id
        )

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
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        auto_suffix: bool = False,
    ) -> Engine:
        self._ensure_loaded()
        validate_launch_profile(args, env)
        # Names are unique (case-insensitive). When the caller derived the
        # name (UCI id / basename), auto-suffix on collision; when the user
        # supplied it, raise so they can pick a different one.
        if self._name_in_use(name):
            if not auto_suffix:
                raise DuplicateEngineError(f"engine name already in use: {name}")
            name = self._unique_name(name)
        engine = Engine.new(
            name=name,
            path=path,
            options=options,
            option_schema=option_schema,
            args=args,
            env=env,
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
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> Engine:
        self._ensure_loaded()
        validate_launch_profile(args, env)
        engine = self.get(engine_id)
        if name is not None:
            if name != engine.name and self._name_in_use(name, exclude_id=engine_id):
                raise DuplicateEngineError(f"engine name already in use: {name}")
            engine.name = name
        if path is not None:
            engine.path = path
        if options is not None:
            engine.options = options
        if option_schema is not None:
            engine.option_schema = option_schema
        if args is not None:
            engine.args = list(args)
        if env is not None:
            engine.env = dict(env)
        self._save()
        return engine

    def remove(self, engine_id: str) -> None:
        self._ensure_loaded()
        if engine_id not in self._engines:
            raise EngineNotFoundError(engine_id)
        del self._engines[engine_id]
        if self._selected_id == engine_id:
            self._selected_id = next(iter(self._engines), None)
        self._save()

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()


@dataclass
class ResolvedLaunch:
    """Launch profile for the active engine.

    `name` and `options` are None when resolved via the legacy
    ``settings.engine_path`` fallback (no registry entry). `path` is
    None when no engine is configured at all.
    """
    path: str | None
    name: str | None
    options: dict | None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


def resolve_selected(
    registry: EngineRegistry, settings,
) -> ResolvedLaunch:
    """Launch profile for the active engine.

    Tries the registry's selected entry first; falls back to
    `settings.engine_path` (the legacy --engine flag). Returns an empty
    profile when nothing is configured.
    """
    if registry.selected_id:
        try:
            e = registry.get(registry.selected_id)
            return ResolvedLaunch(
                path=e.path,
                name=e.name,
                options=dict(e.options or {}),
                args=list(e.args or []),
                env=dict(e.env or {}),
            )
        except EngineNotFoundError:
            pass
    if settings.engine_path:
        return ResolvedLaunch(path=str(settings.engine_path), name=None, options=None)
    return ResolvedLaunch(path=None, name=None, options=None)
