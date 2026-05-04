"""Atomic JSON-on-disk writes used by the registry/settings/game-state stores."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, payload: Any, *, indent: int | None = None) -> None:
    """Write JSON via tempfile + os.replace.

    Tempfile lives in the same directory as ``path`` so the rename is atomic
    (cross-filesystem renames aren't). Creates parent dirs; on failure unlinks
    the temp file and re-raises.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=indent)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
