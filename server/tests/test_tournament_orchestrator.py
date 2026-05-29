"""Slice 4: Orchestrator — composes Store + Runner.

Two layers of tests:

  1. Unit tests with a fake Runner — exercise the orchestrator's logic
     (single-active invariant, status persistence, reconciliation).
  2. One integration test that drives the orchestrator end-to-end with
     the real ``FastchessRunner`` + fake-fastchess script, proving the
     wiring works.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from sturddle_view.tournament.fastchess import FastchessRunner
from sturddle_view.tournament.orchestrator import (
    Orchestrator,
    TournamentBusyError,
)
from sturddle_view.tournament.rescheck import HostSpecs, RescheckError
from sturddle_view.tournament.runner import RunSpec
from sturddle_view.tournament.store import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_STOPPED,
    TournamentNotFoundError,
    TournamentStore,
)


# ---------------------------------------------------------------------------
# Fake Runner — pure unit testing
# ---------------------------------------------------------------------------


class _FakeRunner:
    """Manually controllable Runner. Captures start args; exit is driven
    by tests calling ``finish(kind, payload)``."""

    def __init__(self) -> None:
        self.binary_path = "/fake/fastchess"
        self.started: list[RunSpec] = []
        self._on_event = None
        self._running = False

    async def start(self, spec, on_event):
        if self._running:
            raise RuntimeError("already running")
        self.started.append(spec)
        self._on_event = on_event
        self._running = True

    async def stop(self):
        if not self._running:
            return
        self._running = False
        await self._on_event("stopped", {"rc": -9})

    def is_running(self):
        return self._running

    # Test helper: drive a terminal event from outside.
    async def finish(self, kind: str, payload: dict | None = None):
        assert self._running, "finish() called when not running"
        self._running = False
        await self._on_event(kind, payload or {"rc": 0})


@pytest.fixture
def store(tmp_path):
    return TournamentStore(tmp_path / "tournaments")


@pytest.fixture
def runner():
    return _FakeRunner()


@pytest.fixture
def orch(store, runner):
    return Orchestrator(store, runner)


def _create(store, **kw) -> str:
    t = store.create(
        name=kw.get("name", "t"),
        template=kw.get("template", {}),
        engines=kw.get("engines", [{"name": "A", "cmd": "/x"}, {"name": "B", "cmd": "/y"}]),
        engine_defaults=kw.get("engine_defaults"),
    )
    return t.id


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


async def test_start_marks_running_and_sets_active_id(store, runner, orch):
    tid = _create(store)
    assert orch.active_id() is None

    t = await orch.start(tid)
    assert t.status == STATUS_RUNNING
    assert t.started_at is not None
    assert orch.active_id() == tid
    assert runner.is_running()
    # store agrees
    assert store.get(tid).status == STATUS_RUNNING


async def test_start_unknown_id_raises(orch):
    with pytest.raises(TournamentNotFoundError):
        await orch.start("does-not-exist")


async def test_start_rejects_second_when_active(store, runner, orch):
    a = _create(store, name="a")
    b = _create(store, name="b")
    await orch.start(a)
    with pytest.raises(TournamentBusyError):
        await orch.start(b)
    # b unchanged on disk
    assert store.get(b).status == "idle"


async def test_start_rolls_back_on_runner_failure(store, monkeypatch):
    bad = _FakeRunner()

    async def boom(spec, on_event):
        raise RuntimeError("simulated fastchess explosion")

    bad.start = boom  # type: ignore[assignment]
    orch = Orchestrator(store, bad)
    tid = _create(store)

    with pytest.raises(RuntimeError, match="simulated"):
        await orch.start(tid)

    # active cleared, status rolled back to STOPPED so user sees the failure
    assert orch.active_id() is None
    final = store.get(tid)
    assert final.status == STATUS_STOPPED
    assert final.stopped_at is not None


async def test_start_stops_runner_on_post_spawn_failure(store):
    # Fastchess.start() can raise AFTER spawning the subprocess (e.g.
    # assign_to_job throws, or the started-event emit fails). Without
    # the rollback calling runner.stop(), the subprocess is orphaned.
    bad = _FakeRunner()

    async def boom(spec, on_event):
        bad._running = True
        bad._on_event = on_event
        bad.started.append(spec)
        raise RuntimeError("boom after spawn")

    bad.start = boom  # type: ignore[assignment]
    orch = Orchestrator(store, bad)
    tid = _create(store)

    with pytest.raises(RuntimeError, match="boom after spawn"):
        await orch.start(tid)

    assert not bad.is_running(), "orphaned runner after failed start"
    assert orch.active_id() is None
    assert store.get(tid).status == STATUS_STOPPED


async def test_start_passes_correct_runspec(store, runner, orch):
    tid = _create(store, template={"tc": "10+0.1"})
    await orch.start(tid)
    spec = runner.started[0]
    assert spec.tournament.id == tid
    assert spec.work_dir == store.root / tid
    assert spec.pgn_path == store.pgn_path(tid)
    assert spec.config_path == store.config_path(tid)
    assert spec.log_path.name == "fastchess.log"
    assert spec.log_path.parent.name == "logs"


async def test_start_uses_frozen_engine_defaults_not_live_settings(store, runner, orch):
    # Tournament was created with one set of engine defaults; live settings
    # later differ — start() must use the frozen snapshot.
    tid = _create(store, engine_defaults={
        "threads": 2,
        "hash_mb": 128,
        "syzygy_path": "/frozen/tb",
        "book_path": "/frozen/book.pgn",
        "book_plies": 8,
        "book_order": "sequential",
    })

    class _LiveSettings:
        engine_default_threads = 99
        engine_default_hash_mb = 9999
        engine_default_syzygy_path = "/live/tb"
        engine_default_book_path = "/live/book.pgn"
        engine_default_book_plies = 99
        engine_default_book_order = "random"
    orch.set_settings(_LiveSettings())

    await orch.start(tid)
    spec = runner.started[0]
    assert spec.engine_default_threads == 2
    assert spec.engine_default_hash_mb == 128
    assert spec.engine_default_syzygy_path == "/frozen/tb"
    assert spec.engine_default_book_path == "/frozen/book.pgn"
    assert spec.engine_default_book_plies == 8
    assert spec.engine_default_book_order == "sequential"


async def test_start_frozen_none_overrides_live_settings(store, runner, orch):
    # Tournament was created when Settings was empty — every key snapshotted
    # as None. Later Settings changes must not leak in: explicit None wins
    # over live Settings, just like any other frozen value.
    tid = _create(store, engine_defaults={
        "threads": None, "hash_mb": None, "syzygy_path": None,
        "book_path": None, "book_plies": None, "book_order": None,
    })

    class _LiveSettings:
        engine_default_threads = 99
        engine_default_hash_mb = 9999
        engine_default_book_path = "/live/book.pgn"
        engine_default_book_plies = 99
        engine_default_book_order = "random"
        engine_default_syzygy_path = "/live/tb"
    orch.set_settings(_LiveSettings())

    await orch.start(tid)
    spec = runner.started[0]
    assert spec.engine_default_threads is None
    assert spec.engine_default_hash_mb is None
    assert spec.engine_default_book_path is None


async def test_start_falls_back_to_live_settings_for_legacy_tournaments(store, runner, orch):
    # No engine_defaults snapshot (pre-snapshot tournament) → live Settings used.
    tid = _create(store)  # engine_defaults defaults to {}

    class _LiveSettings:
        engine_default_threads = 7
        engine_default_hash_mb = 512
        engine_default_syzygy_path = None
        engine_default_book_path = None
        engine_default_book_plies = None
        engine_default_book_order = None
    orch.set_settings(_LiveSettings())

    await orch.start(tid)
    spec = runner.started[0]
    assert spec.engine_default_threads == 7
    assert spec.engine_default_hash_mb == 512


async def test_start_busy_when_runner_running_without_active_id(store, runner, orch):
    """`_runner.is_running()` alone (no active_id) must still reject.
    Kills `or`→`and` mutation on the busy guard."""
    runner._running = True  # simulate orphaned runner
    tid = _create(store)
    with pytest.raises(TournamentBusyError):
        await orch.start(tid)


async def test_start_busy_when_active_id_set_but_store_raises(store, runner, orch):
    """active_id is set but store.get() raises → busy without name.
    Kills AddNot on `if active:` branch."""
    orch._active_id = "ghost-id"  # stale id not in store
    tid = _create(store)
    with pytest.raises(TournamentBusyError):
        await orch.start(tid)


async def test_start_passes_paired_false_for_single_game_tournament(store, runner, monkeypatch):
    """games_per_round=1 → paired=False passed to rewrite.
    Kills `!= 1`→`!= 2` / AddNot mutations."""
    calls = []

    def fake_rewrite(pgn_path, config_path, ts, *, paired, patch_config=True):
        calls.append(paired)
        return (0, {})

    monkeypatch.setattr(
        "sturddle_view.tournament.orchestrator.rewrite_drop_partial_pairs",
        fake_rewrite,
    )
    orch = Orchestrator(store, runner)
    tid = _create(store, template={"games_per_round": 1})
    await orch.start(tid)
    assert calls == [False]


async def test_start_passes_paired_true_for_default_tournament(store, runner, monkeypatch):
    """games_per_round defaults to 2 → paired=True."""
    calls = []

    def fake_rewrite(pgn_path, config_path, ts, *, paired, patch_config=True):
        calls.append(paired)
        return (0, {})

    monkeypatch.setattr(
        "sturddle_view.tournament.orchestrator.rewrite_drop_partial_pairs",
        fake_rewrite,
    )
    orch = Orchestrator(store, runner)
    tid = _create(store, template={})  # no games_per_round → default 2
    await orch.start(tid)
    assert calls == [True]


# ---------------------------------------------------------------------------
# stop + terminal events
# ---------------------------------------------------------------------------


async def test_stop_active_marks_stopped_and_clears_active(store, runner, orch):
    tid = _create(store)
    await orch.start(tid)
    await orch.stop(tid)

    assert orch.active_id() is None
    final = store.get(tid)
    assert final.status == STATUS_STOPPED
    assert final.stopped_at is not None
    assert final.started_at is not None


async def test_clean_exit_marks_done(store, runner, orch):
    tid = _create(store)
    await orch.start(tid)
    await runner.finish("done")

    assert orch.active_id() is None
    assert store.get(tid).status == STATUS_DONE


async def test_runner_crash_marks_failed_with_last_error(store, runner, orch):
    """runner_crash → STATUS_FAILED + last_error persisted with stderr_tail."""
    tid = _create(store)
    await orch.start(tid)
    await runner.finish("runner_crash", {
        "rc": 137,
        "stderr_tail": ["Error; no TimeControl specified!"],
    })

    assert orch.active_id() is None
    final = store.get(tid)
    assert final.status == STATUS_FAILED
    assert final.last_error is not None
    assert final.last_error["rc"] == 137
    assert final.last_error["stderr_tail"] == ["Error; no TimeControl specified!"]
    assert final.last_error["at"]


async def test_terminal_event_finalizes_and_clears_tailer(store, runner, orch, tmp_path):
    """_pgn_tailer is not None on terminal event → finalize() called and
    tailer set to None. Kills `is not None`→`is None` mutation."""
    from sturddle_view.tournament.pgn_tail import PgnTailer

    tid = _create(store)
    await orch.start(tid)

    pgn_path = tmp_path / "games.pgn"
    pgn_path.touch()
    orch._pgn_tailer = PgnTailer(pgn_path, orch._on_pgn_record, poll_interval=0.01)

    await runner.finish("done")

    assert orch._pgn_tailer is None
    assert orch.active_id() is None


async def test_terminal_event_no_tailer_does_not_crash(store, runner, orch):
    """_pgn_tailer explicitly None → terminal event skips finalize, no crash.
    Paired with above to pin both branches of the tailer-is-not-None guard."""
    tid = _create(store)
    await orch.start(tid)
    orch._pgn_tailer = None  # simulate: tailer never wired or already torn down

    await runner.finish("done")
    assert orch.active_id() is None


def _patch_specs(monkeypatch, *, logical=4, physical=2, total_ram_mb=8192):
    monkeypatch.setattr(
        "sturddle_view.tournament.rescheck.host_specs",
        lambda: HostSpecs(logical, physical, total_ram_mb),
    )


async def test_rescheck_blocks_cpu_oversubscription(store, runner, orch, monkeypatch):
    _patch_specs(monkeypatch, logical=4, physical=2)
    tid = _create(
        store,
        template={"games_in_parallel": 8, "max_threads": 1, "max_hash_mb": 16},
    )
    with pytest.raises(RescheckError):
        await orch.start(tid)
    final = store.get(tid)
    assert final.status == STATUS_FAILED
    assert final.last_error["rescheck"]["reason"] == "oversubscribed"
    assert orch.active_id() is None
    assert not runner.is_running()


async def test_rescheck_allow_oversubscribe_silences_cpu_block(store, runner, orch, monkeypatch):
    _patch_specs(monkeypatch, logical=4, physical=2)
    tid = _create(
        store,
        template={
            "games_in_parallel": 8, "max_threads": 1, "max_hash_mb": 16,
            "allow_oversubscribe": True,
        },
    )
    await orch.start(tid)
    assert store.get(tid).status == STATUS_RUNNING


async def test_rescheck_blocks_affinity_over_physical(store, runner, orch, monkeypatch):
    _patch_specs(monkeypatch, logical=8, physical=4)
    tid = _create(
        store,
        template={
            "games_in_parallel": 5, "max_threads": 1, "max_hash_mb": 16,
            "pin_affinity": True,
        },
    )
    with pytest.raises(RescheckError):
        await orch.start(tid)
    assert store.get(tid).last_error["rescheck"]["reason"] == "affinity_exceeds_physical"


async def test_rescheck_affinity_block_not_silenced_by_oversubscribe(store, runner, orch, monkeypatch):
    _patch_specs(monkeypatch, logical=8, physical=4)
    tid = _create(
        store,
        template={
            "games_in_parallel": 5, "max_threads": 1, "max_hash_mb": 16,
            "pin_affinity": True, "allow_oversubscribe": True,
        },
    )
    with pytest.raises(RescheckError):
        await orch.start(tid)


async def test_rescheck_blocks_ram(store, runner, orch, monkeypatch):
    # 4 parallel * 2 engines * (4096 + 256) MB overhead = 34816 MB
    # Budget = 0.75 * 8192 = 6144 MB → blocks.
    _patch_specs(monkeypatch, logical=8, physical=4, total_ram_mb=8192)
    tid = _create(
        store,
        template={"games_in_parallel": 4, "max_threads": 1, "max_hash_mb": 4096},
    )
    with pytest.raises(RescheckError):
        await orch.start(tid)
    assert store.get(tid).last_error["rescheck"]["reason"] == "insufficient_ram"


async def test_rescheck_allow_oversubscribe_silences_ram_block(store, runner, orch, monkeypatch):
    _patch_specs(monkeypatch, logical=8, physical=4, total_ram_mb=8192)
    tid = _create(
        store,
        template={
            "games_in_parallel": 4, "max_threads": 1, "max_hash_mb": 4096,
            "allow_oversubscribe": True,
        },
    )
    await orch.start(tid)
    assert store.get(tid).status == STATUS_RUNNING


async def test_restart_clears_last_error(store, runner, orch):
    tid = _create(store)
    await orch.start(tid)
    await runner.finish("runner_crash", {"rc": 1, "stderr_tail": ["bad"]})
    assert store.get(tid).last_error is not None

    await orch.start(tid)
    assert store.get(tid).last_error is None
    await orch.stop(tid)


async def test_stop_when_not_active_is_noop(store, runner, orch):
    tid = _create(store)
    # never started
    result = await orch.stop(tid)
    assert result.status == "idle"
    assert orch.active_id() is None


async def test_stop_idempotent(store, runner, orch):
    tid = _create(store)
    await orch.start(tid)
    await orch.stop(tid)
    await orch.stop(tid)  # second call must not raise
    assert store.get(tid).status == STATUS_STOPPED


async def test_can_start_again_after_stop(store, runner, orch):
    a = _create(store, name="a")
    b = _create(store, name="b")
    await orch.start(a)
    await orch.stop(a)
    # Now a different one can start
    await orch.start(b)
    assert orch.active_id() == b
    await orch.stop(b)


# ---------------------------------------------------------------------------
# verify_proxy_secret
# ---------------------------------------------------------------------------


def test_verify_proxy_secret_matches(orch):
    """Correct secret → True."""
    orch._proxy_secret = "correct"
    assert orch.verify_proxy_secret("correct") is True


def test_verify_proxy_secret_wrong(orch):
    """Wrong secret → False."""
    orch._proxy_secret = "correct"
    assert orch.verify_proxy_secret("wrong") is False


def test_verify_proxy_secret_none_secret_returns_false(orch):
    """_proxy_secret is None (no active tournament) → False regardless of
    what is presented. Kills `or`→`and` (left side) and `False`→`True`
    mutations on the secret-presence guard."""
    orch._proxy_secret = None
    assert orch.verify_proxy_secret("anything") is False


def test_verify_proxy_secret_none_presented_returns_false(orch):
    """presented is None → False. Kills `or`→`and` (right side) on the secret-presence guard."""
    orch._proxy_secret = "set"
    assert orch.verify_proxy_secret(None) is False


def test_verify_proxy_secret_both_none_returns_false(orch):
    """Both None → False."""
    orch._proxy_secret = None
    assert orch.verify_proxy_secret(None) is False


# ---------------------------------------------------------------------------
# reconciliation
# ---------------------------------------------------------------------------


def test_reconcile_marks_stale_running_as_failed_with_diagnostic(store, runner, orch):
    a = _create(store, name="a")
    b = _create(store, name="b")
    c = _create(store, name="c")
    # Simulate a server crash mid-tournament: state.json says running
    # but no process exists.
    store.update_status(a, STATUS_RUNNING, started_at="x")
    store.update_status(c, STATUS_RUNNING, started_at="x")

    reconciled = orch.reconcile_on_startup()
    assert {t.id for t in reconciled} == {a, c}
    assert store.get(a).status == STATUS_FAILED
    assert store.get(b).status == "idle"
    assert store.get(c).status == STATUS_FAILED
    # Synthetic last_error so the UI surfaces *why* it's failed.
    err = store.get(a).last_error
    assert err is not None
    assert err["rc"] is None
    assert err["stderr_tail"]
    assert "Server was killed" in err["stderr_tail"][0]


def test_reconcile_noop_when_nothing_running(store, runner, orch):
    _create(store)
    assert orch.reconcile_on_startup() == []


# ---------------------------------------------------------------------------
# broadcast wiring
# ---------------------------------------------------------------------------


async def test_broadcast_receives_status_change_and_runner_events(store, runner, orch):
    events: list[tuple[str, dict]] = []

    async def cb(kind, payload):
        events.append((kind, payload))

    orch.set_broadcast(cb)
    tid = _create(store)
    await orch.start(tid)
    await runner.finish("done")

    kinds = [k for k, _ in events]
    # status_change emitted at least twice: when start() flips to running,
    # and when terminal event flips to done.
    assert kinds.count("status_change") >= 2
    assert "started" not in kinds  # we don't emit synthetic 'started';
    # the runner emits its own 'started' which is forwarded:
    # so it might appear because we forward all runner events.
    assert "done" in kinds


async def test_broadcast_failure_does_not_break_orchestrator(store, runner, orch):
    async def bad(kind, payload):
        raise RuntimeError("downstream crashed")

    orch.set_broadcast(bad)
    tid = _create(store)
    # Should not propagate
    await orch.start(tid)
    await runner.finish("done")
    assert store.get(tid).status == STATUS_DONE


# ---------------------------------------------------------------------------
# Integration: real FastchessRunner + fake fastchess
# ---------------------------------------------------------------------------


FAKE_FASTCHESS = r"""
import sys, time
i = 1
rc = 0
while i < len(sys.argv):
    a = sys.argv[i]
    if a == "--print":
        n = int(sys.argv[i+1]); i += 2
        for k in range(n):
            print(f"out {k}", flush=True)
    elif a == "--sleep":
        time.sleep(float(sys.argv[i+1])); i += 2
    elif a == "--exit":
        rc = int(sys.argv[i+1]); i += 2
    else:
        i += 1
sys.exit(rc)
"""


async def test_integration_real_runner_clean_exit(tmp_path, monkeypatch):
    """End-to-end: orchestrator → FastchessRunner → fake fastchess process →
    store reflects DONE on natural exit."""
    store = TournamentStore(tmp_path / "tournaments")
    runner = FastchessRunner(binary_path=sys.executable)
    monkeypatch.setattr(
        FastchessRunner, "detect_binary",
        staticmethod(lambda configured: configured),
    )
    from sturddle_view.tournament import fastchess as fc_mod
    monkeypatch.setattr(
        fc_mod, "build_command",
        lambda spec: [sys.executable, "-c", FAKE_FASTCHESS, "--print", "5", "--exit", "0"],
    )

    orch = Orchestrator(store, runner)
    done_evt = asyncio.Event()
    captured: list[tuple[str, dict]] = []

    async def cb(kind, payload):
        captured.append((kind, payload))
        if kind == "status_change" and payload.get("status") == STATUS_DONE:
            done_evt.set()

    orch.set_broadcast(cb)

    t = store.create(name="int", template={}, engines=[
        {"name": "A", "cmd": "/x"}, {"name": "B", "cmd": "/y"}
    ])
    await orch.start(t.id)
    await done_evt.wait()

    assert orch.active_id() is None
    assert store.get(t.id).status == STATUS_DONE
    out_lines = [p["line"] for k, p in captured if k == "runner_log" and p.get("stream") == "out"]
    assert "out 0" in out_lines
    assert "out 4" in out_lines


async def test_integration_real_runner_stop(tmp_path, monkeypatch):
    """End-to-end: orchestrator.stop() → FastchessRunner kills process →
    store reflects STOPPED."""
    store = TournamentStore(tmp_path / "tournaments")
    runner = FastchessRunner(binary_path=sys.executable)
    monkeypatch.setattr(
        FastchessRunner, "detect_binary",
        staticmethod(lambda configured: configured),
    )
    from sturddle_view.tournament import fastchess as fc_mod
    monkeypatch.setattr(
        fc_mod, "build_command",
        lambda spec: [sys.executable, "-c", FAKE_FASTCHESS, "--sleep", "30"],
    )

    orch = Orchestrator(store, runner)
    stop_evt = asyncio.Event()

    async def cb(kind, payload):
        if kind == "status_change" and payload.get("status") == STATUS_STOPPED:
            stop_evt.set()

    orch.set_broadcast(cb)

    t = store.create(name="int", template={}, engines=[
        {"name": "A", "cmd": "/x"}, {"name": "B", "cmd": "/y"}
    ])
    await orch.start(t.id)
    await orch.stop(t.id)
    await stop_evt.wait()


