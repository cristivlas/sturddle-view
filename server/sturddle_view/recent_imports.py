"""Server-side history of imported PGN/FEN texts.

Storage layout under ``<user_data_dir>/imports/``::

    imports/
      index.json                # {hash: {format, summary, ts, file,
                                #         game_id, refs,
                                #         parent_game_id?, fork_ply?}}
      by-hash/
        <sha256>.pgn             # raw text of the imported PGN (or .fen)

``hash`` is the SHA-256 hex of the trimmed text. The blob is content-
addressed so re-importing the same text dedupes naturally.

Each row carries a ``game_id`` (server-assigned, opaque string) and a
``refs`` list of dicts ``{game_id, fork_ply}`` referencing children
forked off this row.

Fork-link fields (optional, set only when the row is a child of another):

- ``parent_game_id``: the parent's game_id.
- ``fork_ply``: the ply in the parent at which this child was forked
  (>= 1; ply 0 forks are treated as plain new games, no link).

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
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from platformdirs import user_data_dir

from . import app_dir_name
from ._atomic import atomic_write_json, atomic_write_text
from .play.canonical_hash import canonical_hash

log = logging.getLogger(__name__)

DEFAULT_CAP = 50

# Row + ref field keys (no inline string literals at call sites).
ROW_GAME_ID = "game_id"
ROW_REFS = "refs"
ROW_PARENT_GAME_ID = "parent_game_id"
ROW_FORK_PLY = "fork_ply"
ROW_FORMAT = "format"
ROW_SUMMARY = "summary"
ROW_TS = "ts"
ROW_FILE = "file"

REF_GAME_ID = "game_id"
REF_FORK_PLY = "fork_ply"


class RemoveStatus(Enum):
    DELETED = "deleted"
    NOT_FOUND = "not_found"
    BLOCKED_BY_REFS = "blocked_by_refs"


@dataclass(frozen=True)
class RemoveResult:
    """Outcome of ``RecentImports.remove``.

    - ``DELETED``: row + blob gone; if the row had a ``parent_game_id``
      the matching entry was scrubbed from the parent's ``refs``.
    - ``NOT_FOUND``: no such hash.
    - ``BLOCKED_BY_REFS``: row has live children. ``children`` lists
      them as ``{game_id, fork_ply, summary}`` for caller-side messaging.
    """
    status: RemoveStatus
    children: list[dict] = field(default_factory=list)


def default_imports_dir() -> Path:
    """Directory for the imports store. ``SV_IMPORTS_DIR`` overrides."""
    override = os.environ.get("SV_IMPORTS_DIR")
    if override:
        return Path(override)
    return Path(user_data_dir(app_dir_name(), appauthor=False)) / "imports"


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
                    fname = row.get(ROW_FILE)
                    if not fname:
                        continue
                    if not (root / fname).exists():
                        continue
                    # Backfill missing fields so older on-disk indexes
                    # (pre-game_id) load cleanly. New imports will be
                    # written with both fields populated.
                    row.setdefault(ROW_GAME_ID, None)
                    row.setdefault(ROW_REFS, [])
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
        rows.sort(key=lambda r: r.get(ROW_TS, 0), reverse=True)
        return rows

    def get(self, h: str) -> tuple[dict, str] | None:
        """Return (row, text) for ``h`` or None if not present. The text is
        read from the blob on disk. Orphaned index rows (blob deleted
        out-of-band) return None and are cleaned up at next load()."""
        row = self._index.get(h)
        if row is None:
            return None
        try:
            text = (self._root / row[ROW_FILE]).read_text(encoding="utf-8")
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
        parent_game_id: str | None = None,
        fork_ply: int | None = None,
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

        Fork link (optional): pass ``parent_game_id`` AND ``fork_ply``
        together to mark this row as a child of an existing row. The
        parent's ``refs`` is appended with ``{game_id, fork_ply}`` in
        the same atomic persist. Asserts both-or-neither and
        ``fork_ply >= 1`` (ply-0 forks are plain new games, link-free).
        On re-save of an already-stored hash, the link is ignored
        (first-save-wins matches game_id semantics).
        """
        if (parent_game_id is None) != (fork_ply is None):
            raise AssertionError(
                "parent_game_id and fork_ply must be supplied together"
            )
        if fork_ply is not None and fork_ply < 1:
            raise AssertionError(
                f"fork_ply must be >= 1 (got {fork_ply}); "
                "ply-0 forks are not links"
            )
        trimmed = text.strip()
        h = precomputed_hash if precomputed_hash is not None else canonical_hash(trimmed, fmt)
        async with self._lock:
            existing = self._index.get(h)
            if existing is not None:
                # First save wins. Keep original game_id; warn if the
                # caller passed a different one (hash collision across
                # game_ids -- normal play should not trigger this).
                stored_id = existing.get(ROW_GAME_ID)
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
                existing[ROW_SUMMARY] = summary
                existing[ROW_TS] = int(time.time() * 1000)
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
            row: dict = {
                ROW_FORMAT: fmt,
                ROW_SUMMARY: summary,
                ROW_TS: int(time.time() * 1000),
                ROW_FILE: fname,
                ROW_GAME_ID: None,
                ROW_REFS: [],
            }
            if parent_game_id is not None:
                row[ROW_PARENT_GAME_ID] = parent_game_id
                row[ROW_FORK_PLY] = fork_ply
            self._index[h] = row
            if game_id is not None:
                self._bind_id_locked(game_id, h, row)
            # Append to parent's refs (atomic with child write).
            if parent_game_id is not None:
                self._append_parent_ref_locked(
                    parent_game_id, game_id, fork_ply,
                )
            self._evict_locked()
            self._persist_locked()
        return h

    async def replace_at(
        self,
        old_hash: str | None,
        fmt: str,
        text: str,
        summary: dict,
        game_id: str,
        precomputed_hash: str | None = None,
        parent_game_id: str | None = None,
        fork_ply: int | None = None,
    ) -> str:
        """Atomically swap content for a preserved ``game_id``.

        Under one lock acquisition:
        - If ``old_hash`` is given and present, evict that row + blob.
        - Insert/upsert the new ``(fmt, text, summary)`` row, binding
          ``game_id`` to the new hash.

        Returns the new hash.

        Use cases:
        - Annotation edit on a game already in recents: pass the
          pre-edit hash as ``old_hash``; the row is replaced in place
          with the same ``game_id``.
        - Annotation edit on a game NOT yet in recents (e.g. play ->
          view -> edit -> annotate flow): pass ``old_hash=None`` and
          the game is promoted to recents.

        ``old_hash == new_hash`` (content unchanged) collapses to a
        summary/ts refresh -- effectively the same as ``save`` upsert.

        ``old_hash`` present but bound to a DIFFERENT game_id is a
        programming bug and asserts (same uniqueness contract as
        ``_bind_id_locked``).

        ``new_hash`` already present with a different game_id (cross-
        game hash collision: ~2^-256, won't happen in practice) is
        logged loudly; the stored row's game_id is kept and the
        incoming binding is dropped. The old row at ``old_hash`` is
        still evicted, so the caller's game effectively vanishes from
        recents -- acceptable given the probability.

        Fork link (optional): pass ``parent_game_id`` AND ``fork_ply``
        together to attach a fresh link on the new row. Used by the
        play -> view -> edit -> annotate path where the child is being
        promoted into recents for the first time. Explicit values
        override any link preserved from the old row. The parent's
        ``refs`` is appended atomically. Asserts both-or-neither and
        ``fork_ply >= 1``.
        """
        if (parent_game_id is None) != (fork_ply is None):
            raise AssertionError(
                "parent_game_id and fork_ply must be supplied together"
            )
        if fork_ply is not None and fork_ply < 1:
            raise AssertionError(
                f"fork_ply must be >= 1 (got {fork_ply}); "
                "ply-0 forks are not links"
            )
        trimmed = text.strip()
        new_hash = precomputed_hash if precomputed_hash is not None else canonical_hash(trimmed, fmt)
        async with self._lock:
            # Capture fork-link fields from the old row so non-lossy
            # edits (the intent of replace_at) carry parent_game_id /
            # fork_ply across content changes. ``refs`` on the old row
            # also carry over since the children still point at this
            # game_id (their ``parent_game_id`` is unchanged).
            preserved_parent_id: str | None = None
            preserved_fork_ply: int | None = None
            preserved_refs: list[dict] = []
            if old_hash is not None:
                old_row = self._index.get(old_hash)
                if old_row is not None:
                    preserved_parent_id = old_row.get(ROW_PARENT_GAME_ID)
                    preserved_fork_ply = old_row.get(ROW_FORK_PLY)
                    preserved_refs = list(old_row.get(ROW_REFS) or [])

            # Explicit fork-link wins over preserved -- used when the
            # caller is promoting a previously-unsaved child into recents
            # for the first time. parent_ref_to_append is set only when
            # the explicit link is *new* (preserved == None), so we don't
            # double-append to the parent's refs when the link was just
            # carried across an in-place rewrite.
            parent_ref_to_append: tuple[str, int] | None = None
            if parent_game_id is not None:
                if preserved_parent_id is None:
                    parent_ref_to_append = (parent_game_id, fork_ply)
                preserved_parent_id = parent_game_id
                preserved_fork_ply = fork_ply

            if old_hash is not None and old_hash != new_hash:
                old_row = self._index.get(old_hash)
                if old_row is not None:
                    bound_id = old_row.get(ROW_GAME_ID)
                    if bound_id is not None and bound_id != game_id:
                        raise AssertionError(
                            f"replace_at refusing to evict hash={old_hash} "
                            f"bound to a different game_id={bound_id} "
                            f"(caller passed game_id={game_id})"
                        )
                    self._index.pop(old_hash, None)
                    if bound_id is not None:
                        self._by_id.pop(bound_id, None)
                    self._delete_blob(old_row.get(ROW_FILE))

            existing = self._index.get(new_hash)
            if existing is not None:
                # Already present (either old_hash == new_hash, or a
                # prior unrelated insert at this content). Refresh
                # summary/ts and rebind game_id when free.
                stored_id = existing.get(ROW_GAME_ID)
                if stored_id is not None and stored_id != game_id:
                    log.warning(
                        "recent-imports hash collision in replace_at: "
                        "hash=%s stored_game_id=%s incoming_game_id=%s; "
                        "keeping stored id",
                        new_hash, stored_id, game_id,
                    )
                else:
                    if stored_id is None:
                        self._bind_id_locked(game_id, new_hash, existing)
                existing[ROW_SUMMARY] = summary
                existing[ROW_TS] = int(time.time() * 1000)
                # Preserve fork-link on the surviving row when it wasn't
                # already set (old_hash == new_hash leaves it intact).
                if (
                    preserved_parent_id is not None
                    and ROW_PARENT_GAME_ID not in existing
                ):
                    existing[ROW_PARENT_GAME_ID] = preserved_parent_id
                    existing[ROW_FORK_PLY] = preserved_fork_ply
                if preserved_refs and not existing.get(ROW_REFS):
                    existing[ROW_REFS] = preserved_refs
                if parent_ref_to_append is not None:
                    self._append_parent_ref_locked(
                        parent_ref_to_append[0], game_id,
                        parent_ref_to_append[1],
                    )
                self._persist_locked()
                return new_hash

            # Fresh insert at new_hash.
            fname = f"by-hash/{new_hash}.{_ext_for(fmt)}"
            blob_path = self._root / fname
            if not blob_path.exists():
                atomic_write_text(blob_path, trimmed)
            row: dict = {
                ROW_FORMAT: fmt,
                ROW_SUMMARY: summary,
                ROW_TS: int(time.time() * 1000),
                ROW_FILE: fname,
                ROW_GAME_ID: None,
                ROW_REFS: preserved_refs,
            }
            if preserved_parent_id is not None:
                row[ROW_PARENT_GAME_ID] = preserved_parent_id
                row[ROW_FORK_PLY] = preserved_fork_ply
            self._index[new_hash] = row
            self._bind_id_locked(game_id, new_hash, row)
            if parent_ref_to_append is not None:
                self._append_parent_ref_locked(
                    parent_ref_to_append[0], game_id,
                    parent_ref_to_append[1],
                )
            self._evict_locked()
            self._persist_locked()
        return new_hash

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

    async def remove(self, h: str) -> RemoveResult:
        """Delete ``h`` (index row + blob).

        Returns:
        - ``RemoveResult(NOT_FOUND)`` if ``h`` is unknown.
        - ``RemoveResult(BLOCKED_BY_REFS, children=[...])`` if the row
          has live children. Nothing is deleted. The returned list
          mirrors what ``children_of`` would surface so the caller can
          message the user.
        - ``RemoveResult(DELETED)`` on success. If the deleted row
          carried a ``parent_game_id``, the matching entry in the
          parent's ``refs`` is scrubbed in the same atomic persist.
          A dangling parent (id not resolvable) is warn-logged and
          treated as a soft success.
        """
        async with self._lock:
            row = self._index.get(h)
            if row is None:
                return RemoveResult(RemoveStatus.NOT_FOUND)

            refs = row.get(ROW_REFS) or []
            if refs:
                children = self._children_summary_locked(refs)
                log.info(
                    "xgame.delete_blocked hash=%s game_id=%s refs=%d",
                    h, row.get(ROW_GAME_ID), len(refs),
                )
                return RemoveResult(
                    RemoveStatus.BLOCKED_BY_REFS,
                    children=children,
                )

            # Detach from parent (if any) before removing.
            parent_id = row.get(ROW_PARENT_GAME_ID)
            child_gid = row.get(ROW_GAME_ID)
            if parent_id is not None:
                self._detach_from_parent_locked(parent_id, child_gid)

            self._index.pop(h, None)
            if child_gid is not None:
                self._by_id.pop(child_gid, None)
            self._delete_blob(row.get(ROW_FILE))
            self._persist_locked()
            if parent_id is not None:
                log.info(
                    "xgame.child_deleted parent=%s child=%s ply=%s",
                    parent_id, child_gid, row.get(ROW_FORK_PLY),
                )
            return RemoveResult(RemoveStatus.DELETED)

    def parent_summary_of(self, game_id: str) -> dict | None:
        """If the row identified by ``game_id`` has a resolvable parent,
        return the parent's ``summary`` dict. Returns None when there is
        no parent or the parent has been evicted / corrupted."""
        h = self._by_id.get(game_id)
        if h is None:
            return None
        row = self._index.get(h)
        if row is None:
            return None
        parent_id = row.get(ROW_PARENT_GAME_ID)
        if parent_id is None:
            return None
        parent_hash = self._by_id.get(parent_id)
        if parent_hash is None:
            return None
        parent_row = self._index.get(parent_hash)
        if parent_row is None:
            return None
        return parent_row.get(ROW_SUMMARY)

    def children_of(self, game_id: str) -> list[dict]:
        """Return live children of ``game_id`` as
        ``[{game_id, fork_ply, summary, ts}, ...]`` sorted by fork_ply
        asc. Dangling ref entries (child id not resolvable) are dropped
        from the result and warn-logged; the parent's ``refs`` list is
        not mutated here (scrubbed lazily on subsequent writes)."""
        h = self._by_id.get(game_id)
        if h is None:
            return []
        row = self._index.get(h)
        if row is None:
            return []
        refs = row.get(ROW_REFS) or []
        return self._children_summary_locked(refs)

    async def scrub_dangling_parent(self, game_id: str) -> bool:
        """If the row identified by ``game_id`` carries a
        ``parent_game_id`` that no longer resolves, drop the fork-link
        fields and persist. Returns True if a scrub happened."""
        async with self._lock:
            h = self._by_id.get(game_id)
            if h is None:
                return False
            row = self._index.get(h)
            if row is None:
                return False
            parent_id = row.get(ROW_PARENT_GAME_ID)
            if parent_id is None:
                return False
            if parent_id in self._by_id:
                return False
            log.warning(
                "xgame.dangling_parent child=%s missing_parent=%s",
                game_id, parent_id,
            )
            row.pop(ROW_PARENT_GAME_ID, None)
            row.pop(ROW_FORK_PLY, None)
            self._persist_locked()
            return True

    # ---- internals (must hold the lock) ----

    def _append_parent_ref_locked(
        self,
        parent_game_id: str,
        child_game_id: str | None,
        fork_ply: int,
    ) -> None:
        """Append ``{game_id, fork_ply}`` to the parent's ``refs``. The
        parent row is mutated in place; the surrounding caller is
        responsible for the atomic persist. Missing parent is logged
        and treated as a no-op (dangling forward reference)."""
        if child_game_id is None:
            raise AssertionError(
                "fork-link requires a bound child game_id"
            )
        h = self._by_id.get(parent_game_id)
        if h is None:
            log.warning(
                "xgame.fork_parent_missing parent=%s child=%s ply=%d",
                parent_game_id, child_game_id, fork_ply,
            )
            return
        row = self._index.get(h)
        if row is None:
            log.warning(
                "xgame.fork_parent_missing parent=%s child=%s ply=%d",
                parent_game_id, child_game_id, fork_ply,
            )
            return
        refs = row.setdefault(ROW_REFS, [])
        refs.append({REF_GAME_ID: child_game_id, REF_FORK_PLY: fork_ply})
        log.info(
            "xgame.fork_created parent=%s child=%s ply=%d",
            parent_game_id, child_game_id, fork_ply,
        )

    def _detach_from_parent_locked(
        self,
        parent_game_id: str,
        child_game_id: str | None,
    ) -> None:
        """Remove the entry for ``child_game_id`` from the parent's
        ``refs``. Dangling parent is warn-logged and treated as a
        no-op."""
        if child_game_id is None:
            return
        h = self._by_id.get(parent_game_id)
        if h is None:
            log.warning(
                "xgame.dangling_parent_on_remove child=%s parent=%s",
                child_game_id, parent_game_id,
            )
            return
        row = self._index.get(h)
        if row is None:
            return
        refs = row.get(ROW_REFS) or []
        row[ROW_REFS] = [
            r for r in refs if r.get(REF_GAME_ID) != child_game_id
        ]

    def _children_summary_locked(self, refs: list[dict]) -> list[dict]:
        """Resolve a parent's ``refs`` to child summaries. Dangling
        entries are dropped + warn-logged."""
        out: list[dict] = []
        for ref in refs:
            child_id = ref.get(REF_GAME_ID)
            fork_ply = ref.get(REF_FORK_PLY)
            if child_id is None:
                continue
            child_hash = self._by_id.get(child_id)
            if child_hash is None:
                log.warning(
                    "xgame.dangling_ref child=%s ply=%s",
                    child_id, fork_ply,
                )
                continue
            child_row = self._index.get(child_hash)
            if child_row is None:
                log.warning(
                    "xgame.dangling_ref child=%s ply=%s",
                    child_id, fork_ply,
                )
                continue
            out.append({
                REF_GAME_ID: child_id,
                REF_FORK_PLY: fork_ply,
                ROW_SUMMARY: child_row.get(ROW_SUMMARY),
                ROW_TS: child_row.get(ROW_TS),
            })
        out.sort(key=lambda d: d.get(REF_FORK_PLY) or 0)
        return out

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
            key=lambda kv: kv[1].get(ROW_TS, 0),
            reverse=True,
        )
        candidates = list(reversed(ranked[self._cap:]))
        drop_target = len(candidates)
        dropped = 0
        for h, row in candidates:
            if row.get(ROW_REFS):
                continue
            if active_gid is not None and row.get(ROW_GAME_ID) == active_gid:
                continue
            self._index.pop(h, None)
            gid = row.get(ROW_GAME_ID)
            if gid is not None:
                self._by_id.pop(gid, None)
            self._delete_blob(row.get(ROW_FILE))
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
            gid = row.get(ROW_GAME_ID)
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
