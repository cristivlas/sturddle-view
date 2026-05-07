from __future__ import annotations

import json

import pytest

from sturddle_view.engines import (
    DuplicateEngineError,
    EngineNotFoundError,
    EngineRegistry,
)


@pytest.fixture
def registry(tmp_path):
    return EngineRegistry(path=tmp_path / "engines.json")


def test_empty_when_no_file(registry):
    assert registry.list() == []


def test_add_persists(registry):
    e = registry.add(name="Stockfish", path="/usr/bin/stockfish", options={"Hash": 256})
    assert e.id and e.name == "Stockfish"

    # New instance, same path: should re-load from disk.
    fresh = EngineRegistry(path=registry.path)
    [loaded] = fresh.list()
    assert loaded.id == e.id
    assert loaded.name == "Stockfish"
    assert loaded.path == "/usr/bin/stockfish"
    assert loaded.options == {"Hash": 256}


def test_add_duplicate_name_rejected(registry):
    registry.add(name="X", path="/p/x")
    with pytest.raises(DuplicateEngineError):
        registry.add(name="X", path="/p/y")
    # Case-insensitive.
    with pytest.raises(DuplicateEngineError):
        registry.add(name="x", path="/p/z")
    # Different name on same path is allowed.
    registry.add(name="X-tuned", path="/p/x")
    assert len(registry.list()) == 2


def test_add_auto_suffix_on_collision(registry):
    registry.add(name="MyEngine", path="/p/a")
    e2 = registry.add(name="MyEngine", path="/p/b", auto_suffix=True)
    assert e2.name == "MyEngine (2)"
    e3 = registry.add(name="MyEngine", path="/p/c", auto_suffix=True)
    assert e3.name == "MyEngine (3)"


def test_update_duplicate_name_rejected(registry):
    registry.add(name="A", path="/p/a")
    b = registry.add(name="B", path="/p/b")
    with pytest.raises(DuplicateEngineError):
        registry.update(b.id, name="A")
    # Case-insensitive.
    with pytest.raises(DuplicateEngineError):
        registry.update(b.id, name="a")
    # Renaming to the same name is fine (no-op).
    registry.update(b.id, name="B")


def test_update(registry):
    e = registry.add(name="A", path="/p/a")
    registry.update(e.id, name="A2", options={"Threads": 4})
    got = registry.get(e.id)
    assert got.name == "A2"
    assert got.options == {"Threads": 4}
    assert got.path == "/p/a"  # untouched


def test_update_unknown(registry):
    with pytest.raises(EngineNotFoundError):
        registry.update("nope", name="X")


def test_remove(registry):
    e = registry.add(name="A", path="/p/a")
    registry.remove(e.id)
    assert registry.list() == []
    with pytest.raises(EngineNotFoundError):
        registry.get(e.id)
    with pytest.raises(EngineNotFoundError):
        registry.remove(e.id)


def test_atomic_write_no_partial_file_on_error(tmp_path, monkeypatch):
    reg = EngineRegistry(path=tmp_path / "engines.json")
    reg.add(name="A", path="/p/a")
    original = reg.path.read_text()

    # Force os.replace to fail; the original file must remain intact and no
    # tmp file should leak in the directory.
    import sturddle_view._atomic as atomic

    def boom(*_args, **_kwargs):
        raise OSError("simulated failure")

    monkeypatch.setattr(atomic.os, "replace", boom)
    with pytest.raises(OSError):
        reg.add(name="B", path="/p/b")

    assert reg.path.read_text() == original
    leaked = [p for p in tmp_path.iterdir() if p.name.startswith(".engines.")]
    assert leaked == []


def test_select_persists(registry):
    a = registry.add(name="A", path="/p/a")
    registry.select(a.id)
    assert registry.selected_id == a.id

    fresh = EngineRegistry(path=registry.path)
    assert fresh.selected_id == a.id


def test_select_unknown_raises(registry):
    with pytest.raises(EngineNotFoundError):
        registry.select("nope")


def test_remove_clears_selection(registry):
    a = registry.add(name="A", path="/p/a")
    registry.select(a.id)
    registry.remove(a.id)
    assert registry.selected_id is None

    fresh = EngineRegistry(path=registry.path)
    assert fresh.selected_id is None


def test_stale_selection_in_file_is_ignored(tmp_path):
    path = tmp_path / "engines.json"
    path.write_text(
        json.dumps({"engines": [], "selected_id": "ghost-id"})
    )
    reg = EngineRegistry(path=path)
    assert reg.selected_id is None


def test_load_normalizes_duplicate_names(tmp_path):
    path = tmp_path / "engines.json"
    path.write_text(
        json.dumps(
            {
                "engines": [
                    {"id": "a", "name": "MyEngine", "path": "/p/a", "options": {}},
                    {"id": "b", "name": "MyEngine", "path": "/p/b", "options": {}},
                    {"id": "c", "name": "myengine", "path": "/p/c", "options": {}},
                ]
            }
        )
    )
    reg = EngineRegistry(path=path)
    names = [e.name for e in reg.list()]
    # Case is preserved per-entry; uniqueness check is case-insensitive.
    assert names == ["MyEngine", "MyEngine (2)", "myengine (3)"]
    # Persisted: a fresh load sees the normalized names without further changes.
    fresh = EngineRegistry(path=path)
    assert [e.name for e in fresh.list()] == names


def test_load_ignores_unknown_fields(tmp_path):
    path = tmp_path / "engines.json"
    path.write_text(
        json.dumps(
            {
                "engines": [
                    {
                        "id": "abc",
                        "name": "X",
                        "path": "/p/x",
                        "options": {},
                        "future_field": "ignored",
                    }
                ]
            }
        )
    )
    reg = EngineRegistry(path=path)
    [e] = reg.list()
    assert e.id == "abc"
