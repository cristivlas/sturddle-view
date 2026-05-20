"""Test isolation: keep tests off the user's real ~/.config files."""
from __future__ import annotations

import contextlib
import socket
import stat
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
import pytest_asyncio


SNAPSHOT_UPDATE_FLAG = "--snapshot-update"
_UVICORN_STARTUP_TIMEOUT = 10.0
_UVICORN_SHUTDOWN_TIMEOUT = 10.0


def make_fake_uci(root: Path, name: str) -> str:
    """Write a minimal UCI stub engine to ``root`` and return its path.

    The stub responds to ``uci``/``isready``/``quit`` only; it does NOT
    play moves. Tests that need a move-playing fake should override
    locally."""
    py = root / f"{name}.py"
    py.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "while True:\n"
        "    line = sys.stdin.readline()\n"
        "    if not line: break\n"
        "    line = line.strip()\n"
        f"    if line == 'uci': sys.stdout.write('id name {name}\\nuciok\\n'); sys.stdout.flush()\n"
        "    elif line == 'isready': sys.stdout.write('readyok\\n'); sys.stdout.flush()\n"
        "    elif line == 'quit': break\n"
    )
    if sys.platform.startswith("win"):
        wrapper = root / f"{name}.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{py}" %*\r\n')
        return str(wrapper)
    py.chmod(py.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(py)


def free_port() -> int:
    """Allocate an ephemeral TCP port on localhost."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def run_uvicorn(app, *, port: int | None = None) -> Iterator[tuple[str, object]]:
    """Run a uvicorn server in a background thread.

    Startup waits on a ``threading.Event`` set by the server inside its
    own loop -- no sleep-polling. Teardown is best-effort: tests that
    leak connections at fixture teardown still rely on ``force_exit``
    plus the daemon thread to clean up at process exit. Tightening this
    requires hoisting Playwright contexts into fixtures across the e2e
    suite so teardown is synchronous; tracked separately."""
    import uvicorn

    from sturddle_view._uvicorn_signal import make_signalling_server

    if port is None:
        port = free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", ws="wsproto"
    )
    s, started = make_signalling_server(config)
    thread = threading.Thread(target=s.run, daemon=True)
    thread.start()
    if not started.wait(timeout=_UVICORN_STARTUP_TIMEOUT):
        s.should_exit = True
        thread.join(timeout=_UVICORN_SHUTDOWN_TIMEOUT)
        raise RuntimeError("uvicorn did not start within timeout")
    try:
        yield f"http://127.0.0.1:{port}", s
    finally:
        s.should_exit = True
        s.force_exit = True
        thread.join(timeout=_UVICORN_SHUTDOWN_TIMEOUT)


def pytest_addoption(parser):
    parser.addoption("--syzygy-path", default=None, help="Path to Syzygy tablebase files")
    parser.addoption(
        SNAPSHOT_UPDATE_FLAG,
        action="store_true",
        default=False,
        help="Overwrite committed snapshot fixtures with current output.",
    )


@pytest_asyncio.fixture(scope="session")
async def browser():
    """One Chromium instance shared across the whole test session.

    Use the ``page`` or ``make_page`` fixtures rather than calling
    ``browser.new_context()`` directly -- those guarantee synchronous
    context teardown so the next test starts clean.
    Yields None when Playwright/Chromium is not installed -- each e2e
    test calls pytest.skip() on None.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        yield None
        return
    async with async_playwright() as pw:
        try:
            b = await pw.chromium.launch()
        except Exception:
            yield None
            return
        yield b
        await b.close()


@pytest_asyncio.fixture
async def make_page(browser):
    """Factory yielding a (ctx, page) tuple at the requested viewport.

    Tracks every context it creates so teardown closes them all in
    reverse order before the next test begins -- prevents WS leaks from
    bleeding into the next test's fixtures."""
    if browser is None:
        pytest.skip("chromium not installed")
    contexts = []

    async def _make(viewport=None, **ctx_kwargs):
        kwargs = dict(ctx_kwargs)
        if viewport is not None:
            kwargs["viewport"] = viewport
        ctx = await browser.new_context(**kwargs)
        contexts.append(ctx)
        page = await ctx.new_page()
        return ctx, page

    try:
        yield _make
    finally:
        for ctx in reversed(contexts):
            try:
                await ctx.close()
            except Exception:
                pass


@pytest_asyncio.fixture
async def page(make_page):
    """Default Playwright page at the test's natural viewport (Playwright default)."""
    _ctx, p = await make_page()
    yield p


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path, monkeypatch):
    """Redirect default user-config paths to per-test tmp locations.

    Covers every default that would otherwise resolve to the developer's
    real platformdirs/~/.config tree: the saved-game snapshot, persisted
    settings, the engine registry, and the tournament store root. Without
    this, a test that constructs ``create_app(settings=...)`` without
    overriding ``tournament_root`` would mount the user's *live*
    tournament directory and the orchestrator's startup reconcile would
    flip any in-flight tournament to ``failed``.

    Tests that need to inspect a saved file should pass explicit paths
    (e.g. ``GameStore``, ``EngineRegistry(path=...)``, or
    ``Settings.tournament_root``) instead of relying on these defaults.
    """
    import sturddle_view.app as app_mod
    import sturddle_view.config as cfg
    import sturddle_view.engines as engines_mod
    import sturddle_view.play.game_store as gs
    import sturddle_view.recent_imports as ri_mod
    import sturddle_view.tournament.store as ts_mod
    monkeypatch.setattr(
        gs, "default_state_path", lambda: tmp_path / "current_game.json"
    )
    monkeypatch.setattr(
        cfg, "default_settings_file", lambda: tmp_path / "settings.json"
    )
    monkeypatch.setattr(
        engines_mod, "default_registry_path", lambda: tmp_path / "engines.json"
    )
    monkeypatch.setattr(
        ri_mod, "default_imports_dir", lambda: tmp_path / "imports"
    )
    # Patch both the canonical symbol and app.py's local import binding.
    fake_root = tmp_path / "tournaments"
    monkeypatch.setattr(ts_mod, "default_root", lambda: fake_root)
    monkeypatch.setattr(app_mod, "default_root", lambda: fake_root)
