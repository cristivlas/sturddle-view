#!/usr/bin/env python3
"""List (and optionally fix) bad fork links in the recent-imports index.

- dead ref: names a child game that no longer resolves; it pins its parent
  against delete and eviction forever.
- self link: a row listed as its own parent / child (a fork whose content
  matched its parent's deduped onto the parent's row).

Read-only unless ``--fix``. With ``--fix``, stop the server first: it
rewrites the index from memory.

Usage:
  python scripts/chkrefs.py [--dir IMPORTS_DIR | --instance TAG] [--fix]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "server"))

from sturddle_view import add_instance_arg, apply_instance  # noqa: E402
from sturddle_view._atomic import atomic_write_json  # noqa: E402
from sturddle_view.recent_imports import (  # noqa: E402
    REF_FORK_PLY,
    REF_GAME_ID,
    ROW_FORK_PLY,
    ROW_GAME_ID,
    ROW_HASH,
    ROW_PARENT_GAME_ID,
    ROW_REFS,
    RecentImports,
    default_imports_dir,
)

DEAD_REF = "dead ref"
SELF_LINK = "self link"

_ENCODING = "utf-8"
_PROBLEM_LINE = "{kind}: parent={parent} child={child} ply={ply}"
_SUMMARY_LINE = "{n} bad link(s){suffix}"
_FIXED_SUFFIX = ", fixed"
_DRY_RUN_SUFFIX = "; rerun with --fix"
_NO_SUFFIX = ""
_NO_INDEX_LINE = "no imports index at {path}"
EXIT_NO_INDEX = 2


def _is_dead(ref: dict, store: RecentImports) -> bool:
    child_id = ref.get(REF_GAME_ID)
    return child_id is None or store.hash_for_id(child_id) is None


def _is_self(game_id: str | None, h: str, store: RecentImports) -> bool:
    return game_id is not None and store.hash_for_id(game_id) == h


def _has_self_link(h: str, row: dict, store: RecentImports) -> bool:
    return _is_self(row.get(ROW_PARENT_GAME_ID), h, store) or any(
        _is_self(ref.get(REF_GAME_ID), h, store) for ref in row.get(ROW_REFS) or []
    )


def find_bad_links(store: RecentImports) -> list[tuple[str, str, int | None, str]]:
    """``(parent_game_id, child_game_id, fork_ply, kind)`` per bad link.
    Liveness is judged by the store's own load (blob-less rows drop)."""
    out = []
    for row in store.list():
        h, gid = row[ROW_HASH], row.get(ROW_GAME_ID)
        if _has_self_link(h, row, store):
            out.append((gid, gid, row.get(ROW_FORK_PLY), SELF_LINK))
        for ref in row.get(ROW_REFS) or []:
            if _is_dead(ref, store):
                out.append((gid, ref.get(REF_GAME_ID), ref.get(REF_FORK_PLY), DEAD_REF))
    return out


def fix_bad_links(store: RecentImports) -> int:
    """Rewrite the index without dead refs and self links. Returns how many
    rows changed."""
    # Raw rewrite, not the store's persist: rows the store's load would
    # skip (missing blob) stay on disk untouched.
    raw = json.loads(store.index_path.read_text(encoding=_ENCODING))
    changed = 0
    for h, row in raw.items():
        if not isinstance(row, dict):
            continue
        refs = row.get(ROW_REFS) or []
        kept = [
            ref for ref in refs
            if not _is_dead(ref, store) and not _is_self(ref.get(REF_GAME_ID), h, store)
        ]
        self_parent = _is_self(row.get(ROW_PARENT_GAME_ID), h, store)
        if len(kept) == len(refs) and not self_parent:
            continue
        row[ROW_REFS] = kept
        if self_parent:
            row.pop(ROW_PARENT_GAME_ID, None)
            row.pop(ROW_FORK_PLY, None)
        changed += 1
    if changed:
        atomic_write_json(store.index_path, raw)
    return changed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    where = ap.add_mutually_exclusive_group()
    where.add_argument("--dir", type=Path, default=None,
                       help="imports dir (default: the app's, or the instance's)")
    add_instance_arg(where)
    ap.add_argument("--fix", action="store_true",
                    help="fix bad links (stop the server first)")
    args = ap.parse_args(argv)
    apply_instance(args.instance)
    store = RecentImports.load(root=args.dir or default_imports_dir())
    # A missing index loads as an empty store; say so, not "0 bad links".
    if not store.index_path.is_file():
        print(_NO_INDEX_LINE.format(path=store.index_path), file=sys.stderr)
        return EXIT_NO_INDEX
    bad = find_bad_links(store)
    for parent_id, child_id, ply, kind in bad:
        print(_PROBLEM_LINE.format(kind=kind, parent=parent_id, child=child_id, ply=ply))
    if args.fix and bad:
        fix_bad_links(store)
    suffix = _FIXED_SUFFIX if args.fix else _DRY_RUN_SUFFIX
    print(_SUMMARY_LINE.format(n=len(bad), suffix=suffix if bad else _NO_SUFFIX))
    return 0


if __name__ == "__main__":
    sys.exit(main())
