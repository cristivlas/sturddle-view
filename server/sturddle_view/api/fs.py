"""Server filesystem browse endpoint.

Lets the client enumerate directories so the user can pick paths (engine
binaries, PGN dirs, etc.) without typing them by hand. Cross-platform; uses
pathlib only.

Access is gated by the same token as other endpoints. Permissions are whatever
the server process has — we don't impose an additional allowlist.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import require_token

router = APIRouter(prefix="/fs", tags=["fs"], dependencies=[Depends(require_token)])


@dataclass
class Entry:
    name: str
    path: str
    is_dir: bool
    is_file: bool
    is_executable: bool
    size: int | None
    mtime: float | None
    error: str | None = None


def _home() -> Path:
    return Path.home()


_DEFAULT_WIN_PATHEXT = ".COM;.EXE;.BAT;.CMD"


def _is_executable(p: Path, is_file: bool) -> bool:
    """Cross-platform executability check.

    POSIX: honor the X bit via ``os.access``. Windows: ``os.access(X_OK)``
    returns True for any readable file, so use the suffix-against-PATHEXT
    test that the shell itself uses to decide what counts as a program.
    """
    if not is_file:
        return False
    if sys.platform.startswith("win"):
        exts = os.environ.get("PATHEXT", _DEFAULT_WIN_PATHEXT).split(";")
        wanted = {e.strip().lower() for e in exts if e.strip()}
        return p.suffix.lower() in wanted
    return os.access(p, os.X_OK)


def _entry_for(p: Path) -> dict:
    try:
        st = p.stat()
        is_dir = p.is_dir()
        is_file = p.is_file()
        is_exec = _is_executable(p, is_file)
        size = st.st_size if is_file else None
        return {
            "name": p.name or str(p),
            "path": str(p),
            "is_dir": is_dir,
            "is_file": is_file,
            "is_executable": is_exec,
            "size": size,
            "mtime": st.st_mtime,
        }
    except OSError as e:
        return {
            "name": p.name or str(p),
            "path": str(p),
            "is_dir": False,
            "is_file": False,
            "is_executable": False,
            "size": None,
            "mtime": None,
            "error": str(e),
        }


def _windows_drives() -> list[str]:
    """Return available drive letters (e.g. ['C:\\', 'D:\\']) on Windows."""
    drives = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        if Path(root).exists():
            drives.append(root)
    return drives


@router.get("")
def list_dir(
    path: str | None = Query(default=None, description="Directory to list (default: home)"),
    show_hidden: bool = Query(default=False),
) -> dict:
    """List the contents of a directory.

    `path=""` or `"/"` on Windows returns the list of drive roots.
    """
    if path is None:
        target = _home()
    elif sys.platform.startswith("win") and (not path or path == "/"):
        return {
            "path": "",
            "parent": None,
            "is_root": True,
            "entries": [
                {"name": d, "path": d, "is_dir": True, "is_file": False,
                 "is_executable": False, "size": None, "mtime": None}
                for d in _windows_drives()
            ],
        }
    else:
        target = Path(path).expanduser()

    try:
        target = target.resolve(strict=False)
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"bad path: {e}") from e

    if not target.exists():
        raise HTTPException(status_code=404, detail=f"not found: {target}")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail=f"not a directory: {target}")

    try:
        def _sort_key(p: Path) -> tuple:
            try:
                return (not p.is_dir(), p.name.lower())
            except OSError:
                return (True, p.name.lower())

        children = sorted(target.iterdir(), key=_sort_key)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e

    entries = []
    for c in children:
        if not show_hidden and c.name.startswith("."):
            continue
        entries.append(_entry_for(c))

    parent = str(target.parent) if target.parent != target else None
    is_root = parent is None or (sys.platform.startswith("win") and len(target.parts) == 1)

    return {
        "path": str(target),
        "parent": parent,
        "is_root": is_root,
        "entries": entries,
    }


@router.get("/stat")
def stat_path(path: str = Query(...)) -> dict:
    """Stat a single path. Used for client-side validation."""
    p = Path(path).expanduser()
    try:
        p = p.resolve(strict=False)
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"bad path: {e}") from e
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"not found: {p}")
    return _entry_for(p)
