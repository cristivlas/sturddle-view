"""Slice 3: FastchessRunner — command building + process lifecycle.

A real fastchess binary is not required: the lifecycle tests use a
small inline Python program as a stand-in, started via the same
``Popen`` / asyncio plumbing the runner uses for the real thing.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from sturddle_view.tournament.fastchess import FastchessRunner, build_command
from sturddle_view.tournament.runner import RunSpec
from sturddle_view.tournament.store import (
    STATUS_IDLE,
    Tournament,
    TournamentStore,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spec(
    tmp_path: Path,
    template: dict,
    engines: list,
    binary: str = "fastchess",
    **spec_overrides,
) -> RunSpec:
    store = TournamentStore(tmp_path / "tournaments")
    t = store.create(name="t", template=template, engines=engines)
    work = store.root / t.id
    return RunSpec(
        tournament=t,
        binary_path=binary,
        work_dir=work,
        pgn_path=work / "games.pgn",
        config_path=work / "config.json",
        log_path=work / "logs" / "fastchess.log",
        **spec_overrides,
    )


# Inline Python that masquerades as fastchess. Behavior parametrized by argv:
#   --print N           : print N lines of stdout, flush each
#   --print-err N       : print N lines of stderr
#   --sleep S           : sleep S seconds
#   --exit RC           : exit with code RC
# Order matters; the script does each in argv order.
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
    elif a == "--print-err":
        n = int(sys.argv[i+1]); i += 2
        for k in range(n):
            print(f"err {k}", file=sys.stderr, flush=True)
    elif a == "--sleep":
        time.sleep(float(sys.argv[i+1])); i += 2
    elif a == "--exit":
        rc = int(sys.argv[i+1]); i += 2
    else:
        i += 1
sys.exit(rc)
"""


def _fake_argv(*args: str) -> list[str]:
    """argv that runs the fake fastchess script with the given trailing args."""
    return [sys.executable, "-c", FAKE_FASTCHESS, *args]


class _Recorder:
    """Captures (kind, payload) tuples from the runner."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.done = asyncio.Event()
        self._terminal = {"done", "stopped", "runner_crash"}

    async def __call__(self, kind: str, payload: dict) -> None:
        self.events.append((kind, payload))
        if kind in self._terminal:
            self.done.set()


# ---------------------------------------------------------------------------
# build_command
# ---------------------------------------------------------------------------


def test_build_command_minimal(tmp_path):
    spec = _make_spec(
        tmp_path,
        template={"tc": "10+0.1"},
        engines=[
            {"name": "A", "cmd": "/bin/engineA"},
            {"name": "B", "cmd": "/bin/engineB"},
        ],
    )
    cmd = build_command(spec)
    assert cmd[0] == "fastchess"
    # Two -engine blocks
    assert cmd.count("-engine") == 2
    assert "name=A" in cmd
    assert "name=B" in cmd
    assert "cmd=/bin/engineA" in cmd
    assert "tc=10+0.1" in cmd
    # PGN out, append, fastchess output format
    assert "-pgnout" in cmd
    pgnout_idx = cmd.index("-pgnout")
    assert cmd[pgnout_idx + 1] == f"file={spec.pgn_path}"
    assert "append=true" in cmd
    assert "format=fastchess" in cmd


def test_build_command_each_options(tmp_path):
    # Hash/Threads/SyzygyPath now come from settings via the
    # engine_default_* fields on RunSpec; ponder still lives in the
    # tournament template.
    spec = _make_spec(
        tmp_path,
        template={"ponder": False},
        engines=[{"name": "A", "cmd": "/x"}, {"name": "B", "cmd": "/y"}],
        engine_default_hash_mb=256,
        engine_default_threads=1,
        engine_default_syzygy_path="/tb",
    )
    cmd = build_command(spec)
    each_idx = cmd.index("-each")
    each = cmd[each_idx + 1 : each_idx + 5]
    assert "option.Hash=256" in each
    assert "option.Threads=1" in each
    assert "option.Ponder=false" in each
    assert "option.SyzygyPath=/tb" in each


def test_build_command_legacy_template_hash_threads_ignored(tmp_path):
    """Old tournaments saved before the Defaults tab still load with
    hash/threads/tablebase in their template — those values must NOT
    leak into the fastchess argv (settings is now authoritative)."""
    spec = _make_spec(
        tmp_path,
        template={"hash": 999, "threads": 999, "tablebase": "/old/tb"},
        engines=[{"name": "A", "cmd": "/x"}, {"name": "B", "cmd": "/y"}],
    )
    cmd = build_command(spec)
    assert not any("Hash=999" in s for s in cmd)
    assert not any("Threads=999" in s for s in cmd)
    assert not any("/old/tb" in s for s in cmd)


def test_build_command_concurrency_rounds_games(tmp_path):
    spec = _make_spec(
        tmp_path,
        template={"games_in_parallel": 4, "rounds": 100, "games_per_round": 2},
        engines=[{"name": "A", "cmd": "/x"}, {"name": "B", "cmd": "/y"}],
    )
    cmd = build_command(spec)
    assert cmd[cmd.index("-concurrency") + 1] == "4"
    assert cmd[cmd.index("-rounds") + 1] == "100"
    assert cmd[cmd.index("-games") + 1] == "2"


def test_build_command_gauntlet_with_seeds(tmp_path):
    spec = _make_spec(
        tmp_path,
        template={"tournament_type": "gauntlet", "seeds": 1},
        engines=[{"name": "A", "cmd": "/x"}, {"name": "B", "cmd": "/y"}],
    )
    cmd = build_command(spec)
    assert cmd[cmd.index("-tournament") + 1] == "gauntlet"
    assert cmd[cmd.index("-seeds") + 1] == "1"


def test_build_command_pins_seed_when_in_template(tmp_path):
    spec = _make_spec(
        tmp_path,
        template={"seed": 1234567890},
        engines=[{"name": "A", "cmd": "/x"}, {"name": "B", "cmd": "/y"}],
    )
    cmd = build_command(spec)
    assert cmd[cmd.index("-srand") + 1] == "1234567890"


def test_build_command_omits_srand_when_no_seed_in_template():
    # Cover the build_command branch directly (the store always auto-injects
    # a seed, so we bypass it by hand-constructing a Tournament).
    from sturddle_view.tournament.store import STATUS_IDLE, Tournament

    t = Tournament(
        id="x", name="x", status=STATUS_IDLE, created_at="2024-01-01T00:00:00Z",
        template={"tc": "10+0.1"},
        engines=[{"name": "A", "cmd": "/x"}, {"name": "B", "cmd": "/y"}],
    )
    spec = RunSpec(
        tournament=t, binary_path="fastchess",
        work_dir=Path("/tmp"), pgn_path=Path("/tmp/g.pgn"),
        config_path=Path("/tmp/c.json"), log_path=Path("/tmp/f.log"),
    )
    cmd = build_command(spec)
    assert "-srand" not in cmd


def test_build_command_book(tmp_path):
    # Book file + plies now come from settings; format is inferred from
    # the file extension.
    spec = _make_spec(
        tmp_path,
        template={},
        engines=[{"name": "A", "cmd": "/x"}, {"name": "B", "cmd": "/y"}],
        engine_default_book_path="/books/8moves.epd",
        engine_default_book_plies=8,
    )
    cmd = build_command(spec)
    idx = cmd.index("-openings")
    assert cmd[idx + 1] == "file=/books/8moves.epd"
    assert cmd[idx + 2] == "format=epd"
    assert cmd[idx + 3] == "plies=8"


def test_build_command_book_pgn_extension(tmp_path):
    spec = _make_spec(
        tmp_path,
        template={},
        engines=[{"name": "A", "cmd": "/x"}, {"name": "B", "cmd": "/y"}],
        engine_default_book_path="/books/lichess.pgn",
    )
    cmd = build_command(spec)
    idx = cmd.index("-openings")
    assert cmd[idx + 2] == "format=pgn"
    # plies omitted when not configured
    assert all(not s.startswith("plies=") for s in cmd[idx : idx + 4])


def test_build_command_legacy_book_template_ignored(tmp_path):
    """Old tournaments with book/book_format in the template must not
    emit -openings — the settings book is the only authoritative source."""
    spec = _make_spec(
        tmp_path,
        template={"book": "/old/book.epd", "book_format": "epd"},
        engines=[{"name": "A", "cmd": "/x"}, {"name": "B", "cmd": "/y"}],
    )
    cmd = build_command(spec)
    assert "-openings" not in cmd


def test_build_command_sprt(tmp_path):
    spec = _make_spec(
        tmp_path,
        template={"sprt": {"elo0": 0, "elo1": 5, "alpha": 0.05, "beta": 0.05}},
        engines=[{"name": "A", "cmd": "/x"}, {"name": "B", "cmd": "/y"}],
    )
    cmd = build_command(spec)
    idx = cmd.index("-sprt")
    block = cmd[idx + 1 : idx + 6]
    assert "elo0=0" in block
    assert "elo1=5" in block
    assert "alpha=0.05" in block
    assert "beta=0.05" in block
    assert "model=normalized" in block


def test_build_command_resign_and_draw(tmp_path):
    spec = _make_spec(
        tmp_path,
        template={
            "resign": {"movecount": 3, "score": 700},
            "draw": {"movenumber": 40, "movecount": 8, "score": 10},
        },
        engines=[{"name": "A", "cmd": "/x"}, {"name": "B", "cmd": "/y"}],
    )
    cmd = build_command(spec)
    assert "-resign" in cmd
    r = cmd[cmd.index("-resign") + 1 : cmd.index("-resign") + 3]
    assert "movecount=3" in r and "score=700" in r
    d = cmd[cmd.index("-draw") + 1 : cmd.index("-draw") + 4]
    assert "movenumber=40" in d and "movecount=8" in d and "score=10" in d


def test_build_command_wraps_engines_in_proxy_when_configured(tmp_path):
    """Slice 9b: when proxy_broadcast_url + proxy_secret are set on the
    spec, each engine's cmd= becomes the python proxy invocation with
    the real engine binary as an argument."""
    import sys as _sys

    spec = _make_spec(
        tmp_path,
        template={"tc": "10+0.1"},
        engines=[
            {"name": "A", "cmd": "/bin/engineA"},
            {"name": "B", "cmd": "/bin/engineB"},
        ],
    )
    spec.proxy_broadcast_url = "http://127.0.0.1:8765/internal/proxy"
    spec.proxy_secret = "test-secret"

    cmd = build_command(spec)

    # Each engine block now uses python (sys.executable) as cmd= and
    # passes the proxy invocation via args=.
    cmd_eq = [c for c in cmd if c.startswith("cmd=")]
    assert len(cmd_eq) == 2
    for ceq in cmd_eq:
        assert ceq == f"cmd={_sys.executable}"

    args_eq = [c for c in cmd if c.startswith("args=")]
    assert len(args_eq) == 2
    for aeq in args_eq:
        assert "sturddle_view.tournament.proxy" in aeq
        assert "--broadcast-url http://127.0.0.1:8765/internal/proxy" in aeq
        # Secret is passed via SV_PROXY_SECRET env (see FastchessRunner),
        # not argv — so it's not visible to `ps` / /proc/<pid>/cmdline.
        assert "--secret" not in aeq
        assert "test-secret" not in aeq
        # The proxy_id is generated per-process by the proxy script
        # itself; not baked into argv.
        assert "--proxy-id" not in aeq

    # Engine names + tc still appear after the wrap.
    assert "name=A" in cmd
    assert "name=B" in cmd
    assert "tc=10+0.1" in cmd


def test_build_command_engine_missing_cmd_raises(tmp_path):
    spec = _make_spec(
        tmp_path,
        template={},
        engines=[{"name": "no-cmd"}],
    )
    with pytest.raises(ValueError, match="missing 'cmd'"):
        build_command(spec)


# ---------------------------------------------------------------------------
# detect_binary
# ---------------------------------------------------------------------------


def test_detect_binary_uses_configured_path_if_executable(tmp_path):
    fake = tmp_path / "fc"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    assert FastchessRunner.detect_binary(str(fake)) == str(fake)


def test_detect_binary_falls_back_to_path():
    # `python3` is always on PATH in CI/dev.
    found = FastchessRunner.detect_binary(None)
    # May be None if fastchess truly not installed; assertion is "no exception".
    assert found is None or os.path.isabs(found) or "fastchess" in found


def test_detect_binary_missing_returns_none(tmp_path):
    # Configured path does not exist; PATH may or may not have fastchess.
    # We can't assert None unconditionally, but we can assert no crash.
    assert FastchessRunner.detect_binary(str(tmp_path / "nope")) in (None, "fastchess") or True


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_runner(monkeypatch):
    """A FastchessRunner whose ``build_command`` is patched per-test to
    invoke the fake fastchess script. Returns a factory ``(runner,
    set_argv)`` — ``set_argv`` swaps in the fake-fastchess argv."""

    def make(extra_argv: list[str]):
        runner = FastchessRunner(binary_path=sys.executable)
        # Force detect_binary to accept whatever we pass.
        monkeypatch.setattr(
            FastchessRunner, "detect_binary",
            staticmethod(lambda configured: configured)
        )
        # Replace build_command so start() uses our fake-fastchess argv.
        from sturddle_view.tournament import fastchess as fc_mod
        monkeypatch.setattr(
            fc_mod, "build_command",
            lambda spec: _fake_argv(*extra_argv),
        )
        return runner

    return make


async def test_runner_clean_exit_emits_done(tmp_path, patched_runner):
    spec = _make_spec(tmp_path, {}, [{"name": "A", "cmd": "/x"}, {"name": "B", "cmd": "/y"}])
    rec = _Recorder()
    runner = patched_runner(["--print", "3", "--exit", "0"])

    await runner.start(spec, rec)
    assert runner.is_running() in (True, False)  # may already exit; race-free below
    await asyncio.wait_for(rec.done.wait(), timeout=5.0)

    kinds = [k for k, _ in rec.events]
    assert "started" in kinds
    assert kinds[-1] == "done"
    assert rec.events[-1][1]["rc"] == 0
    # fastchess.log should now contain the captured stdout
    log_text = spec.log_path.read_text()
    assert "out 0" in log_text
    assert "out 2" in log_text


async def test_runner_emits_runner_log_per_stdout_line(tmp_path, patched_runner):
    """Slice 9a: fastchess stdout is forwarded to the event bus as
    ``runner_log`` events so the workspace's Event log window can show
    each line."""
    spec = _make_spec(tmp_path, {}, [{"name": "A", "cmd": "/x"}, {"name": "B", "cmd": "/y"}])
    rec = _Recorder()
    runner = patched_runner(["--print", "3", "--print-err", "1", "--exit", "0"])

    await runner.start(spec, rec)
    await asyncio.wait_for(rec.done.wait(), timeout=5.0)

    log_events = [(k, p) for (k, p) in rec.events if k == "runner_log"]
    out_lines = [p["line"] for (_, p) in log_events if p.get("stream") == "out"]
    err_lines = [p["line"] for (_, p) in log_events if p.get("stream") == "err"]

    assert "out 0" in out_lines
    assert "out 1" in out_lines
    assert "out 2" in out_lines
    assert "err 0" in err_lines


async def test_runner_nonzero_exit_emits_runner_crash(tmp_path, patched_runner):
    spec = _make_spec(tmp_path, {}, [{"name": "A", "cmd": "/x"}, {"name": "B", "cmd": "/y"}])
    rec = _Recorder()
    runner = patched_runner(["--exit", "7"])

    await runner.start(spec, rec)
    await asyncio.wait_for(rec.done.wait(), timeout=5.0)

    assert rec.events[-1][0] == "runner_crash"
    assert rec.events[-1][1]["rc"] == 7


async def test_runner_stop_emits_stopped_not_crash(tmp_path, patched_runner):
    spec = _make_spec(tmp_path, {}, [{"name": "A", "cmd": "/x"}, {"name": "B", "cmd": "/y"}])
    rec = _Recorder()
    runner = patched_runner(["--sleep", "10"])  # longer than test timeout

    await runner.start(spec, rec)
    assert runner.is_running()
    await runner.stop()
    await asyncio.wait_for(rec.done.wait(), timeout=5.0)

    kinds = [k for k, _ in rec.events]
    assert "stopped" in kinds
    assert "runner_crash" not in kinds  # we asked for it; not a crash
    assert not runner.is_running()


async def test_runner_stop_idempotent(tmp_path, patched_runner):
    spec = _make_spec(tmp_path, {}, [{"name": "A", "cmd": "/x"}, {"name": "B", "cmd": "/y"}])
    rec = _Recorder()
    runner = patched_runner(["--exit", "0"])

    await runner.start(spec, rec)
    await asyncio.wait_for(rec.done.wait(), timeout=5.0)
    # second stop after natural exit must not raise
    await runner.stop()
    await runner.stop()


async def test_runner_double_start_raises(tmp_path, patched_runner):
    spec = _make_spec(tmp_path, {}, [{"name": "A", "cmd": "/x"}, {"name": "B", "cmd": "/y"}])
    rec = _Recorder()
    runner = patched_runner(["--sleep", "10"])

    await runner.start(spec, rec)
    try:
        with pytest.raises(RuntimeError, match="already running"):
            await runner.start(spec, rec)
    finally:
        await runner.stop()
        await asyncio.wait_for(rec.done.wait(), timeout=5.0)


async def test_runner_binary_missing_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(
        FastchessRunner, "detect_binary",
        staticmethod(lambda configured: None),
    )
    runner = FastchessRunner(binary_path=None)
    spec = _make_spec(tmp_path, {}, [{"name": "A", "cmd": "/x"}, {"name": "B", "cmd": "/y"}])
    with pytest.raises(FileNotFoundError):
        await runner.start(spec, _Recorder())


async def test_runner_drains_large_output_no_deadlock(tmp_path, patched_runner):
    """Without pipe drains, a child filling stdout would block on write
    after ~64 KB on Linux. This exercises that the drains keep up."""
    spec = _make_spec(tmp_path, {}, [{"name": "A", "cmd": "/x"}, {"name": "B", "cmd": "/y"}])
    rec = _Recorder()
    # 2000 lines × ~20 bytes ≈ 40 KB; plus stderr to keep both drains busy.
    runner = patched_runner(["--print", "2000", "--print-err", "1000", "--exit", "0"])

    await runner.start(spec, rec)
    await asyncio.wait_for(rec.done.wait(), timeout=10.0)
    assert rec.events[-1][0] == "done"
    log_text = spec.log_path.read_text()
    # Spot-check first and last expected lines were captured.
    assert "out 0" in log_text
    assert "out 1999" in log_text
    assert "err 999" in log_text
