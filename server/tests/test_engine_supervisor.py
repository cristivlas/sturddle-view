"""Tests for play/engine_supervisor.py (R3 / P6).

All tests are red until play/engine_supervisor.py is implemented.

Stub interface pin -- mirrors chess.engine.UciProtocol's public surface as
the supervisor uses it. Adding methods to the stub without updating this
docstring is a violation; the stub exists to catch interface drift early.

Supervisor reads/writes on the protocol object:
    - send_line(line: str)       -- patched by _patch_log; called by cancel
    - line_received(line: str)   -- patched by _patch_log
    - configure(options: dict)   -- async; raises chess.engine.EngineError
    - quit()                     -- async; idempotent on supervisor side
    - transport                  -- has .close() (cancel fallback path)
    - options                    -- dict-like; entries have is_managed()
                                    plus type/default/min/max/var attrs
    - id                         -- dict (id name fills engine_name)
    - returncode                 -- optional Future-like, .add_done_callback

References:
    server-chess-audit.md section 10 (R3 gap closure)
    test-battery-plan.md P6 sub-task
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any
from unittest.mock import patch

import chess.engine
import pytest

# -- module under test (will fail ImportError until implemented) --
from sturddle_view.play.engine_supervisor import EngineSupervisor
from sturddle_view.events import EventBus


# ---------------------------------------------------------------------------
# Stub UCI protocol -- mirrors the surface pinned above. Nothing more.
# ---------------------------------------------------------------------------


class _OptionDef:
    def __init__(self, name: str, managed: bool = False) -> None:
        self.name = name
        self.type = "spin"
        self.default = 0
        self.min = 0
        self.max = 1024
        self.var: list[str] = []
        self._managed = managed

    def is_managed(self) -> bool:
        return self._managed


class _StubTransport:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _StubEngine:
    """Mirrors chess.engine.UciProtocol surface used by the supervisor."""

    def __init__(self, options: dict | None = None, id_name: str | None = None) -> None:
        self.options = options or {
            "Threads": _OptionDef("Threads"),
            "Hash": _OptionDef("Hash"),
            "MultiPV": _OptionDef("MultiPV", managed=True),
        }
        self.id = {"name": id_name} if id_name else {}
        self.transport = _StubTransport()
        self.configured: dict = {}
        self.send_lines: list[str] = []
        self.quit_calls = 0
        self.configure_error: Exception | None = None
        self._send_line_orig = lambda line: self.send_lines.append(line)
        self.send_line = self._send_line_orig
        self.line_received = lambda line: None

    async def configure(self, options: dict) -> None:
        if self.configure_error is not None:
            raise self.configure_error
        self.configured.update(options)

    async def quit(self) -> None:
        self.quit_calls += 1


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def stub_engine() -> _StubEngine:
    return _StubEngine(id_name="StubEngine")


@pytest.fixture
def supervisor(bus, stub_engine) -> EngineSupervisor:
    sup = EngineSupervisor(engine_path="/fake/engine", bus=bus)

    async def fake_popen_uci(command, **kwargs):
        fake_popen_uci.last_command = command
        fake_popen_uci.last_kwargs = kwargs
        return (stub_engine.transport, stub_engine)

    fake_popen_uci.last_command = None
    fake_popen_uci.last_kwargs = None
    sup._popen_uci = fake_popen_uci  # injection point for tests
    return sup


# ---------------------------------------------------------------------------
# spawn() -- options, args, env, overrides, platform flags
# ---------------------------------------------------------------------------


async def test_spawn_applies_options_filters_managed(supervisor, stub_engine):
    supervisor.options = {"Threads": 4, "Hash": 256, "MultiPV": 3, "Unknown": "x"}
    await supervisor.spawn()
    assert stub_engine.configured == {"Threads": 4, "Hash": 256}


async def test_spawn_includes_overrides_for_analysis(supervisor, stub_engine):
    supervisor.options = {"Threads": 1}
    await supervisor.spawn(overrides={"Threads": 8})
    assert stub_engine.configured == {"Threads": 8}


async def test_spawn_uses_windows_creation_flag(supervisor):
    with patch("sturddle_view.engines.sys") as mock_sys:
        mock_sys.platform = "win32"
        await supervisor.spawn()
    kwargs = supervisor._popen_uci.last_kwargs
    import subprocess
    assert kwargs.get("creationflags") == subprocess.CREATE_NO_WINDOW


async def test_spawn_no_creation_flag_on_posix(supervisor):
    with patch("sturddle_view.engines.sys") as mock_sys:
        mock_sys.platform = "linux"
        await supervisor.spawn()
    kwargs = supervisor._popen_uci.last_kwargs
    assert "creationflags" not in kwargs


async def test_spawn_env_overlays_parent(supervisor, monkeypatch):
    monkeypatch.setenv("PARENT_VAR", "parent")
    supervisor.env = {"CHILD_VAR": "child"}
    await supervisor.spawn()
    env = supervisor._popen_uci.last_kwargs["env"]
    assert env["PARENT_VAR"] == "parent"
    assert env["CHILD_VAR"] == "child"


async def test_spawn_args_list_appended_to_command(supervisor):
    supervisor.args = ["--profile", "fast"]
    await supervisor.spawn()
    assert supervisor._popen_uci.last_command == ["/fake/engine", "--profile", "fast"]


async def test_spawn_no_args_passes_bare_path(supervisor):
    await supervisor.spawn()
    assert supervisor._popen_uci.last_command == "/fake/engine"


async def test_spawn_configure_error_logged_not_raised(supervisor, stub_engine):
    stub_engine.configure_error = chess.engine.EngineError("nope")
    supervisor.options = {"Threads": 4}
    # Must not raise -- audit behavior: log + continue with un-configured engine.
    engine = await supervisor.spawn()
    assert engine is stub_engine


# ---------------------------------------------------------------------------
# ensure() -- lazy singleton + engine_name from id
# ---------------------------------------------------------------------------


async def test_ensure_spawns_on_first_call(supervisor, stub_engine):
    engine = await supervisor.ensure()
    assert engine is stub_engine
    assert supervisor.engine is stub_engine


async def test_ensure_reuses_existing_engine(supervisor, stub_engine):
    await supervisor.ensure()
    spawn_count_before = supervisor._popen_uci.last_command  # sentinel
    e2 = await supervisor.ensure()
    assert e2 is stub_engine


async def test_ensure_fills_engine_name_from_uci_id(supervisor):
    await supervisor.ensure()
    assert supervisor.engine_name == "StubEngine"


async def test_ensure_fallback_engine_name_from_basename(bus):
    sup = EngineSupervisor(engine_path="/fake/myengine.exe", bus=bus)
    stub = _StubEngine(id_name=None)

    async def fake_popen_uci(command, **kwargs):
        return (stub.transport, stub)

    sup._popen_uci = fake_popen_uci
    await sup.ensure()
    assert supervisor_name(sup) in ("myengine.exe", "myengine")


def supervisor_name(sup: EngineSupervisor) -> str:
    return sup.engine_name or ""


async def test_ensure_preserves_explicit_engine_name(supervisor):
    supervisor.engine_name = "UserOverride"
    await supervisor.ensure()
    assert supervisor.engine_name == "UserOverride"


# ---------------------------------------------------------------------------
# cancel() -- stop, grace, transport.close
# ---------------------------------------------------------------------------


async def test_cancel_search_sends_stop(supervisor, stub_engine):
    await supervisor.ensure()
    done_task = asyncio.create_task(asyncio.sleep(0))
    await asyncio.sleep(0)
    await supervisor.cancel(think_task=done_task, analysis=None)
    assert "stop" in stub_engine.send_lines


async def test_cancel_search_uses_analysis_stop_when_available(supervisor, stub_engine):
    await supervisor.ensure()

    class _Analysis:
        def __init__(self) -> None:
            self.stopped = 0

        def stop(self) -> None:
            self.stopped += 1

    analysis = _Analysis()
    done_task = asyncio.create_task(asyncio.sleep(0))
    await asyncio.sleep(0)
    await supervisor.cancel(think_task=done_task, analysis=analysis)
    assert analysis.stopped == 1
    assert "stop" not in stub_engine.send_lines


async def test_cancel_search_falls_back_to_transport_close_on_timeout(supervisor, stub_engine):
    await supervisor.ensure()

    async def wedged() -> None:
        await asyncio.sleep(5.0)

    think_task = asyncio.create_task(wedged())
    await supervisor.cancel(think_task=think_task, analysis=None)
    assert stub_engine.transport.closed
    assert supervisor.engine is None
    think_task.cancel()
    try:
        await think_task
    except (asyncio.CancelledError, Exception):
        pass


async def test_cancel_no_task_is_noop(supervisor, stub_engine):
    await supervisor.ensure()
    await supervisor.cancel(think_task=None, analysis=None)
    assert "stop" not in stub_engine.send_lines
    assert not stub_engine.transport.closed


# ---------------------------------------------------------------------------
# quit() -- idempotent
# ---------------------------------------------------------------------------


async def test_quit_engine_idempotent(supervisor, stub_engine):
    await supervisor.ensure()
    await supervisor.quit()
    await supervisor.quit()
    assert stub_engine.quit_calls == 1
    assert supervisor.engine is None


async def test_quit_clears_uci_log_tasks(supervisor):
    await supervisor.ensure()
    # Patch installs send_line wrapper that creates publish tasks.
    supervisor.engine.send_line("test-line")
    await asyncio.sleep(0)
    await supervisor.quit()
    assert len(supervisor._uci_log_tasks) == 0


# ---------------------------------------------------------------------------
# swap() -- clears per-engine state
# ---------------------------------------------------------------------------


async def test_swap_clears_options_and_args_and_env_and_name(supervisor, bus):
    supervisor.options = {"Threads": 4}
    supervisor.args = ["--profile", "x"]
    supervisor.env = {"FOO": "bar"}
    supervisor.engine_name = "Old"
    await supervisor.ensure()
    await supervisor.swap("/fake/other")
    assert supervisor.engine_path == "/fake/other"
    assert supervisor.options == {}
    assert supervisor.args == []
    assert supervisor.env == {}
    assert supervisor.engine_name is None
    assert supervisor.engine is None


# ---------------------------------------------------------------------------
# apply_settings_live() -- quit so next ensure respawns
# ---------------------------------------------------------------------------


async def test_apply_settings_live_kills_engine_for_respawn(supervisor, stub_engine):
    await supervisor.ensure()
    await supervisor.apply_settings_live()
    assert supervisor.engine is None
    assert stub_engine.quit_calls == 1


# ---------------------------------------------------------------------------
# UCI log fanout
# ---------------------------------------------------------------------------


async def test_uci_log_emits_send_and_recv(bus, supervisor):
    queue = await bus.subscribe()
    await supervisor.ensure()
    supervisor.engine.send_line("sent-line")
    supervisor.engine.line_received("recv-line")
    received: list[dict] = []
    for _ in range(2):
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert event.kind == "uci_log"
        received.append(event.payload)
    dirs = sorted(p["dir"] for p in received)
    lines = sorted(p["line"] for p in received)
    assert dirs == ["<", ">"]
    assert lines == ["recv-line", "sent-line"]
