"""Single-instance guard via an exclusive file lock.

The OS releases the lock automatically on process exit or crash,
so no cleanup is needed and stale locks never block a restart.
"""
from __future__ import annotations

import sys
from pathlib import Path

_lock_fh = None  # keep file handle open for the process lifetime


def acquire(lock_path: Path) -> bool:
    """Return True if the lock was acquired, False if another instance holds it."""
    global _lock_fh
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("w")
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    _lock_fh = fh  # keep alive
    return True