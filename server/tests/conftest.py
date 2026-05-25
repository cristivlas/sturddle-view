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

# Per-test registry: base URL -> server log path. Populated by
# run_uvicorn_subprocess so the failure hook can dump server output.
_SERVER_LOGS: dict[str, Path] = {}


def _write_uci_stub(root: Path, name: str, extra_body: str = "", *, pre_loop: str = "") -> str:
    """Shared writer for Python-based fake UCI engines.

    `extra_body`: indented elif branches in the read loop.
    `pre_loop`: module-level statements before the read loop (e.g. state
    vars like `last_position = ''` for the position-aware fake).
    """
    py = root / f"{name}.py"
    body = (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"{pre_loop}"
        "while True:\n"
        "    line = sys.stdin.readline()\n"
        "    if not line: break\n"
        "    line = line.strip()\n"
        f"    if line == 'uci': sys.stdout.write('id name {name}\\nuciok\\n'); sys.stdout.flush()\n"
        "    elif line == 'isready': sys.stdout.write('readyok\\n'); sys.stdout.flush()\n"
        f"{extra_body}"
        "    elif line == 'quit': break\n"
    )
    py.write_text(body)
    if sys.platform.startswith("win"):
        wrapper = root / f"{name}.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{py}" %*\r\n')
        return str(wrapper)
    py.chmod(py.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(py)


def make_searching_fake_uci(
    root: Path,
    name: str,
    *,
    score_cp: int = 25,
    depth: int = 6,
    bestmove: str = "e2e4",
    pv: str = "e2e4 e7e5",
) -> str:
    """Fake UCI engine that responds to `go` with a canned info line +
    bestmove. Used by analysis-driving tests (e.g. the AI `analyze`
    tool) to exercise the real chess.engine loop without a heavy binary.

    Honors `stop` by emitting bestmove immediately (same canned line),
    so a cancel-mid-search test sees the engine wind down cleanly.
    """
    extra = (
        "    elif line.startswith('go') or line == 'stop':\n"
        f"        sys.stdout.write('info depth {depth} score cp {score_cp} nodes 1234 time 50 pv {pv}\\n')\n"
        f"        sys.stdout.write('bestmove {bestmove}\\n')\n"
        "        sys.stdout.flush()\n"
    )
    return _write_uci_stub(root, name, extra)


def make_position_aware_fake_uci(
    root: Path,
    name: str,
    *,
    score_by_substring: dict[str, int],
    default_score_cp: int = 0,
    depth: int = 6,
) -> str:
    """Fake UCI engine that varies `score cp` by the latest `position`
    line. `score_by_substring` keys are substrings matched against that
    line; first match wins, otherwise `default_score_cp`. No PV emitted."""
    import json
    table_json = json.dumps(score_by_substring)
    extra = (
        "    elif line.startswith('position'):\n"
        "        last_position = line\n"
        "    elif line.startswith('go') or line == 'stop':\n"
        f"        table = {table_json}\n"
        f"        cp = {default_score_cp}\n"
        "        for needle, val in table.items():\n"
        "            if needle in last_position:\n"
        "                cp = val\n"
        "                break\n"
        f"        sys.stdout.write(f'info depth {depth} score cp {{cp}} nodes 1234 time 50\\n')\n"
        "        sys.stdout.write('bestmove 0000\\n')\n"
        "        sys.stdout.flush()\n"
    )
    return _write_uci_stub(root, name, extra, pre_loop="last_position = ''\n")


def make_fake_uci(root: Path, name: str) -> str:
    """Minimal UCI stub: handles ``uci``/``isready``/``quit`` only and
    does NOT play moves. Tests that need a move-playing fake should
    use ``make_searching_fake_uci`` or override locally."""
    return _write_uci_stub(root, name)


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

    import tempfile as _tempfile
    log_path = Path(_tempfile.gettempdir()) / f"sturddle-test-uvicorn-{port}.log"
    base = f"http://127.0.0.1:{port}"
    log_fh = open(log_path, "wb")

    # Event-based startup handshake: listen on an ephemeral port and
    # pass it to the subprocess via SV_READY_PORT. The app's lifespan
    # startup connects to it after uvicorn has bound the listen socket,
    # giving us a deterministic ready signal with no polling.
    ready_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ready_sock.bind(("127.0.0.1", 0))
    ready_sock.listen(1)
    ready_sock.settimeout(_UVICORN_STARTUP_TIMEOUT)
    env["SV_READY_PORT"] = str(ready_sock.getsockname()[1])

    try:
        with subprocess.Popen(
            [sys.executable, "-m", "sturddle_view", "--no-auth",
             "--host", "127.0.0.1", "--port", str(port)],
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        ) as proc:
            _SERVER_LOGS[base] = log_path
            try:
                try:
                    conn, _addr = ready_sock.accept()
                    conn.close()
                except (socket.timeout, OSError) as e:
                    log_fh.flush()
                    try:
                        tail = log_path.read_bytes().decode(errors="replace")
                    except OSError:
                        tail = "(log unreadable)"
                    raise RuntimeError(
                        f"uvicorn subprocess startup signal not received "
                        f"within {_UVICORN_STARTUP_TIMEOUT}s "
                        f"(exit code={proc.poll()}; accept err={e})\n"
                        f"LOG ({log_path}):\n{tail}"
                    ) from e
                yield base
            finally:
                proc.kill()
                _SERVER_LOGS.pop(base, None)
    finally:
        try:
            ready_sock.close()
        except Exception:
            pass
        try:
            log_fh.close()
        except Exception:
            pass


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
    # Real-Ollama opt-in for AI analysis integration tests. Default off
    # so CI / normal runs skip the network. Model is required when the
    # flag is set; base URL defaults to the local daemon.
    parser.addoption(
        "--ollama",
        action="store_true",
        default=False,
        help="Run integration tests that talk to a real Ollama daemon.",
    )
    parser.addoption(
        "--ollama-model",
        default=None,
        help="Model name to use when --ollama is set (e.g. qwen2.5:3b).",
    )
    parser.addoption(
        "--ollama-base-url",
        default="http://localhost:11434",
        help="Base URL of the Ollama daemon when --ollama is set.",
    )


_E2E_DUMP_SCRIPT = """
() => {
  const pick = (sel) => Array.from(document.querySelectorAll(sel));
  const dump = {
    url: location.href,
    title: document.title,
    perspective: {
      root: !!document.querySelector('#perspective-root'),
      isPending: document.querySelector('#perspective-root')?.classList.contains('is-pending') ?? null,
      active: document.querySelector('[data-perspective].active')?.getAttribute('data-perspective') ?? null,
    },
    dockSlots: pick('.dock-slot').map(s => ({
      title: s.querySelector('.dock-slot-title')?.textContent,
      parent: s.parentElement?.className ?? null,
    })),
    winboxes: pick('.winbox').map(w => ({ cls: w.className, title: w.querySelector('.wb-title')?.textContent })),
    localStorage: Object.fromEntries(
      Object.keys(localStorage)
        .filter(k => k.startsWith('sturddle:'))
        .map(k => [k, localStorage.getItem(k)])
    ),
  };
  return dump;
}
"""


@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or not report.failed:
        return
    if not any(m.name == "e2e" for m in item.iter_markers()):
        return
    pages = getattr(item, _E2E_PAGES_ATTR, None) or []
    if not pages:
        return

    import asyncio
    import json

    sections: list[str] = []
    for idx, page in enumerate(pages):
        try:
            if page.is_closed():
                sections.append(f"[page {idx}] closed before forensics")
                continue
        except Exception as e:
            sections.append(f"[page {idx}] is_closed() failed: {e}")
            continue

        errors = getattr(page, _E2E_ERRORS_ATTR, []) or []
        sections.append(f"[page {idx}] url={page.url}")
        sections.append(f"  console/pageerror ({len(errors)}):")
        sections.extend(f"    {e}" for e in errors[-50:])

        try:
            loop = asyncio.get_event_loop()
            dump = loop.run_until_complete(page.evaluate(_E2E_DUMP_SCRIPT))
            sections.append("  snapshot: " + json.dumps(dump, indent=2)[:4000])
        except Exception as e:
            sections.append(f"  snapshot eval failed: {e}")

        try:
            shot_path = f"/tmp/sv-e2e-fail-{item.name}-p{idx}.png"
            if sys.platform.startswith("win"):
                import tempfile
                shot_path = str(Path(tempfile.gettempdir()) / f"sv-e2e-fail-{item.name}-p{idx}.png")
            loop = asyncio.get_event_loop()
            loop.run_until_complete(page.screenshot(path=shot_path, full_page=True))
            sections.append(f"  screenshot: {shot_path}")
        except Exception as e:
            sections.append(f"  screenshot failed: {e}")

    seen_logs: set[Path] = set()
    for page in pages:
        try:
            url = page.url
        except Exception:
            continue
        from urllib.parse import urlsplit
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        log_path = _SERVER_LOGS.get(base)
        if log_path is None or log_path in seen_logs:
            continue
        seen_logs.add(log_path)
        try:
            data = log_path.read_text(errors="replace")
        except OSError as e:
            sections.append(f"server log {log_path}: read failed: {e}")
            continue
        tail_lines = data.splitlines()[-200:]
        sections.append(f"server log {log_path} (last {len(tail_lines)} lines):")
        sections.extend(f"  {line}" for line in tail_lines)

    if sections:
        report.sections.append(("e2e forensics", "\n".join(sections)))


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


_E2E_PAGES_ATTR = "_sv_e2e_pages"
_E2E_ERRORS_ATTR = "_sv_console_errors"


def _attach_console_capture(page):
    """Sink console.error/warn and pageerror events into a list on the page.

    The on-failure hook reads this list to dump forensic info without
    requiring every test body to remember to print it. Idempotent.
    """
    if getattr(page, _E2E_ERRORS_ATTR, None) is not None:
        return getattr(page, _E2E_ERRORS_ATTR)
    errors: list[str] = []
    setattr(page, _E2E_ERRORS_ATTR, errors)
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.on(
        "console",
        lambda msg: errors.append(f"console.{msg.type}: {msg.text}")
        if msg.type in ("error", "warning") else None,
    )
    return errors


@pytest_asyncio.fixture
async def make_page(browser, request):
    """Factory yielding a (ctx, page) tuple at the requested viewport.

    Tracks every context it creates so teardown closes them all in
    reverse order before the next test begins -- prevents WS leaks from
    bleeding into the next test's fixtures. Also registers each page on
    the test node so the on-failure hook can dump forensics."""
    if browser is None:
        pytest.skip("chromium not installed")
    contexts = []
    pages: list = getattr(request.node, _E2E_PAGES_ATTR, None) or []
    setattr(request.node, _E2E_PAGES_ATTR, pages)

    async def _make(viewport=None, **ctx_kwargs):
        kwargs = dict(ctx_kwargs)
        if viewport is not None:
            kwargs["viewport"] = viewport
        ctx = await browser.new_context(**kwargs)
        contexts.append(ctx)
        page = await ctx.new_page()
        _attach_console_capture(page)
        pages.append(page)
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
    its controller resolves ``ready``; before that, ``#perspective-root``
    carries the ``is-pending`` class. Tests that click ribbon buttons
    immediately after page.goto MUST await this before clicking, or
    the click can land on an unbound element and silently do nothing."""
    await page.wait_for_function(
        "() => !document.querySelector('#perspective-root')?.classList.contains('is-pending')",
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
