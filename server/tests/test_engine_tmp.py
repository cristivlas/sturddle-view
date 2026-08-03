"""Tests for managed engine temp dirs (docs/engine-temp-cleanup-spec.md).

Engines spawn with TMP/TEMP/TMPDIR pointed at a per-spawn dir under a
managed root so self-extracted files can always be cleaned: on engine
exit (quit / throwaway cleanup / respawn) and by a startup orphan sweep.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from sturddle_view.engine_tmp import (
    TEMP_ENV_VARS,
    cleanup_spawn_dir,
    create_spawn_dir,
    engine_tmp_root,
    sweep_orphans,
    temp_env,
)
from sturddle_view.engines import _popen_kwargs, probe_engine
from sturddle_view.events import EventBus
from sturddle_view.play.engine_supervisor import EngineSupervisor


@pytest.fixture
def tmp_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "engine-tmp"
    monkeypatch.setenv("SV_ENGINE_TMP_ROOT", str(root))
    return root


# ---------------------------------------------------------------------------
# engine_tmp module
# ---------------------------------------------------------------------------


def test_root_env_override(tmp_root):
    assert engine_tmp_root() == tmp_root


def test_create_spawn_dir_unique_under_root(tmp_root):
    a = create_spawn_dir()
    b = create_spawn_dir()
    assert a != b
    assert a.parent == tmp_root and b.parent == tmp_root
    assert a.is_dir() and b.is_dir()
    assert a.name.startswith(f"{os.getpid()}-")


def test_temp_env_covers_windows_and_posix(tmp_root):
    d = create_spawn_dir()
    env = temp_env(d)
    assert set(env) == {"TMP", "TEMP", "TMPDIR"}
    assert set(env) == set(TEMP_ENV_VARS)
    assert all(v == str(d) for v in env.values())


def test_cleanup_removes_tree(tmp_root):
    d = create_spawn_dir()
    (d / "weights.nnue").write_bytes(b"x")
    cleanup_spawn_dir(d)
    assert not d.exists()


def test_cleanup_missing_dir_is_noop(tmp_root):
    cleanup_spawn_dir(tmp_root / "gone")
    cleanup_spawn_dir(None)


def test_sweep_removes_all_subdirs(tmp_root):
    for _ in range(3):
        d = create_spawn_dir()
        (d / "book.bin").write_bytes(b"x")
    stray = tmp_root / "stray.txt"
    stray.write_text("keep")
    assert sweep_orphans() == 3
    assert [p for p in tmp_root.iterdir()] == [stray]


def test_sweep_missing_root_returns_zero(tmp_root):
    assert sweep_orphans() == 0


# ---------------------------------------------------------------------------
# _popen_kwargs injection
# ---------------------------------------------------------------------------


def test_popen_kwargs_injects_temp_env(tmp_root):
    d = create_spawn_dir()
    env = _popen_kwargs(None, tmp_dir=d)["env"]
    for var in TEMP_ENV_VARS:
        assert env[var] == str(d)


def test_popen_kwargs_user_env_wins_over_temp(tmp_root):
    d = create_spawn_dir()
    env = _popen_kwargs({"TMPDIR": "/custom"}, tmp_dir=d)["env"]
    assert env["TMPDIR"] == "/custom"
    assert env["TMP"] == str(d)


def test_popen_kwargs_no_env_without_tmp_dir():
    assert "env" not in _popen_kwargs(None)


# ---------------------------------------------------------------------------
# EngineSupervisor lifecycle
# ---------------------------------------------------------------------------


class _StubTransport:
    def close(self) -> None:
        pass

    def get_pipe_transport(self, fd):
        return None

    def is_closing(self) -> bool:
        return True


class _StubEngine:
    def __init__(self) -> None:
        self.options: dict = {}
        self.id = {"name": "StubEngine"}
        self.transport = _StubTransport()
        self.send_line = lambda line: None
        self.line_received = lambda line: None

    async def configure(self, options: dict) -> None:
        pass

    async def quit(self) -> None:
        pass


@pytest.fixture
def supervisor(tmp_root) -> EngineSupervisor:
    sup = EngineSupervisor(engine_path="/fake/engine", bus=EventBus())

    async def fake_popen_uci(command, **kwargs):
        fake_popen_uci.last_kwargs = kwargs
        stub = _StubEngine()
        return (stub.transport, stub)

    fake_popen_uci.last_kwargs = None
    sup._popen_uci = fake_popen_uci
    return sup


def _spawn_dirs(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []


async def test_spawn_creates_dir_and_injects_env(supervisor, tmp_root):
    await supervisor.spawn()
    dirs = _spawn_dirs(tmp_root)
    assert len(dirs) == 1
    env = supervisor._popen_uci.last_kwargs["env"]
    for var in TEMP_ENV_VARS:
        assert env[var] == str(dirs[0])


async def test_quit_removes_spawn_dir(supervisor, tmp_root):
    await supervisor.ensure()
    (_spawn_dirs(tmp_root)[0] / "weights.nnue").write_bytes(b"x")
    await supervisor.quit()
    assert _spawn_dirs(tmp_root) == []


async def test_respawn_cleans_previous_dir(supervisor, tmp_root):
    # cancel-teardown discards the engine without quit(); the next
    # spawn must not leak the prior spawn's dir.
    await supervisor.spawn()
    first = _spawn_dirs(tmp_root)[0]
    await supervisor.spawn()
    dirs = _spawn_dirs(tmp_root)
    assert len(dirs) == 1
    assert first not in dirs


async def test_spawn_failure_removes_dir(supervisor, tmp_root):
    async def boom(command, **kwargs):
        raise FileNotFoundError(command)

    supervisor._popen_uci = boom
    with pytest.raises(FileNotFoundError):
        await supervisor.spawn()
    assert _spawn_dirs(tmp_root) == []


async def test_throwaway_cleanup_removes_dir(supervisor, tmp_root):
    engine, cleanup = await supervisor.spawn_throwaway()
    assert len(_spawn_dirs(tmp_root)) == 1
    await cleanup()
    assert _spawn_dirs(tmp_root) == []


async def test_throwaway_does_not_touch_long_lived_dir(supervisor, tmp_root):
    await supervisor.ensure()
    long_lived = _spawn_dirs(tmp_root)[0]
    engine, cleanup = await supervisor.spawn_throwaway()
    await cleanup()
    assert _spawn_dirs(tmp_root) == [long_lived]


# ---------------------------------------------------------------------------
# probe_engine
# ---------------------------------------------------------------------------


async def test_probe_success_removes_dir(tmp_root, monkeypatch):
    import chess.engine

    async def fake_popen_uci(command, **kwargs):
        stub = _StubEngine()
        return (stub.transport, stub)

    monkeypatch.setattr(chess.engine, "popen_uci", fake_popen_uci)
    name, schema, err = await probe_engine("/fake/engine")
    assert err is None
    assert _spawn_dirs(tmp_root) == []


async def test_probe_survives_spawn_dir_failure(tmp_root, monkeypatch):
    import chess.engine

    import sturddle_view.engines as engines_mod

    def boom_mkdir():
        raise OSError("disk full")

    async def fake_popen_uci(command, **kwargs):
        fake_popen_uci.last_kwargs = kwargs
        stub = _StubEngine()
        return (stub.transport, stub)

    fake_popen_uci.last_kwargs = None
    monkeypatch.setattr(engines_mod, "create_spawn_dir", boom_mkdir)
    monkeypatch.setattr(chess.engine, "popen_uci", fake_popen_uci)
    name, schema, err = await probe_engine("/fake/engine")
    assert err is None
    assert "env" not in fake_popen_uci.last_kwargs


async def test_probe_failure_removes_dir(tmp_root, monkeypatch):
    import chess.engine

    async def boom(command, **kwargs):
        raise FileNotFoundError(command)

    monkeypatch.setattr(chess.engine, "popen_uci", boom)
    name, schema, err = await probe_engine("/fake/engine")
    assert err is not None
    assert _spawn_dirs(tmp_root) == []


# ---------------------------------------------------------------------------
# startup sweep wiring
# ---------------------------------------------------------------------------


def test_lifespan_sweeps_orphans(tmp_root, tmp_path):
    from fastapi.testclient import TestClient

    from sturddle_view.app import create_app
    from sturddle_view.config import Settings
    from sturddle_view.engines import EngineRegistry

    orphan = tmp_root / "12345-deadbeef"
    orphan.mkdir(parents=True)
    (orphan / "book.bin").write_bytes(b"x")

    settings = Settings(token="t", auth_disabled=True)
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry)
    with TestClient(app):
        assert not orphan.exists()
