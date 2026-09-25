"""Server filesystem browse endpoint.

Lets the client enumerate directories so the user can pick paths (engine
binaries, PGN dirs, etc.) without typing them by hand. Cross-platform; uses
pathlib only.

Access is gated by the same token as other endpoints. Permissions are whatever
the server process has -- we don't impose an additional allowlist.
"""
from __future__ import annotations

import os
import string
from dataclasses import asdict, dataclass
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status

from .. import is_windows
from ..auth import require_token
from ._http import bad_request, not_found

router = APIRouter(prefix="/fs", tags=["fs"], dependencies=[Depends(require_token)])

_DEFAULT_WIN_PATHEXT = ".COM;.EXE;.BAT;.CMD"
_PATHEXT_SEP = ";"
# Windows path the client sends to ask for the list of drive roots.
_DRIVES_PATH = "/"
_HIDDEN_PREFIX = "."


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


def _is_executable(p: Path, is_file: bool) -> bool:
    """Cross-platform executability check.

    POSIX: honor the X bit via ``os.access``. Windows: ``os.access(X_OK)``
    returns True for any readable file, so use the suffix-against-PATHEXT
    test that the shell itself uses to decide what counts as a program.
    """
    if not is_file:
        return False
    if is_windows():
        exts = os.environ.get("PATHEXT", _DEFAULT_WIN_PATHEXT).split(_PATHEXT_SEP)
        wanted = {e.strip().lower() for e in exts if e.strip()}
        return p.suffix.lower() in wanted
    return os.access(p, os.X_OK)


def _entry_for(p: Path) -> dict:
    name = p.name or str(p)
    try:
        st = p.stat()
        is_file = p.is_file()
        entry = Entry(
            name=name,
            path=str(p),
            is_dir=p.is_dir(),
            is_file=is_file,
            is_executable=_is_executable(p, is_file),
            size=st.st_size if is_file else None,
            mtime=st.st_mtime,
        )
    except OSError as e:
        entry = Entry(
            name=name, path=str(p), is_dir=False, is_file=False,
            is_executable=False, size=None, mtime=None, error=str(e),
        )
    return asdict(entry)


def _windows_drives() -> list[str]:
    """Return available drive letters (e.g. ['C:\\', 'D:\\']) on Windows."""
    drives = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if Path(root).exists():
            drives.append(root)
    return drives


def _drive_entry(root: str) -> dict:
    return asdict(Entry(
        name=root, path=root, is_dir=True, is_file=False,
        is_executable=False, size=None, mtime=None,
    ))


def _listing(path: str, parent: str | None, is_root: bool, entries: list[dict]) -> dict:
    return {"path": path, "parent": parent, "is_root": is_root, "entries": entries}


def _resolved_existing(raw: Path) -> Path:
    try:
        p = raw.resolve(strict=False)
    except OSError as e:
        raise bad_request(f"bad path: {e}") from e
    if not p.exists():
        raise not_found(f"not found: {p}")
    return p


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
    elif is_windows() and (not path or path == _DRIVES_PATH):
        return _listing("", None, True, [_drive_entry(d) for d in _windows_drives()])
    else:
        target = Path(path).expanduser()

    target = _resolved_existing(target)
    if not target.is_dir():
        raise bad_request(f"not a directory: {target}")

    try:
        def _sort_key(p: Path) -> tuple:
            try:
                return (not p.is_dir(), p.name.lower())
            except OSError:
                return (True, p.name.lower())

        children = sorted(target.iterdir(), key=_sort_key)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e)) from e

    entries = [
        _entry_for(c) for c in children
        if show_hidden or not c.name.startswith(_HIDDEN_PREFIX)
    ]
    parent = str(target.parent) if target.parent != target else None
    is_root = parent is None or (is_windows() and len(target.parts) == 1)
    return _listing(str(target), parent, is_root, entries)


@router.get("/stat")
def stat_path(path: str = Query(...)) -> dict:
    """Stat a single path. Used for client-side validation."""
    return _entry_for(_resolved_existing(Path(path).expanduser()))
