"""Single-instance lock: cross-process exclusion and crash-safe release."""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from sturddle_view._instance_lock import acquire


HOLDER = textwrap.dedent("""\
    import sys, time
    from pathlib import Path
    from sturddle_view._instance_lock import acquire
    path = Path(sys.argv[1])
    assert acquire(path), "holder could not acquire lock"
    sys.stdout.write("ready\\n")
    sys.stdout.flush()
    time.sleep(60)
""")


@pytest.fixture()
def lock_path(tmp_path: Path) -> Path:
    return tmp_path / "test.lock"


def test_acquire_succeeds(lock_path: Path) -> None:
    assert acquire(lock_path)


def test_second_acquire_fails(lock_path: Path, tmp_path: Path) -> None:
    # Spawn a subprocess that holds the lock, then try to acquire it here.
    script = tmp_path / "holder.py"
    script.write_text(HOLDER)
    proc = subprocess.Popen(
        [sys.executable, str(script), str(lock_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        out = proc.stdout.read(6)  # "ready\n" (or "ready\r" on Windows)
        assert out.rstrip(b"\r\n") == b"ready", f"holder did not start: {proc.stderr.read()}"
        assert not acquire(lock_path), "expected lock to be held by subprocess"
    finally:
        proc.kill()
        proc.wait()


def test_lock_released_after_crash(lock_path: Path, tmp_path: Path) -> None:
    script = tmp_path / "holder.py"
    script.write_text(HOLDER)
    proc = subprocess.Popen(
        [sys.executable, str(script), str(lock_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    proc.stdout.read(6)  # wait for "ready\n" / "ready\r" on Windows
    proc.kill()
    proc.wait()
    # After the holder dies the OS releases the lock; we must be able to acquire it.
    assert acquire(lock_path), "lock not released after subprocess death"
