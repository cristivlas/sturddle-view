"""scripts/chkrefs.py: find and drop dead fork refs in the imports index."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from sturddle_view.play.canonical_hash import FMT_PGN
from sturddle_view.recent_imports import (
    REF_GAME_ID,
    ROW_FILE,
    ROW_REFS,
    RecentImports,
    RemoveStatus,
)

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "chkrefs.py"
_spec = importlib.util.spec_from_file_location("chkrefs", _SCRIPT)
chkrefs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chkrefs)

_IMPORTS_DIRNAME = "imports"
_PARENT_GID = "gid-parent"
_LIVE_GID = "gid-live"
_DEAD_GID = "gid-dead"
_PARENT_PGN = "1. e4 e5 *"
_LIVE_PGN = "1. d4 d5 *"
_DEAD_PGN = "1. c4 c5 *"
_SUMMARY = "s"
_LIVE_PLY = 3
_DEAD_PLY = 5
_APPLY_FLAG = "--apply"
_DIR_FLAG = "--dir"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _save(store, text, gid, ply=None):
    parent = None if ply is None else _PARENT_GID
    return _run(store.save(
        fmt=FMT_PGN, text=text, summary=_SUMMARY, game_id=gid,
        parent_game_id=parent, fork_ply=ply,
    ))


@pytest.fixture
def root(tmp_path):
    return tmp_path / _IMPORTS_DIRNAME


@pytest.fixture
def parent_hash(root):
    """Parent with a live child and a child whose blob is gone, as an
    eviction that skipped the parent detach left it."""
    store = _load(root)
    h_parent = _save(store, _PARENT_PGN, _PARENT_GID)
    _save(store, _LIVE_PGN, _LIVE_GID, ply=_LIVE_PLY)
    h_dead = _save(store, _DEAD_PGN, _DEAD_GID, ply=_DEAD_PLY)
    (root / store.get(h_dead)[0][ROW_FILE]).unlink()
    return h_parent


def _load(root):
    """Fresh store off disk, as a new process would see it."""
    return RecentImports.load(root=root)


def _parent_ref_ids(root, h_parent):
    row = _load(root).get(h_parent)[0]
    return [ref[REF_GAME_ID] for ref in row[ROW_REFS]]


def test_no_dead_refs_when_children_live(root):
    store = _load(root)
    _save(store, _PARENT_PGN, _PARENT_GID)
    _save(store, _LIVE_PGN, _LIVE_GID, ply=_LIVE_PLY)
    assert chkrefs.find_dead_refs(_load(root)) == []


def test_find_lists_only_the_dead_ref(root, parent_hash):
    [(parent_id, ref)] = chkrefs.find_dead_refs(_load(root))
    assert parent_id == _PARENT_GID
    assert ref[REF_GAME_ID] == _DEAD_GID


def test_drop_keeps_live_refs(root, parent_hash):
    assert chkrefs.drop_dead_refs(_load(root)) == 1
    assert _parent_ref_ids(root, parent_hash) == [_LIVE_GID]
    assert chkrefs.find_dead_refs(_load(root)) == []


def test_drop_unpins_parent_once_live_child_removed(root, parent_hash):
    chkrefs.drop_dead_refs(_load(root))
    store = _load(root)
    _run(store.remove(store.hash_for_id(_LIVE_GID)))
    assert _run(store.remove(parent_hash)).status is RemoveStatus.DELETED


def test_main_without_apply_leaves_index_untouched(root, parent_hash):
    index_path = _load(root).index_path
    before = index_path.read_bytes()
    assert chkrefs.main([_DIR_FLAG, str(root)]) == 0
    assert index_path.read_bytes() == before


def test_main_with_apply_drops_dead_refs(root, parent_hash):
    assert chkrefs.main([_DIR_FLAG, str(root), _APPLY_FLAG]) == 0
    assert _parent_ref_ids(root, parent_hash) == [_LIVE_GID]
