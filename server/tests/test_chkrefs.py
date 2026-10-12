"""scripts/chkrefs.py: find and fix bad fork links (dead refs, self links)
in the imports index."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

import sturddle_view
from sturddle_view import APP_NAME, INSTANCE_ENV, INSTANCE_FLAG
from sturddle_view.play.canonical_hash import FMT_PGN
from sturddle_view.recent_imports import (
    REF_FORK_PLY,
    REF_GAME_ID,
    ROW_FILE,
    ROW_FORK_PLY,
    ROW_PARENT_GAME_ID,
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
_FIX_FLAG = "--fix"
_DIR_FLAG = "--dir"
_INSTANCE = "chk"
_MISSING_DIRNAME = "no-such-instance"
_IMPORTS_DIR_ENV = "SV_IMPORTS_DIR"
_ENCODING = "utf-8"


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
    return _seed_dead_ref(root)


def _seed_dead_ref(root):
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
    assert chkrefs.find_bad_links(_load(root)) == []


def test_find_lists_only_the_dead_ref(root, parent_hash):
    assert chkrefs.find_bad_links(_load(root)) == [
        (_PARENT_GID, _DEAD_GID, _DEAD_PLY, chkrefs.DEAD_REF),
    ]


def test_drop_keeps_live_refs(root, parent_hash):
    assert chkrefs.fix_bad_links(_load(root)) == 1
    assert _parent_ref_ids(root, parent_hash) == [_LIVE_GID]
    assert chkrefs.find_bad_links(_load(root)) == []


def test_drop_unpins_parent_once_live_child_removed(root, parent_hash):
    chkrefs.fix_bad_links(_load(root))
    store = _load(root)
    _run(store.remove(store.hash_for_id(_LIVE_GID)))
    assert _run(store.remove(parent_hash)).status is RemoveStatus.DELETED


def test_main_without_fix_leaves_index_untouched(root, parent_hash):
    index_path = _load(root).index_path
    before = index_path.read_bytes()
    assert chkrefs.main([_DIR_FLAG, str(root)]) == 0
    assert index_path.read_bytes() == before


def test_main_with_fix_drops_dead_refs(root, parent_hash):
    assert chkrefs.main([_DIR_FLAG, str(root), _FIX_FLAG]) == 0
    assert _parent_ref_ids(root, parent_hash) == [_LIVE_GID]


def test_main_instance_reads_that_instances_imports(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(INSTANCE_ENV, "")  # main sets it; restored after
    monkeypatch.delenv(_IMPORTS_DIR_ENV, raising=False)
    monkeypatch.setattr(
        sturddle_view, "_user_data_dir", lambda name, appauthor: str(tmp_path / name),
    )
    _seed_dead_ref(tmp_path / f"{APP_NAME}-{_INSTANCE}" / _IMPORTS_DIRNAME)
    assert chkrefs.main([INSTANCE_FLAG, _INSTANCE]) == 0
    assert chkrefs._PROBLEM_LINE.format(
        kind=chkrefs.DEAD_REF, parent=_PARENT_GID, child=_DEAD_GID, ply=_DEAD_PLY,
    ) in capsys.readouterr().out


def test_main_reports_missing_index(tmp_path, capsys):
    missing = tmp_path / _MISSING_DIRNAME
    assert chkrefs.main([_DIR_FLAG, str(missing)]) == chkrefs.EXIT_NO_INDEX
    assert str(missing) in capsys.readouterr().err


def test_main_rejects_dir_with_instance(root):
    with pytest.raises(SystemExit):
        chkrefs.main([_DIR_FLAG, str(root), INSTANCE_FLAG, _INSTANCE])


@pytest.fixture
def self_linked_hash(root):
    """A row listed as its own parent and child, as the old save() left a
    fork that deduped onto its parent's row."""
    store = _load(root)
    h = _save(store, _PARENT_PGN, _PARENT_GID)
    raw = json.loads(store.index_path.read_text(encoding=_ENCODING))
    raw[h][ROW_PARENT_GAME_ID] = _PARENT_GID
    raw[h][ROW_FORK_PLY] = _DEAD_PLY
    raw[h][ROW_REFS] = [{REF_GAME_ID: _PARENT_GID, REF_FORK_PLY: _DEAD_PLY}]
    store.index_path.write_text(json.dumps(raw), encoding=_ENCODING)
    return h


def test_find_reports_self_link(root, self_linked_hash):
    assert chkrefs.find_bad_links(_load(root)) == [
        (_PARENT_GID, _PARENT_GID, _DEAD_PLY, chkrefs.SELF_LINK),
    ]


def test_fix_breaks_self_link(root, self_linked_hash):
    assert chkrefs.fix_bad_links(_load(root)) == 1
    store = _load(root)
    row = store.get(self_linked_hash)[0]
    assert row[ROW_REFS] == []
    assert ROW_PARENT_GAME_ID not in row
    assert chkrefs.find_bad_links(store) == []
    assert _run(store.remove(self_linked_hash)).status is RemoveStatus.DELETED
