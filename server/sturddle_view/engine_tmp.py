"""Managed temp dirs for engine subprocesses.

Self-extracting engine builds unpack into the OS temp dir at startup;
a crash or kill orphans those files. Engines are spawned with
TMP/TEMP/TMPDIR pointed at a per-spawn dir under a root we own, so
cleanup is always possible: on engine exit, and via a startup sweep
for anything a dead server left behind.
See docs/engine-temp-cleanup-spec.md.
"""
from __future__ import annotations

import logging
import os
import secrets
import shutil
from pathlib import Path

from platformdirs import user_data_dir

from . import app_dir_name

log = logging.getLogger(__name__)

_ROOT_ENV = "SV_ENGINE_TMP_ROOT"
_ROOT_DIRNAME = "engine-tmp"
_SPAWN_TOKEN_BYTES = 4

# Windows honors TMP/TEMP; POSIX honors TMPDIR. Set all three so one
# spawn path covers both platforms.
TEMP_ENV_VARS = ("TMP", "TEMP", "TMPDIR")


def engine_tmp_root() -> Path:
    """Managed root for per-spawn engine temp dirs.

    ``SV_ENGINE_TMP_ROOT`` overrides the default (tests, isolated
    deployments). Defaults to platform user-data dir."""
    override = os.environ.get(_ROOT_ENV)
    if override:
        return Path(override)
    return Path(user_data_dir(app_dir_name(), appauthor=False)) / _ROOT_DIRNAME


def create_spawn_dir() -> Path:
    """Fresh per-spawn dir ``<root>/<pid>-<token>/``. The pid is ours
    (the child's is unknown pre-spawn); the token disambiguates
    concurrent spawns from one server process."""
    d = engine_tmp_root() / f"{os.getpid()}-{secrets.token_hex(_SPAWN_TOKEN_BYTES)}"
    d.mkdir(parents=True, exist_ok=False)
    return d


def temp_env(spawn_dir: Path) -> dict[str, str]:
    """Env overlay pointing all temp-dir conventions at *spawn_dir*."""
    return {name: str(spawn_dir) for name in TEMP_ENV_VARS}


def cleanup_spawn_dir(spawn_dir: Path | None) -> None:
    """Best-effort tree removal; failures (e.g. files still locked on
    Windows) are logged and retried by the next startup sweep."""
    if spawn_dir is None:
        return
    try:
        shutil.rmtree(spawn_dir)
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("could not remove engine temp dir %s: %s", spawn_dir, e)


def sweep_orphans() -> int:
    """Delete every subdir under the managed root; return the count.

    Startup-only: the single-instance lock guarantees no engine spawned
    by a previous run can still be alive."""
    root = engine_tmp_root()
    if not root.is_dir():
        return 0
    removed = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            shutil.rmtree(child)
            removed += 1
        except OSError as e:
            log.warning("orphan sweep: could not remove %s: %s", child, e)
    return removed
