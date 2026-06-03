"""Tests for the PyWebView JsApi bridge.

The bridge exposes a ``save_pgn`` method to the embedded page so the Save
PGN button can write to the local filesystem in desktop mode (where the
WebView2 ``a.download`` trick is a no-op).

These tests cover the bridge in isolation -- no real window, no real
dialog -- by stubbing the window's ``create_file_dialog``.
"""
from __future__ import annotations

import errno
import socket
from pathlib import Path

import pytest
import uvicorn

from sturddle_view._uvicorn_signal import make_signalling_server
from sturddle_view.desktop import JsApi, _port_in_use


class _FakeWindow:
    def __init__(self, dialog_result):
        self._dialog_result = dialog_result
        self.last_kwargs: dict | None = None
        self.raise_on_dialog: Exception | None = None

    def create_file_dialog(self, _kind, **kwargs):
        self.last_kwargs = kwargs
        if self.raise_on_dialog is not None:
            raise self.raise_on_dialog
        return self._dialog_result


PGN_TEXT = '[Event "Test"]\n[Site "?"]\n\n1. e4 e5 *\n'
DEFAULT_FILENAME = "sturddle-20260101-120000-Alice-vs-Bob.pgn"


def test_save_pgn_writes_file(tmp_path: Path) -> None:
    target = tmp_path / "out.pgn"
    api = JsApi()
    api.attach(_FakeWindow(str(target)))

    res = api.save_pgn(PGN_TEXT, DEFAULT_FILENAME)

    assert res == {"ok": True, "path": str(target)}
    assert target.read_text(encoding="utf-8") == PGN_TEXT


def test_save_pgn_handles_sequence_result(tmp_path: Path) -> None:
    # Some PyWebView backends return a tuple even from SAVE_DIALOG.
    target = tmp_path / "out.pgn"
    api = JsApi()
    api.attach(_FakeWindow((str(target),)))

    res = api.save_pgn(PGN_TEXT, DEFAULT_FILENAME)

    assert res["ok"] is True
    assert target.read_text(encoding="utf-8") == PGN_TEXT


def test_save_pgn_passes_default_filename(tmp_path: Path) -> None:
    target = tmp_path / "out.pgn"
    window = _FakeWindow(str(target))
    api = JsApi()
    api.attach(window)

    api.save_pgn(PGN_TEXT, DEFAULT_FILENAME)

    assert window.last_kwargs is not None
    assert window.last_kwargs["save_filename"] == DEFAULT_FILENAME


def test_save_pgn_cancelled_returns_cancelled(tmp_path: Path) -> None:
    api = JsApi()
    api.attach(_FakeWindow(None))

    res = api.save_pgn(PGN_TEXT, DEFAULT_FILENAME)

    assert res == {"ok": False, "cancelled": True}


def test_save_pgn_empty_tuple_treated_as_cancel() -> None:
    api = JsApi()
    api.attach(_FakeWindow(()))

    res = api.save_pgn(PGN_TEXT, DEFAULT_FILENAME)

    assert res == {"ok": False, "cancelled": True}


def test_save_pgn_dialog_exception_returns_error() -> None:
    api = JsApi()
    window = _FakeWindow(None)
    window.raise_on_dialog = RuntimeError("backend exploded")
    api.attach(window)

    res = api.save_pgn(PGN_TEXT, DEFAULT_FILENAME)

    assert res["ok"] is False
    assert "backend exploded" in res["error"]


def test_save_pgn_write_failure_returns_error(tmp_path: Path) -> None:
    # Path under a file (not a directory) -> write fails with OSError.
    not_a_dir = tmp_path / "blocker"
    not_a_dir.write_text("x")
    bad_path = not_a_dir / "nested" / "out.pgn"
    api = JsApi()
    api.attach(_FakeWindow(str(bad_path)))

    res = api.save_pgn(PGN_TEXT, DEFAULT_FILENAME)

    assert res["ok"] is False
    assert res["path"] == str(bad_path)
    assert res["error"]


def test_save_pgn_without_attached_window_returns_error() -> None:
    api = JsApi()

    res = api.save_pgn(PGN_TEXT, DEFAULT_FILENAME)

    assert res == {"ok": False, "error": "window not attached"}


def test_save_pgn_writes_utf8_bytes(tmp_path: Path) -> None:
    # Non-ASCII player names round-trip as UTF-8.
    target = tmp_path / "out.pgn"
    api = JsApi()
    api.attach(_FakeWindow(str(target)))
    pgn = '[White "Magnús"]\n[Black "Hikaru"]\n\n1. e4 *\n'

    res = api.save_pgn(pgn, DEFAULT_FILENAME)

    assert res["ok"] is True
    assert target.read_bytes() == pgn.encode("utf-8")


# --- startup signal: a bind failure must be captured, not swallowed -----


def _noop_app(scope, receive, send):  # minimal ASGI app, never run here
    raise AssertionError("app should not run in these tests")


@pytest.mark.asyncio
async def test_signal_captures_startup_failure(monkeypatch) -> None:
    # A failing startup() must set error + done, leave ready clear, and
    # re-raise -- so the launcher surfaces it instead of timing out.
    boom = OSError(errno.EADDRINUSE, "address already in use")

    async def _fail(self, sockets=None):
        raise boom

    monkeypatch.setattr(uvicorn.Server, "startup", _fail)
    server, signal = make_signalling_server(uvicorn.Config(_noop_app))

    with pytest.raises(OSError):
        await server.startup()

    assert signal.done.is_set()
    assert not signal.ready.is_set()
    assert signal.error is boom


@pytest.mark.asyncio
async def test_signal_marks_ready_on_success(monkeypatch) -> None:
    async def _ok(self, sockets=None):
        return None

    monkeypatch.setattr(uvicorn.Server, "startup", _ok)
    server, signal = make_signalling_server(uvicorn.Config(_noop_app))

    await server.startup()

    assert signal.ready.is_set()
    assert signal.done.is_set()
    assert signal.error is None


# --- port pre-flight: detect "in use" before launching uvicorn ----------


def test_port_in_use_true_when_bound() -> None:
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.bind(("127.0.0.1", 0))
    held.listen()
    port = held.getsockname()[1]
    try:
        assert _port_in_use("127.0.0.1", port) is True
    finally:
        held.close()


def test_port_in_use_false_when_free() -> None:
    # Grab a free port, release it, then check -- almost certainly still free.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert _port_in_use("127.0.0.1", port) is False


def test_port_in_use_maps_wildcard_host() -> None:
    # 0.0.0.0 is probed against the loopback bind host; a free port reads free.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert _port_in_use("0.0.0.0", port) is False
