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
    _atomic_write(path, lambda f: json.dump(payload, f, indent=indent))


def atomic_write_text(path: Path, text: str) -> None:
    """Write a UTF-8 text payload via tempfile + os.replace. Same guarantees
    as ``atomic_write_json`` — readers see the old file or the fully-written
    new one, never a partial write."""
    _atomic_write(path, lambda f: f.write(text))


def _atomic_write(path: Path, writer) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            writer(f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
