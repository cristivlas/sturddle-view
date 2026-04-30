"""Slice 1: TournamentStore — on-disk layout, state.json, atomic writes,
status transitions, error paths."""
from __future__ import annotations

import json

import pytest

from sturddle_view.tournament.store import (
    STATUS_DONE,
    STATUS_IDLE,
    STATUS_RUNNING,
    STATUS_STOPPED,
    CorruptStateError,
    Tournament,
    TournamentNotFoundError,
    TournamentStore,
    default_root,
)


@pytest.fixture
def store(tmp_path):
    return TournamentStore(tmp_path / "tournaments")


def test_default_root_uses_platformdirs():
    root = default_root()
    assert root.name == "tournaments"
    assert "sturddle-view" in str(root)


def test_create_round_trip(store):
    t = store.create(
        name="My Tournament",
        template={"tc": "10+0.1", "concurrency": 4},
        engines=[{"name": "engine-A"}, {"name": "engine-B"}],
    )
    assert t.name == "My Tournament"
    assert t.status == STATUS_IDLE
    assert t.id and len(t.id) == 32  # uuid4 hex
    assert t.template["tc"] == "10+0.1"
    assert len(t.engines) == 2

    fetched = store.get(t.id)
    assert fetched.id == t.id
    assert fetched.name == t.name


def test_create_makes_directory_with_logs_subdir(store):
    t = store.create(name="x", template={}, engines=[])
    d = store.root / t.id
    assert d.is_dir()
    assert (d / "state.json").exists()
    assert (d / "logs").is_dir()
    # No PGN/config until fastchess writes them
    assert not (d / "games.pgn").exists()
    assert not (d / "config.json").exists()


def test_state_json_is_valid_json_with_expected_fields(store):
    t = store.create(name="x", template={"k": "v"}, engines=[])
    payload = json.loads((store.root / t.id / "state.json").read_text())
    assert payload["id"] == t.id
    assert payload["status"] == STATUS_IDLE
    assert payload["template"] == {"k": "v"}
    assert payload["created_at"]
    assert payload["started_at"] is None
    assert payload["stopped_at"] is None


def test_path_helpers(store):
    t = store.create(name="x", template={}, engines=[])
    assert store.pgn_path(t.id).name == "games.pgn"
    assert store.config_path(t.id).name == "config.json"
    assert store.logs_dir(t.id).name == "logs"
    assert store.pgn_path(t.id).parent == store.root / t.id


def test_get_unknown_id_raises(store):
    with pytest.raises(TournamentNotFoundError):
        store.get("nonexistent")


def test_list_empty_when_root_absent(tmp_path):
    s = TournamentStore(tmp_path / "does-not-exist")
    assert s.list() == []


def test_list_sorts_by_created_at(store):
    a = store.create(name="a", template={}, engines=[])
    b = store.create(name="b", template={}, engines=[])
    c = store.create(name="c", template={}, engines=[])
    ids = [t.id for t in store.list()]
    assert ids == [a.id, b.id, c.id]


def test_list_skips_directories_without_state_json(store):
    store.create(name="real", template={}, engines=[])
    (store.root / "junk-dir").mkdir()
    (store.root / "stray-file").write_text("not a dir's job")
    listed = store.list()
    assert len(listed) == 1
    assert listed[0].name == "real"


def test_list_skips_corrupt_state_json(store):
    t = store.create(name="real", template={}, engines=[])
    bad = store.create(name="will-corrupt", template={}, engines=[])
    (store.root / bad.id / "state.json").write_text("{not json")
    listed = store.list()
    assert {x.id for x in listed} == {t.id}


def test_get_corrupt_state_json_raises(store):
    t = store.create(name="x", template={}, engines=[])
    (store.root / t.id / "state.json").write_text("{not json")
    with pytest.raises(CorruptStateError):
        store.get(t.id)


def test_get_state_with_invalid_status_raises(store):
    t = store.create(name="x", template={}, engines=[])
    raw = json.loads((store.root / t.id / "state.json").read_text())
    raw["status"] = "bogus"
    (store.root / t.id / "state.json").write_text(json.dumps(raw))
    with pytest.raises(CorruptStateError):
        store.get(t.id)


def test_get_state_missing_required_field_raises(store):
    t = store.create(name="x", template={}, engines=[])
    raw = json.loads((store.root / t.id / "state.json").read_text())
    raw.pop("created_at")
    (store.root / t.id / "state.json").write_text(json.dumps(raw))
    with pytest.raises(CorruptStateError):
        store.get(t.id)


def test_update_status_persists(store):
    t = store.create(name="x", template={}, engines=[])
    updated = store.update_status(t.id, STATUS_RUNNING, started_at="2026-04-30T12:00:00+00:00")
    assert updated.status == STATUS_RUNNING
    assert updated.started_at == "2026-04-30T12:00:00+00:00"

    # Persisted across instantiations
    s2 = TournamentStore(store.root)
    refetched = s2.get(t.id)
    assert refetched.status == STATUS_RUNNING
    assert refetched.started_at == "2026-04-30T12:00:00+00:00"


def test_update_status_running_then_stopped(store):
    t = store.create(name="x", template={}, engines=[])
    store.update_status(t.id, STATUS_RUNNING, started_at="2026-01-01T00:00:00+00:00")
    final = store.update_status(t.id, STATUS_STOPPED, stopped_at="2026-01-01T01:00:00+00:00")
    assert final.status == STATUS_STOPPED
    assert final.started_at == "2026-01-01T00:00:00+00:00"  # preserved
    assert final.stopped_at == "2026-01-01T01:00:00+00:00"


def test_update_status_invalid_value_rejected(store):
    t = store.create(name="x", template={}, engines=[])
    with pytest.raises(ValueError):
        store.update_status(t.id, "bogus")


def test_update_status_unknown_id_raises(store):
    with pytest.raises(TournamentNotFoundError):
        store.update_status("does-not-exist", STATUS_RUNNING)


def test_remove(store):
    t = store.create(name="x", template={}, engines=[])
    assert (store.root / t.id).exists()
    store.remove(t.id)
    assert not (store.root / t.id).exists()
    with pytest.raises(TournamentNotFoundError):
        store.get(t.id)


def test_remove_unknown_id_raises(store):
    with pytest.raises(TournamentNotFoundError):
        store.remove("nope")


def test_find_by_status(store):
    a = store.create(name="a", template={}, engines=[])
    b = store.create(name="b", template={}, engines=[])
    c = store.create(name="c", template={}, engines=[])
    store.update_status(a.id, STATUS_RUNNING)
    store.update_status(b.id, STATUS_DONE)
    # c stays idle

    running = store.find_by_status(STATUS_RUNNING)
    assert {t.id for t in running} == {a.id}

    idle = store.find_by_status(STATUS_IDLE)
    assert {t.id for t in idle} == {c.id}


def test_atomic_write_no_partial_state_visible_on_crash(store, monkeypatch):
    """If the rename step fails, the original state.json must not be replaced."""
    t = store.create(name="x", template={"k": 1}, engines=[])
    original = json.loads((store.root / t.id / "state.json").read_text())

    import os as _os

    real_replace = _os.replace
    fail = {"once": True}

    def boom(src, dst):
        if fail["once"]:
            fail["once"] = False
            raise OSError("simulated rename failure")
        return real_replace(src, dst)

    monkeypatch.setattr(_os, "replace", boom)

    with pytest.raises(OSError):
        store.update_status(t.id, STATUS_RUNNING)

    # state.json untouched
    after = json.loads((store.root / t.id / "state.json").read_text())
    assert after == original
    # No leftover temp files
    leftover = [p for p in (store.root / t.id).iterdir() if p.name.startswith(".state.json.")]
    assert leftover == []


def test_root_auto_created_on_first_create(tmp_path):
    root = tmp_path / "deep" / "nested" / "tournaments"
    assert not root.exists()
    s = TournamentStore(root)
    s.create(name="x", template={}, engines=[])
    assert root.is_dir()


def test_tournament_dataclass_round_trips_through_dict():
    t = Tournament(id="abc", name="n", status=STATUS_IDLE, created_at="t")
    d = t.to_dict()
    t2 = Tournament(**d)
    assert t2 == t
