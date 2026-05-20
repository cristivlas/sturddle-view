"""Test isolation: keep tests off the user's real ~/.config files."""
from __future__ import annotations

import contextlib
import os
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
def run_uvicorn_subprocess(
    *,
    port: int | None = None,
    env_overrides: dict[str, str] | None = None,
) -> Iterator[str]:
    """Run uvicorn as a subprocess so the OS owns its lifecycle.

    On teardown the process is killed -- WS/HTTP transports are reclaimed
    by the OS instantly, with no proactor-cleanup races. Yields the
    server's base URL.

    Per-test isolation flows through env vars (``SV_*``):
    ``SV_PGN_DIR``, ``SV_TOURNAMENT_ROOT``, ``SV_ENGINE_REGISTRY_PATH``,
    ``SV_IMPORTS_DIR``, ``SV_SETTINGS_FILE``, ``SV_GAME_STATE_PATH``,
    ``SV_TOKEN``, ``SV_AUTH_DISABLED``. Pass them in ``env_overrides``.
    """
    import subprocess

    if port is None:
        port = free_port()
    env = dict(os.environ)
    env.setdefault("SV_AUTH_DISABLED", "1")
    env.setdefault("SV_TOKEN", "test-token")
    env["SV_HOST"] = "127.0.0.1"
    env["SV_PORT"] = str(port)
    env["SV_TEST_MODE"] = "1"
    if env_overrides:
        env.update(env_overrides)
    # Each subprocess gets a private instance-lock so the user's real
    # lock file doesn't collide with the test run.
    if "SV_INSTANCE_LOCK_PATH" not in env:
        import tempfile
        env["SV_INSTANCE_LOCK_PATH"] = str(
            Path(tempfile.gettempdir()) / f"sturddle-test-{port}.lock"
        )

    with subprocess.Popen(
        [sys.executable, "-m", "sturddle_view", "--no-auth",
         "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as proc:
        base = f"http://127.0.0.1:{port}"
        try:
            # Wait for the server to bind by attempting a TCP connect.
            # If the process exits early we surface stdout/stderr.
            import socket as _socket
            while True:
                if proc.poll() is not None:
                    stdout, stderr = proc.communicate()
                    raise RuntimeError(
                        f"uvicorn subprocess exited with code {proc.returncode}\n"
                        f"STDOUT:\n{stdout.decode(errors='replace')}\n"
                        f"STDERR:\n{stderr.decode(errors='replace')}"
                    )
                with _socket.socket() as s:
                    try:
                        s.settimeout(0.1)
                        s.connect(("127.0.0.1", port))
                        break
                    except OSError:
                        continue
            yield base
        finally:
            proc.kill()


@contextlib.contextmanager
def run_uvicorn(app, *, port: int | None = None) -> Iterator[tuple[str, object]]:
    """Run a uvicorn server in a background thread.

    Startup waits on a ``threading.Event`` set by the server inside its
    own loop -- no sleep-polling. Teardown signals ``should_exit`` and
    lets uvicorn run its full lifespan shutdown (so app.state.hve
    cleanup, etc. actually runs) before the loop is closed. The
    ``page``/``make_page`` fixtures close Playwright contexts before
    this teardown runs, so WS connections drain cleanly."""
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
        thread.join(timeout=_UVICORN_SHUTDOWN_TIMEOUT)


def pytest_addoption(parser):
    parser.addoption("--syzygy-path", default=None, help="Path to Syzygy tablebase files")
    parser.addoption(
        SNAPSHOT_UPDATE_FLAG,
        action="store_true",
        default=False,
        help="Overwrite committed snapshot fixtures with current output.",
    )


@pytest_asyncio.fixture
async def browser():
    """Chromium instance scoped per-test.

    Per-test scope means the browser is killed between tests, which
    forcibly drops every TCP connection to uvicorn -- the OS sends
    RST on the leaked WS sockets and uvicorn's shutdown has nothing
    left to drain. Costs ~500ms per test for relaunch, but eliminates
    the cross-test ResourceWarning class.

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


async def wait_perspective_ready(page) -> None:
    """Wait until the active perspective has finished mounting.

    The play perspective's button click handlers are attached only after
    its controller resolves ``ready``; before that, ``.perspective-root``
    carries the ``is-pending`` class. Tests that click ribbon buttons
    immediately after page.goto MUST await this before clicking, or
    the click can land on an unbound element and silently do nothing."""
    await page.wait_for_function(
        "() => !document.querySelector('.perspective-root')?.classList.contains('is-pending')",
    )


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
