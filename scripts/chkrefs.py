#!/usr/bin/env python3
"""List (and optionally drop) dead fork refs in the recent-imports index.

A dead ref names a child game that no longer resolves; it pins its parent
against delete and eviction forever. Read-only unless ``--apply``. With
``--apply``, stop the server first: it rewrites the index from memory.

Usage:
  python scripts/chkrefs.py [--dir IMPORTS_DIR] [--apply]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "server"))

from sturddle_view._atomic import atomic_write_json  # noqa: E402
from sturddle_view.recent_imports import (  # noqa: E402
    REF_FORK_PLY,
    REF_GAME_ID,
    ROW_GAME_ID,
    ROW_REFS,
    RecentImports,
    default_imports_dir,
)

_ENCODING = "utf-8"
_DEAD_REF_LINE = "dead ref: parent={parent} child={child} ply={ply}"
_SUMMARY_LINE = "{n} dead ref(s){suffix}"
_APPLIED_SUFFIX = ", removed"
_DRY_RUN_SUFFIX = "; rerun with --apply to remove"
_NO_SUFFIX = ""


def _is_dead(ref: dict, store: RecentImports) -> bool:
    child_id = ref.get(REF_GAME_ID)
    return child_id is None or store.hash_for_id(child_id) is None


def find_dead_refs(store: RecentImports) -> list[tuple[str, dict]]:
    """Return ``(parent_game_id, ref)`` for every unresolvable ref.
    Liveness is judged by the store's own load (blob-less rows drop)."""
    return [
        (row.get(ROW_GAME_ID), ref)
        for row in store.list()
        for ref in row.get(ROW_REFS) or []
        if _is_dead(ref, store)
    ]


def drop_dead_refs(store: RecentImports) -> int:
    """Rewrite the index without dead refs. Returns how many were dropped."""
    # Raw rewrite, not the store's persist: rows the store's load would
    # skip (missing blob) stay on disk untouched.
    raw = json.loads(store.index_path.read_text(encoding=_ENCODING))
    dropped = 0
    for row in raw.values():
        if not isinstance(row, dict):
            continue
        refs = row.get(ROW_REFS) or []
        live = [ref for ref in refs if not _is_dead(ref, store)]
        if len(live) != len(refs):
            dropped += len(refs) - len(live)
            row[ROW_REFS] = live
    if dropped:
        atomic_write_json(store.index_path, raw)
    return dropped


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", type=Path, default=None,
                    help="imports dir (default: the app's)")
    ap.add_argument("--apply", action="store_true",
                    help="remove dead refs (stop the server first)")
    args = ap.parse_args(argv)
    store = RecentImports.load(root=args.dir or default_imports_dir())
    dead = find_dead_refs(store)
    for parent_id, ref in dead:
        print(_DEAD_REF_LINE.format(
            parent=parent_id, child=ref.get(REF_GAME_ID), ply=ref.get(REF_FORK_PLY),
        ))
    if args.apply and dead:
        drop_dead_refs(store)
    suffix = _APPLIED_SUFFIX if args.apply else _DRY_RUN_SUFFIX
    print(_SUMMARY_LINE.format(n=len(dead), suffix=suffix if dead else _NO_SUFFIX))
    return 0


if __name__ == "__main__":
    sys.exit(main())
