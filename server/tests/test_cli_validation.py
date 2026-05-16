"""CLI flag validation must run before the single-instance lock is
acquired. Otherwise: bad-flag launches leave temporary lockfiles and
race with concurrent bad-flag launches (one sees the lock error
instead of the actual flag error)."""
from __future__ import annotations

import pytest


def test_bad_cert_combo_exits_before_lock_acquire(monkeypatch):
    """`--cert foo` (no --key) must exit with code 2 without touching
    the single-instance lock."""
    from sturddle_view import __main__ as m

    lock_called = False

    def fake_acquire(_path):
        nonlocal lock_called
        lock_called = True
        return True

    monkeypatch.setattr(m, "_acquire_lock", fake_acquire)
    monkeypatch.setattr("sys.argv", ["sturddle-view", "--cert", "/nonexistent.pem"])

    with pytest.raises(SystemExit) as exc_info:
        m.main()

    assert exc_info.value.code == 2
    assert not lock_called, "lock must not be acquired before flag validation"


def test_missing_cert_file_exits_before_lock_acquire(monkeypatch):
    """`--cert nonexistent.pem --key nonexistent.key` must exit with
    code 2 without touching the lock."""
    from sturddle_view import __main__ as m

    lock_called = False

    def fake_acquire(_path):
        nonlocal lock_called
        lock_called = True
        return True

    monkeypatch.setattr(m, "_acquire_lock", fake_acquire)
    monkeypatch.setattr(
        "sys.argv",
        ["sturddle-view", "--cert", "/nope.pem", "--key", "/nope.key"],
    )

    with pytest.raises(SystemExit) as exc_info:
        m.main()

    assert exc_info.value.code == 2
    assert not lock_called


def test_desktop_with_cert_exits_before_lock_acquire(monkeypatch):
    """`--desktop --cert ... --key ...` must exit with code 2 without
    touching the lock."""
    from sturddle_view import __main__ as m

    lock_called = False

    def fake_acquire(_path):
        nonlocal lock_called
        lock_called = True
        return True

    monkeypatch.setattr(m, "_acquire_lock", fake_acquire)
    monkeypatch.setattr(
        "sys.argv",
        ["sturddle-view", "--desktop", "--cert", "/c.pem", "--key", "/c.key"],
    )

    with pytest.raises(SystemExit) as exc_info:
        m.main()

    assert exc_info.value.code == 2
    assert not lock_called
