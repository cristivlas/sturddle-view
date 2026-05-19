"""Server-side history of imported PGN/FEN texts.

Storage layout under ``<user_data_dir>/imports/``::

    imports/
      index.json                # {hash: {format, summary, ts, file}}
      by-hash/
        <sha256>.pgn             # raw text of the imported PGN (or .fen)

``hash`` is the SHA-256 hex of the trimmed text. The blob is content-
addressed so re-importing the same text dedupes naturally.

Mutations go through ``RecentImports.save`` / ``.touch`` / ``.remove``
which serialize on an asyncio lock; reads (``list``, ``get``) take the
lock briefly to snapshot the in-memory index.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path

from platformdirs import user_data_dir

from . import APP_NAME
from ._atomic import atomic_write_json, atomic_write_text
from .play.canonical_hash import canonical_hash

log = logging.getLogger(__name__)

DEFAULT_CAP = 50


def default_imports_dir() -> Path:
    return Path(user_data_dir(APP_NAME, appauthor=False)) / "imports"


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ext_for(fmt: str) -> str:
    return "fen" if fmt == "fen" else "pgn"


class RecentImports:
    """In-memory index of recent imports, persisted to disk.

    Construct via :meth:`load`. Mutating methods are async and acquire
    an internal lock so concurrent requests serialize cleanly. Read
    methods are synchronous and return shallow copies of the index
    rows; they assume single-threaded access (which the FastAPI server
    provides per event loop).
    """

    def __init__(self, root: Path, cap: int = DEFAULT_CAP) -> None:
        self._root = root
        self._index_path = root / "index.json"
        self._blob_dir = root / "by-hash"
        self._cap = cap
        # hash -> {format, summary, ts, file}
        self._index: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    @property
    def root(self) -> Path:
        return self._root

    @classmethod
    def load(cls, root: Path | None = None, cap: int = DEFAULT_CAP) -> "RecentImports":
        root = root or default_imports_dir()
        inst = cls(root, cap=cap)
        try:
            raw = json.loads(inst._index_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                # Filter to entries that still have a blob on disk. An
                # orphaned index row (blob deleted out-of-band) would
                # raise on every GET; drop it lazily here.
                cleaned: dict[str, dict] = {}
                for h, row in raw.items():
                    if not isinstance(row, dict):
                        continue
                    fname = row.get("file")
                    if not fname:
                        continue
                    if not (root / fname).exists():
                        continue
                    cleaned[h] = row
                inst._index = cleaned
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError):
            log.exception("recent-imports index unreadable; starting empty")
        return inst

    # ---- read paths (sync) ----

    def list(self) -> list[dict]:
        """Return index rows sorted by ts desc, capped to the configured cap.
        Each row carries the hash plus its metadata."""
        rows = [{"hash": h, **v} for h, v in self._index.items()]
        rows.sort(key=lambda r: r.get("ts", 0), reverse=True)
        return rows[: self._cap]

    def get(self, h: str) -> tuple[dict, str] | None:
        """Return (row, text) for ``h`` or None if not present. The text is
        read from the blob on disk. Orphaned index rows (blob deleted
        out-of-band) return None and are cleaned up at next load()."""
        row = self._index.get(h)
        if row is None:
            return None
        try:
            text = (self._root / row["file"]).read_text(encoding="utf-8")
        except OSError:
            log.warning("recent-imports blob missing for %s", h)
            return None
        return dict(row), text

    # ---- write paths (async) ----

    async def save(self, fmt: str, text: str, summary: dict) -> str:
        """Upsert an entry for ``text``. Writes the blob if new, updates the
        index, evicts oldest entries past the cap. Returns the hash."""
        trimmed = text.strip()
        h = canonical_hash(trimmed, fmt)
        async with self._lock:
            fname = f"by-hash/{h}.{_ext_for(fmt)}"
            blob_path = self._root / fname
            if not blob_path.exists():
                atomic_write_text(blob_path, trimmed)
            self._index[h] = {
                "format": fmt,
                "summary": summary,
                "ts": int(time.time() * 1000),
                "file": fname,
            }
            self._evict_locked()
            self._persist_locked()
        return h

    async def touch(self, h: str) -> None:
        """Bump ``ts`` for ``h`` so frequently revisited entries don't fall
        off when eviction runs. No-op if ``h`` isn't present."""
        async with self._lock:
            row = self._index.get(h)
            if row is None:
                return
            row["ts"] = int(time.time() * 1000)
            self._persist_locked()

    async def remove(self, h: str) -> bool:
        """Delete ``h`` (index row + blob). Returns True if anything was
        removed."""
        async with self._lock:
            row = self._index.pop(h, None)
            if row is None:
                return False
            self._delete_blob(row.get("file"))
            self._persist_locked()
            return True

    # ---- internals (must hold the lock) ----

    def _evict_locked(self) -> None:
        if len(self._index) <= self._cap:
            return
        rows = sorted(self._index.items(), key=lambda kv: kv[1].get("ts", 0))
        drop = len(self._index) - self._cap
        for h, row in rows[:drop]:
            self._index.pop(h, None)
            self._delete_blob(row.get("file"))

    def _delete_blob(self, rel: str | None) -> None:
        if not rel:
            return
        try:
            (self._root / rel).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("could not delete recent-import blob %s", rel)

    def _persist_locked(self) -> None:
        atomic_write_json(self._index_path, self._index)
