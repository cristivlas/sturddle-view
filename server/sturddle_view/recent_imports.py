"""Server-side history of imported PGN/FEN texts.

Storage layout under ``<user_data_dir>/imports/``::

    imports/
      index.json                # {hash: {format, summary, ts, file, game_id, refs}}
      by-hash/
        <sha256>.pgn             # raw text of the imported PGN (or .fen)

``hash`` is the SHA-256 hex of the trimmed text. The blob is content-
addressed so re-importing the same text dedupes naturally.

Each row carries a ``game_id`` (server-assigned, opaque string) and a
``refs`` list of game_ids referencing this row. ``refs`` is reserved
for cross-game annotations; populated as ``[]`` today.

Mutations go through ``RecentImports.save`` / ``.touch`` / ``.remove``
which serialize on an asyncio lock; reads (``list``, ``get``,
``get_by_id``) take the lock briefly to snapshot the in-memory index.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Callable

from platformdirs import user_data_dir

from . import APP_NAME
from ._atomic import atomic_write_json, atomic_write_text
from .play.canonical_hash import canonical_hash

log = logging.getLogger(__name__)

DEFAULT_CAP = 50


def default_imports_dir() -> Path:
    """Directory for the imports store. ``SV_IMPORTS_DIR`` overrides."""
    override = os.environ.get("SV_IMPORTS_DIR")
    if override:
        return Path(override)
    return Path(user_data_dir(APP_NAME, appauthor=False)) / "imports"


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ext_for(fmt: str) -> str:
    return "fen" if fmt == "fen" else "pgn"


# Getter contract: returns the active HVE session's current game_id or
# None. Must not raise -- eviction calls it inline and does not guard
# against exceptions. The standard wiring (a lambda over
# ``getattr(app.state.hve, "game_id", None)``) trivially satisfies this.
ActiveGameIdGetter = Callable[[], "str | None"]


class RecentImports:
    """In-memory index of recent imports, persisted to disk.

    Construct via :meth:`load`. Mutating methods are async and acquire
    an internal lock so concurrent requests serialize cleanly. Read
    methods are synchronous and return shallow copies of the index
    rows; they assume single-threaded access (which the FastAPI server
    provides per event loop).

    ``active_game_id`` is an optional getter returning the active HVE
    session's current game_id (or None). The store consults it during
    eviction so the live session's row is never dropped.
    """

    def __init__(
        self,
        root: Path,
        cap: int = DEFAULT_CAP,
        active_game_id: ActiveGameIdGetter | None = None,
    ) -> None:
        self._root = root
        self._index_path = root / "index.json"
        self._blob_dir = root / "by-hash"
        self._cap = cap
        # hash -> {format, summary, ts, file, game_id, refs}
        self._index: dict[str, dict] = {}
        # game_id -> hash, rebuilt from _index after load and kept in
        # sync with every save/remove/evict.
        self._by_id: dict[str, str] = {}
        self._active_game_id = active_game_id
        self._lock = asyncio.Lock()

    @property
    def root(self) -> Path:
        return self._root

    def set_active_game_id_getter(self, getter: ActiveGameIdGetter | None) -> None:
        """Install/replace the active-session getter post-construction.

        Useful when the HVE singleton is created lazily (after app
        startup) and the store was built first."""
        self._active_game_id = getter

    @classmethod
    def load(
        cls,
        root: Path | None = None,
        cap: int = DEFAULT_CAP,
        active_game_id: ActiveGameIdGetter | None = None,
    ) -> "RecentImports":
        root = root or default_imports_dir()
        inst = cls(root, cap=cap, active_game_id=active_game_id)
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
                    # Backfill missing fields so older on-disk indexes
                    # (pre-game_id) load cleanly. New imports will be
                    # written with both fields populated.
                    row.setdefault("game_id", None)
                    row.setdefault("refs", [])
                    cleaned[h] = row
                inst._index = cleaned
                inst._rebuild_by_id_locked()
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError):
            log.exception("recent-imports index unreadable; starting empty")
        return inst

    # ---- read paths (sync) ----

    def list(self) -> list[dict]:
        """Return all index rows sorted by ts desc. Each row carries the
        hash plus its metadata. Cap enforcement is in eviction; pinned
        rows (active session, non-empty refs) may push the total past
        the cap."""
        rows = [{"hash": h, **v} for h, v in self._index.items()]
        rows.sort(key=lambda r: r.get("ts", 0), reverse=True)
        return rows

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

    def get_by_id(self, game_id: str) -> tuple[dict, str] | None:
        """Resolve a ``game_id`` to its row + text. Returns None for
        unknown / evicted ids."""
        h = self._by_id.get(game_id)
        if h is None:
            return None
        return self.get(h)

    def hash_for_id(self, game_id: str) -> str | None:
        """Cheap reverse lookup without touching the blob."""
        return self._by_id.get(game_id)

    # ---- write paths (async) ----

    async def save(
        self,
        fmt: str,
        text: str,
        summary: dict,
        game_id: str | None = None,
        precomputed_hash: str | None = None,
    ) -> str:
        """Upsert an entry for ``text``. Writes the blob if new, updates the
        index, evicts oldest entries past the cap. Returns the hash.

        ``game_id`` is the server-assigned identity for the *content*:
        - First save of a hash: the row stores the supplied id (mints
          one if not supplied, though normal call sites always pass it).
        - Re-save of the same hash: first-save-wins. The original
          ``game_id`` is preserved; a different supplied id is logged
          and orphaned (no store row).
        - Collision: supplied id matches an existing reverse-index
          entry but for a *different* hash -> programming bug, assert.
        """
        trimmed = text.strip()
        h = precomputed_hash if precomputed_hash is not None else canonical_hash(trimmed, fmt)
        async with self._lock:
            existing = self._index.get(h)
            if existing is not None:
                # First save wins. Keep original game_id; warn if the
                # caller passed a different one (hash collision across
                # game_ids -- normal play should not trigger this).
                stored_id = existing.get("game_id")
                if (
                    game_id is not None
                    and stored_id is not None
                    and game_id != stored_id
                ):
                    log.warning(
                        "recent-imports hash collision: hash=%s "
                        "stored_game_id=%s incoming_game_id=%s; "
                        "keeping stored id",
                        h, stored_id, game_id,
                    )
                # Bump ts + refresh summary on re-save (existing behavior).
                # Both "game_id" and "refs" keys are guaranteed present
                # by load()'s setdefault backfill, so no key-existence
                # check is needed here.
                existing["summary"] = summary
                existing["ts"] = int(time.time() * 1000)
                # Backfill game_id if missing (legacy row from pre-Phase-1).
                if stored_id is None and game_id is not None:
                    self._bind_id_locked(game_id, h, existing)
                self._persist_locked()
                return h

            # New row.
            fname = f"by-hash/{h}.{_ext_for(fmt)}"
            blob_path = self._root / fname
            if not blob_path.exists():
                atomic_write_text(blob_path, trimmed)
            row = {
                "format": fmt,
                "summary": summary,
                "ts": int(time.time() * 1000),
                "file": fname,
                "game_id": None,
                "refs": [],
            }
            self._index[h] = row
            if game_id is not None:
                self._bind_id_locked(game_id, h, row)
            self._evict_locked()
            self._persist_locked()
        return h

    def _bind_id_locked(self, game_id: str, h: str, row: dict) -> None:
        """Attach ``game_id`` to ``row`` (hash=``h``) and the reverse
        index. Asserts the id is free (or already bound to this hash).
        Must hold the lock."""
        self._assert_id_free_or_self(game_id, h)
        row["game_id"] = game_id
        self._by_id[game_id] = h

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
            gid = row.get("game_id")
            if gid is not None:
                self._by_id.pop(gid, None)
            self._delete_blob(row.get("file"))
            self._persist_locked()
            return True

    # ---- internals (must hold the lock) ----

    def _assert_id_free_or_self(self, game_id: str, h: str) -> None:
        """Programming-bug check: ``game_id`` must not already map to a
        *different* hash. uuid4 collision is ~2^-122; observing one
        means the RNG is broken or id-generation is. Crash loudly."""
        existing_hash = self._by_id.get(game_id)
        if existing_hash is not None and existing_hash != h:
            raise AssertionError(
                f"game_id collision: {game_id} already maps to "
                f"hash={existing_hash}, refused new mapping to hash={h}"
            )

    def _evict_locked(self) -> None:
        """Drop oldest rows past the cap. Skip rows that are
        active-session-pinned (matches the live HVE's game_id) or have
        non-empty ``refs``. If every eviction candidate is pinned, the
        store grows past cap and WARNs; cap is a soft floor.

        Eviction candidates are the rows older than the cap-th newest;
        the newest ``cap`` rows are never considered. This keeps a
        just-added row safe even when every older row is pinned."""
        if len(self._index) <= self._cap:
            return
        getter = self._active_game_id
        active_gid = getter() if getter is not None else None
        # ts desc; the first ``cap`` are kept regardless; the rest are
        # eviction candidates (oldest first).
        ranked = sorted(
            self._index.items(),
            key=lambda kv: kv[1].get("ts", 0),
            reverse=True,
        )
        candidates = list(reversed(ranked[self._cap:]))
        drop_target = len(candidates)
        dropped = 0
        for h, row in candidates:
            if row.get("refs"):
                continue
            if active_gid is not None and row.get("game_id") == active_gid:
                continue
            self._index.pop(h, None)
            gid = row.get("game_id")
            if gid is not None:
                self._by_id.pop(gid, None)
            self._delete_blob(row.get("file"))
            dropped += 1
        if dropped < drop_target:
            log.warning(
                "recent-imports cap (%d) exceeded: %d rows after "
                "eviction; remaining rows are pinned (active session "
                "or non-empty refs)",
                self._cap, len(self._index),
            )

    def _delete_blob(self, rel: str | None) -> None:
        if not rel:
            return
        try:
            (self._root / rel).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("could not delete recent-import blob %s", rel)

    def _rebuild_by_id_locked(self) -> None:
        """Rebuild the game_id -> hash reverse index from ``self._index``.
        Called from :meth:`load`."""
        by_id: dict[str, str] = {}
        for h, row in self._index.items():
            gid = row.get("game_id")
            if gid is None:
                continue
            if gid in by_id:
                # Two rows on disk claim the same game_id. Programming
                # bug per the uniqueness contract; surface and drop the
                # later one's mapping so the index stays usable.
                log.error(
                    "duplicate game_id %s on load: hash=%s also claims it; "
                    "keeping first mapping (hash=%s)",
                    gid, h, by_id[gid],
                )
                continue
            by_id[gid] = h
        self._by_id = by_id

    def _persist_locked(self) -> None:
        atomic_write_json(self._index_path, self._index)
