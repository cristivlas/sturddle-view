"""Test isolation: keep tests off the user's real ~/.config files."""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import itertools
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlparse, urlsplit

import chess
import pytest
import pytest_asyncio
import uvicorn
from fastapi.testclient import TestClient
import httpx
from httpx import AsyncClient

import sturddle_view.api.tournaments as tournaments_api
import sturddle_view.app as app_mod
import sturddle_view.config as cfg
import sturddle_view.engines as engines_mod
import sturddle_view.play.game_store as gs
import sturddle_view.play.playbook_catalog as pc_mod
import sturddle_view.recent_imports as ri_mod
import sturddle_view.tournament.store as ts_mod
from sturddle_view import key_store
from sturddle_view._uvicorn_signal import make_signalling_server
from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.engines import EngineRegistry
from sturddle_view.play.chess_clock import ChessClock, TimeControl
from sturddle_view.play.human_vs_engine import HumanVsEngine
from sturddle_view.tournament import fastchess as fc_mod
from sturddle_view.tournament.fastchess import FastchessRunner


SNAPSHOT_UPDATE_FLAG = "--snapshot-update"
SERVER_PER_TEST_FLAG = "--server-per-test"
# How often a blocking accept() wakes so Ctrl+C lands; a wake only retries.
_ACCEPT_WAKE_S = float(os.environ.get("SV_TEST_ACCEPT_WAKE_S", "1.0"))

# Per-test registry: base URL -> server log path. Populated by
# run_uvicorn_subprocess so the failure hook can dump server output.
_SERVER_LOGS: dict[str, Path] = {}


class _InMemoryKeyring:
    """Tests must never touch the real OS keyring -- they'd pollute the
    user's Credential Manager / Keychain. This shim implements the two
    methods key_store uses and lives only for the test process."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self._store.get((service, account))

    def set_password(self, service: str, account: str, password: str) -> None:
        self._store[(service, account)] = password

    def delete_password(self, service: str, account: str) -> None:
        self._store.pop((service, account), None)


@pytest.fixture(autouse=True)
def _isolate_keyring(monkeypatch):
    """Force every test to use an in-memory keyring -- never the OS one."""
    fake = _InMemoryKeyring()
    monkeypatch.setattr(key_store, "_keyring", lambda: fake)
    yield


def _write_uci_stub(
    root: Path, name: str, extra_body: str = "", *, pre_loop: str = "", uci_options: str = "",
) -> str:
    """Shared writer for Python-based fake UCI engines.

    `extra_body`: indented elif branches in the read loop.
    `pre_loop`: module-level statements before the read loop (e.g. state
    vars like `last_position = ''` for the position-aware fake).
    `uci_options`: `option name ...` lines announced before `uciok`, each
    ending in an escaped newline.
    """
    py = root / f"{name}.py"
    body = (
        # Use the test interpreter (which has `chess` and friends) rather
        # than `/usr/bin/env python3`: the latter can resolve to a bare
        # system Python lacking our deps. On Windows the .cmd wrapper below
        # invokes sys.executable directly, so this shebang only matters on
        # POSIX, where the stub is exec'd via its shebang.
        f"#!{sys.executable}\n"
        "import sys\n"
        f"{pre_loop}"
        "while True:\n"
        "    line = sys.stdin.readline()\n"
        "    if not line: break\n"
        "    line = line.strip()\n"
        f"    if line == 'uci': sys.stdout.write('id name {name}\\n{uci_options}uciok\\n')"
        "; sys.stdout.flush()\n"
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
    """Fake UCI engine that varies `score cp` by matching substrings
    against the latest `position` line concatenated with the current
    `go` line (so callers can key on FEN, played moves, OR `searchmoves`
    args in `go`). First match wins, otherwise `default_score_cp`."""
    table_json = json.dumps(score_by_substring)
    extra = (
        "    elif line.startswith('position'):\n"
        "        last_position = line\n"
        "    elif line.startswith('go') or line == 'stop':\n"
        "        ctx = last_position + ' ' + line\n"
        f"        table = {table_json}\n"
        f"        cp = {default_score_cp}\n"
        "        for needle, val in table.items():\n"
        "            if needle in ctx:\n"
        "                cp = val\n"
        "                break\n"
        f"        sys.stdout.write(f'info depth {depth} score cp {{cp}} nodes 1234 time 50\\n')\n"
        "        sys.stdout.write('bestmove 0000\\n')\n"
        "        sys.stdout.flush()\n"
    )
    return _write_uci_stub(root, name, extra, pre_loop="last_position = ''\n")


def make_long_search_fake_uci(
    root: Path,
    name: str,
    *,
    bestmove: str = "e2e4",
    pv: str = "e2e4",
    score_cp: int = 25,
) -> str:
    """Fake UCI that emits an info line on `go` and then sits idle until
    `stop` (emits bestmove) or `quit` (exits). The outer read loop in
    _write_uci_stub already handles waiting for the next command, so
    after the info line we just fall back to it.

    Lets a test deterministically observe "search in flight" and exercise
    mid-search cancel via the engine's stop path."""
    extra = (
        "    elif line.startswith('go'):\n"
        f"        sys.stdout.write('info depth 1 score cp {score_cp} nodes 100 time 10 pv {pv}\\n')\n"
        "        sys.stdout.flush()\n"
        "    elif line == 'stop':\n"
        f"        sys.stdout.write('bestmove {bestmove}\\n')\n"
        "        sys.stdout.flush()\n"
    )
    return _write_uci_stub(root, name, extra)


def make_fake_uci(root: Path, name: str) -> str:
    """Minimal UCI stub: handles ``uci``/``isready``/``quit`` only and
    does NOT play moves. Tests that need a move-playing fake should
    use ``make_searching_fake_uci`` or override locally."""
    return _write_uci_stub(root, name)


class PageObserver:
    """Tracks HTTP request lifecycles and WS board_update frames for a
    Playwright page. All observation happens on the Playwright (CDP) side
    -- no JS injection, so nothing to clean up in the page itself.

    Helpers (all timeout-free):
      * ``wait_quiet(substr)`` -- await zero in-flight requests whose
        URL contains ``substr``. Pre-registers the substring so a later
        request can be counted.
      * ``wait_board_update(predicate)`` -- await the next board_update
        whose payload satisfies ``predicate(payload)``. Re-checks the
        last observed payload first, so a state already reached resolves
        immediately.
    """

    # Eager forensic log: every event is written and flushed immediately
    # so a Ctrl+C of a hanging test still leaves us a usable trace at
    # %TEMP%/sturddle-test-pageobs-<pid>-<ts>.log. The path is also
    # printed once at construction so the user can find it.
    def __init__(self, page):
        self._page = page
        self._inflight: dict[str, int] = {}
        self._quiet_waiters: list[tuple[str, asyncio.Future]] = []
        self._last_board: dict | None = None
        self._board_waiters: list[tuple[callable, asyncio.Future]] = []
        log_path = Path(tempfile.gettempdir()) / (
            f"sturddle-test-pageobs-{Path(sys.argv[0]).stem}-"
            f"{int(time.time() * 1000)}.log"
        )
        self._log_fh = open(log_path, "w", encoding="utf-8")
        self.log_path = log_path
        print(f"[PageObserver] eager log: {log_path}", flush=True)
        self._log("init")
        page.on("request", self._on_request)
        page.on("requestfinished", self._on_finished)
        page.on("requestfailed", self._on_finished)
        page.on("websocket", self._on_websocket)

    def _log(self, event: str, **fields) -> None:
        ts = time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
        parts = [ts, event]
        for k, v in fields.items():
            parts.append(f"{k}={v}")
        self._log_fh.write(" ".join(parts) + "\n")
        self._log_fh.flush()

    def _on_request(self, req):
        for key in self._inflight:
            if key in req.url:
                self._inflight[key] += 1
                self._log("req-start", key=key, method=req.method, url=req.url)

    def _on_finished(self, req):
        for key in list(self._inflight):
            if key in req.url:
                self._inflight[key] = max(0, self._inflight[key] - 1)
                self._log("req-end", key=key, method=req.method, url=req.url,
                          inflight=self._inflight[key])
                if self._inflight[key] == 0:
                    still = []
                    for k, f in self._quiet_waiters:
                        if k == key and not f.done():
                            f.set_result(None)
                        else:
                            still.append((k, f))
                    self._quiet_waiters = still

    def _on_websocket(self, ws):
        self._log("ws-open", url=ws.url)
        ws.on("framereceived", self._on_frame)
        ws.on("close", lambda: self._log("ws-close", url=ws.url))

    def _on_frame(self, frame):
        try:
            ev = json.loads(frame)
        except Exception:
            self._log("frame-unparseable", n_bytes=len(frame) if frame else 0)
            return
        kind = ev.get("kind")
        if kind != "board_update":
            self._log("frame-other", kind=kind)
            return
        payload = ev.get("payload") or {}
        self._last_board = payload
        view = payload.get("view") or {}
        self._log(
            "board_update",
            moves=len(payload.get("moves_san") or []),
            editing=payload.get("editing"),
            viewing=bool(view),
            cursor=view.get("cursor"),
            n_waiters=len(self._board_waiters),
        )
        still = []
        for pred, fut in self._board_waiters:
            if not fut.done() and pred(payload):
                fut.set_result(payload)
                self._log("predicate-matched", pred=pred.__name__)
            else:
                still.append((pred, fut))
        self._board_waiters = still

    def __del__(self):
        try:
            self._log_fh.close()
        except Exception:
            pass

    def track(self, substr: str) -> None:
        self._inflight.setdefault(substr, 0)

    async def wait_quiet(self, substr: str) -> None:
        self.track(substr)
        if self._inflight[substr] == 0:
            self._log("wait_quiet-immediate", key=substr)
            return
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._quiet_waiters.append((substr, fut))
        self._log("wait_quiet-park", key=substr, inflight=self._inflight[substr])
        await fut
        self._log("wait_quiet-resume", key=substr)

    async def wait_board_update(self, predicate):
        """Resolve to the first board_update payload matching predicate.

        Checks the last observed payload first so callers don't race."""
        if self._last_board is not None and predicate(self._last_board):
            self._log("wait_board_update-immediate", pred=predicate.__name__)
            return self._last_board
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._board_waiters.append((predicate, fut))
        self._log("wait_board_update-park", pred=predicate.__name__,
                  has_last=self._last_board is not None)
        await fut
        self._log("wait_board_update-resume", pred=predicate.__name__)
        return fut.result()


_POST = "POST"


def posted(path: str):
    """``page.expect_response`` predicate: the POST to exactly ``path``."""
    def pred(resp) -> bool:
        return urlparse(resp.url).path == path and resp.request.method == _POST
    return pred


# Fork-link lookup by game id (GET /game/recent-imports/by-id/{id}).
XGAME_BY_ID_PATH = "/game/recent-imports/by-id/"
XGAME_BY_ID_ROUTE = f"**{XGAME_BY_ID_PATH}**"
# Mirrors APP_EVT.XGAME_INFO_APPLIED in web/app/app-events.js.
XGAME_APPLIED_EVT = "sturddle:xgame-info-applied"


async def count_xgame_applies(page) -> None:
    """Count XGAME_INFO_APPLIED on a window slot from document start, so the
    listener exists before any page script runs (a listener added after goto
    would miss the initial mount's fetch)."""
    await page.add_init_script(f"""
        window.__xgameApplied = 0;
        window.addEventListener('{XGAME_APPLIED_EVT}', () => {{ window.__xgameApplied += 1; }});
    """)


async def await_xgame_apply(page) -> None:
    """Block until the x-game fetch continuation (json -> cached board_update
    apply -> arrow restore) has run once since the last reset."""
    await page.wait_for_function("() => window.__xgameApplied >= 1")


async def reset_xgame_apply_count(page) -> None:
    await page.evaluate("() => { window.__xgameApplied = 0; }")


BOARD_SVG = ".game-view-board svg.cm-chessboard"
PIECE_ON = BOARD_SVG + ' [data-piece][data-square="{}"]'
_DRAG_STEPS = 8


async def drag_piece_one_rank_up(page, square: str) -> None:
    """Drag the piece on ``square`` one rank toward Black (White at bottom)."""
    box = await page.locator(PIECE_ON.format(square)).bounding_box()
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    await page.mouse.move(x, y)
    await page.mouse.down()
    await page.mouse.move(x, y - box["height"], steps=_DRAG_STEPS)
    await page.mouse.up()


# cm-chessboard resizes, redrawing every piece node, in a task deferred from
# its ResizeObserver: until then its container's width lags its host's.
_BOARD_SETTLED_JS = (
    "(sel) => { const c = document.querySelector(sel)?.parentElement;"
    " return !!c && c.style.width === `${c.parentElement.clientWidth}px`; }"
)


async def wait_board_settled(page) -> None:
    """Block until the board has redrawn at its host's current width, so a
    piece located next isn't replaced by a pending resize redraw. Call once
    the app has applied the layout change (e.g. a dock opening)."""
    await page.wait_for_function(_BOARD_SETTLED_JS, arg=BOARD_SVG)


def free_port() -> int:
    """Allocate an ephemeral TCP port on localhost."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Registry file under tmp_path; tests seed it before the server starts.
REGISTRY_FILE = "engines.json"
# Host header fastapi.testclient.TestClient sends by default.
TEST_CLIENT_HOST = "testserver"
STARTPOS_FEN = chess.STARTING_FEN


def e2e_env(tmp_path: Path) -> dict[str, str]:
    """Per-test ``SV_*`` overrides rooting every server store under
    ``tmp_path``. Seed the registry at ``tmp_path / REGISTRY_FILE``."""
    return {
        "SV_PGN_DIR": str(tmp_path / "pgn"),
        "SV_TOURNAMENT_ROOT": str(tmp_path / "tournaments"),
        "SV_ENGINE_REGISTRY_PATH": str(tmp_path / REGISTRY_FILE),
        "SV_IMPORTS_DIR": str(tmp_path / "imports"),
        "SV_SETTINGS_FILE": str(tmp_path / "settings.json"),
        "SV_GAME_STATE_PATH": str(tmp_path / "current_game.json"),
        "SV_AI_PLAYBOOK_DIR": str(tmp_path / "playbook"),
        "SV_WALLPAPER_DIR": str(tmp_path / "wallpaper"),
        "SV_THEMES_DIR": str(tmp_path / "themes"),
        # Also set by _isolate_user_config, but a per-file server starts
        # before that per-test fixture patches the environment.
        "SV_ENGINE_TMP_ROOT": str(tmp_path / "engine-tmp"),
    }


def _await_ready(sock: socket.socket, proc: subprocess.Popen) -> bool:
    """True once the server connects to `sock`, False if `proc` exits first.
    Wakes every _ACCEPT_WAKE_S so Ctrl+C lands (Windows ignores it in a
    blocking accept); a wake only re-checks, never fails."""
    sock.settimeout(_ACCEPT_WAKE_S)
    while proc.poll() is None:
        try:
            conn, _addr = sock.accept()
        except TimeoutError:
            continue
        conn.close()
        return True
    return False


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
        env["SV_INSTANCE_LOCK_PATH"] = str(
            Path(tempfile.gettempdir()) / f"sturddle-test-{port}.lock"
        )

    log_path = Path(tempfile.gettempdir()) / f"sturddle-test-uvicorn-{port}.log"
    base = f"http://127.0.0.1:{port}"
    log_fh = open(log_path, "wb")

    # Event-based startup handshake: listen on an ephemeral port and
    # pass it to the subprocess via SV_READY_PORT. Its ReadyPortServer
    # connects to it once uvicorn has bound the listen socket, giving us a
    # deterministic ready signal with no polling.
    ready_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ready_sock.bind(("127.0.0.1", 0))
    ready_sock.listen(1)
    env["SV_READY_PORT"] = str(ready_sock.getsockname()[1])

    try:
        # Auth comes from SV_AUTH_DISABLED (on by default above), so a test
        # can turn it back on with env_overrides.
        with subprocess.Popen(
            [sys.executable, "-m", "sturddle_view",
             "--host", "127.0.0.1", "--port", str(port)],
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        ) as proc:
            _SERVER_LOGS[base] = log_path
            try:
                # The ready signal or the server's exit, never a deadline.
                if not _await_ready(ready_sock, proc):
                    log_fh.flush()
                    try:
                        tail = log_path.read_bytes().decode(errors="replace")
                    except OSError:
                        tail = "(log unreadable)"
                    raise RuntimeError(
                        f"uvicorn subprocess exited before its startup signal "
                        f"(exit code={proc.returncode})\nLOG ({log_path}):\n{tail}"
                    )
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


# Display + script name of the engine serve_fake_engine registers.
FAKE_ENGINE = "FakeEngine"
# Canned search answering 1. e4 with 1...e5: serve_fake_engine(**E5_REPLY).
E5_REPLY = {"bestmove": "e7e5", "pv": "e7e5"}
# A registered engine that never launches: the game endpoints need one
# configured, and flows that never search (view, edit, replay) never spawn it.
UNLAUNCHED_ENGINE = "MyEngine"
UNLAUNCHED_ENGINE_PATH = "/nonexistent/engine"


def make_replying_fake_uci(root: Path, name: str, bestmove: str = E5_REPLY["bestmove"]) -> str:
    """UCI stub that answers every ``go`` at once with ``bestmove``, no
    info lines."""
    return _write_uci_stub(
        root, name,
        "    elif line.startswith('go'):\n"
        f"        sys.stdout.write('bestmove {bestmove}\\n'); sys.stdout.flush()\n",
    )


@contextlib.contextmanager
def serve_with_engine(tmp_path: Path, name: str, path: str) -> Iterator[str]:
    """Run the app (subprocess) with one engine registered and selected.
    Yields the base URL."""
    seed = EngineRegistry(path=tmp_path / REGISTRY_FILE)
    seed.select(seed.add(name=name, path=path).id)
    with run_uvicorn_subprocess(env_overrides=e2e_env(tmp_path)) as base:
        yield base


def serve_unlaunched_engine(
    tmp_path: Path, name: str = UNLAUNCHED_ENGINE,
) -> contextlib.AbstractContextManager[str]:
    """serve_with_engine for an engine that never launches."""
    return serve_with_engine(tmp_path, name, UNLAUNCHED_ENGINE_PATH)


def serve_fake_engine(tmp_path: Path, **search) -> contextlib.AbstractContextManager[str]:
    """serve_with_engine for the searching fake engine; `search` overrides
    its canned line (make_searching_fake_uci)."""
    return serve_with_engine(
        tmp_path, FAKE_ENGINE, make_searching_fake_uci(tmp_path, FAKE_ENGINE, **search),
    )


_START_AI_ANALYSIS_PATH = "/_test/hve/start_ai_analysis"


def start_ai_analysis(base: str) -> None:
    """Enter an AI analysis session with no turn kicked, for seed_replay to
    stand in for one: AI on, then the test hook (POST /game/analysis/start
    would kick a real turn)."""
    httpx.put(f"{base}/settings", json={cfg.AI_ENABLED_KEY: True}).raise_for_status()
    httpx.post(base + _START_AI_ANALYSIS_PATH).raise_for_status()


@contextlib.asynccontextmanager
async def run_uvicorn(app, *, port: int | None = None) -> AsyncIterator[tuple[str, object]]:
    """Run a uvicorn server in a background thread.

    Startup waits on a ``threading.Event`` set by the server inside its
    own loop -- no sleep-polling. Teardown signals ``should_exit`` and
    lets uvicorn run its full lifespan shutdown (so app.state.hve
    cleanup, etc. actually runs) before the loop is closed. Both waits
    run off the caller's loop: the shutdown can need it (work the test
    started there), so blocking it would deadlock."""
    if port is None:
        port = free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", ws="wsproto"
    )
    s, signal = make_signalling_server(config)

    # done also fires if run() ends before startup (a config error), so the
    # wait below has an event either way and never a deadline.
    def run():
        try:
            s.run()
        finally:
            signal.done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    await asyncio.to_thread(signal.done.wait)
    if not signal.ready.is_set():
        await asyncio.to_thread(thread.join)
        raise RuntimeError("uvicorn did not start") from signal.error
    try:
        yield f"http://127.0.0.1:{port}", s
    finally:
        s.should_exit = True
        # PROBE (temporary): dump the server loop's state while shutdown stalls.
        loop = s.servers[0].get_loop()
        join = asyncio.ensure_future(asyncio.to_thread(thread.join))
        while not (await asyncio.wait({join}, timeout=_PROBE_DUMP_S))[0]:
            _probe_dump(s, loop, port)
        await join


_PROBE_DUMP_S = float(os.environ.get("SV_TEST_PROBE_DUMP_S", "30"))


def _probe_dump(s, loop, port):
    path = Path(tempfile.gettempdir()) / f"sturddle-test-shutdown-{port}.log"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"=== {time.strftime('%H:%M:%S')} stalled shutdown, port {port}\n")
        for c in list(s.server_state.connections):
            w = getattr(c, "writable", None)
            fh.write(f"conn {type(c).__name__} writable={w.is_set() if w else None} "
                     f"closing={c.transport.is_closing()}\n")
        fh.write(f"tasks={len(s.server_state.tasks)}\n")
        for t in list(asyncio.all_tasks(loop)):
            t.print_stack(file=fh)
    print(f"PROBE: stalled uvicorn shutdown dumped to {path}", file=sys.stderr, flush=True)


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
    parser.addoption(
        SERVER_PER_TEST_FLAG,
        action="store_true",
        default=False,
        help="Start a fresh server for each e2e test instead of one per file "
             "(isolation when troubleshooting).",
    )


def server_scope(fixture_name, config) -> str:
    """Scope for an e2e file's `server` fixture: one server per file, or per
    test under --server-per-test. Only for files whose tests leave the
    server's state as they found it; such a fixture takes
    tmp_path_factory, not the per-test tmp_path."""
    return "function" if config.getoption(SERVER_PER_TEST_FLAG) else "module"


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

    # Forensics must run on the SAME loop the page was driven on (the page
    # object is loop-bound). asyncio.run() would spin up a fresh loop and
    # every page call would fail. Recover the existing loop; if none is
    # available the page is unusable anyway.
    try:
        loop = asyncio.get_event_loop_policy().get_event_loop()
    except Exception:
        loop = None

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
        requests = getattr(page, _E2E_REQUESTS_ATTR, None)
        if requests is not None:
            sections.extend(requests.report())

        try:
            dump = loop.run_until_complete(page.evaluate(_E2E_DUMP_SCRIPT))
            sections.append("  snapshot: " + json.dumps(dump, indent=2)[:4000])
        except Exception as e:
            sections.append(f"  snapshot eval failed: {e}")

        try:
            shot_path = f"/tmp/sv-e2e-fail-{item.name}-p{idx}.png"
            if sys.platform.startswith("win"):
                shot_path = str(Path(tempfile.gettempdir()) / f"sv-e2e-fail-{item.name}-p{idx}.png")
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
_E2E_REQUESTS_ATTR = "_sv_requests"
# Instant dock changes: a view-transition glide would show tests mid-move.
_E2E_REDUCED_MOTION = "reduce"


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
    _attach_request_tracker(page)
    return errors


@dataclasses.dataclass
class _RequestLog:
    """A page's requests still in flight (with their start time) and how
    many finished, so a stalled page shows what it was waiting on."""
    in_flight: dict = dataclasses.field(default_factory=dict)
    finished: int = 0

    def start(self, req) -> None:
        self.in_flight[req] = time.monotonic()

    def end(self, req) -> None:
        if self.in_flight.pop(req, None) is not None:
            self.finished += 1

    def report(self) -> list[str]:
        now = time.monotonic()
        lines = [f"  requests: {self.finished} finished, {len(self.in_flight)} in flight"]
        lines.extend(
            f"    {now - t:6.1f}s {req.method} {req.url}"
            for req, t in sorted(self.in_flight.items(), key=lambda kv: kv[1])
        )
        return lines


def _attach_request_tracker(page) -> None:
    log = _RequestLog()
    setattr(page, _E2E_REQUESTS_ATTR, log)
    page.on("request", log.start)
    page.on("requestfinished", log.end)
    page.on("requestfailed", log.end)


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
        kwargs.setdefault("reduced_motion", _E2E_REDUCED_MOTION)
        if viewport is not None:
            kwargs["viewport"] = viewport
        ctx = await browser.new_context(**kwargs)
        contexts.append(ctx)
        page = await ctx.new_page()
        _attach_console_capture(page)
        pages.append(page)
        return ctx, page

    # Callable early by a server the test owns (running_tournament): its
    # teardown must follow the pages' so their sockets drain first.
    async def close_all():
        for ctx in reversed(contexts):
            try:
                await ctx.close()
            except Exception:
                pass
        contexts.clear()

    _make.close_all = close_all
    try:
        yield _make
    finally:
        await close_all()


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
    the click can land on an unbound element and silently do nothing.
    An empty root means main.js hasn't mounted anything yet: is-pending is
    absent then too."""
    await page.wait_for_function(
        """() => {
            const root = document.querySelector('#perspective-root');
            return root?.childElementCount > 0 && !root.classList.contains('is-pending');
        }""",
    )


# Resolves after two frames, so ResizeObserver work scheduled by the last
# layout has run. Frame ordering, not a timer.
NEXT_FRAME_JS = "() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))"


# Monotonic suffix for wait_for_async_predicate's window slots -- one
# page can host several concurrent/sequential waits without collisions.
_ASYNC_WAIT_SEQ = itertools.count()


async def wait_for_async_predicate(page, body: str, *, poll_ms: int = 100) -> None:
    """``wait_for_function`` for ASYNC JS predicates.

    Playwright never awaits an async predicate: the returned Promise is
    truthy, so a plain ``wait_for_function("async () => ...")`` passes
    instantly, whatever the condition (a latent no-op in several older
    tests). Instead, run the predicate on a page-side interval, mirror
    its first truthy result onto a window slot, and sync-poll the slot.
    A throwing predicate simply retries next tick; if it never turns
    truthy, the sync wait times out with Playwright's usual error."""
    key = f"__sv_async_wait_{next(_ASYNC_WAIT_SEQ)}"
    await page.evaluate(
        f"""() => {{
            const pred = {body};
            const timer = setInterval(async () => {{
                try {{
                    if (await pred()) {{
                        window["{key}"] = true;
                        clearInterval(timer);
                    }}
                }} catch {{}}
            }}, {poll_ms});
        }}"""
    )
    await page.wait_for_function(f"() => window['{key}'] === true")


async def _wait_json(page, js: str, value, arg, op: str) -> None:
    await page.wait_for_function(
        f"([arg, v]) => JSON.stringify(({js})(arg)) {op} JSON.stringify(v)",
        arg=[arg, value],
    )


async def wait_eval(page, js: str, expected, arg=None) -> None:
    """Wait until the sync JS function ``js(arg)`` JSON-equals ``expected``.
    Use after an action instead of evaluate-then-assert, so the read sees
    the state the action's handlers settle on."""
    await _wait_json(page, js, expected, arg, "===")


async def wait_eval_not(page, js: str, unexpected, arg=None) -> None:
    """``wait_eval``'s converse: wait until ``js(arg)`` differs from ``unexpected``."""
    await _wait_json(page, js, unexpected, arg, "!==")


def watch_page_errors(page) -> list[str]:
    """Collect page JS exceptions + console errors; assert empty at test end."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(f"console.{msg.type}: {msg.text}")
            if msg.type == "error" else None)
    return errors


# A missing optional resource (404) is not a JS error.
BENIGN_CONSOLE_ERRORS = ("Failed to load resource",)


def assert_no_page_errors(errors: list[str]) -> None:
    """Fail on any watched page error that isn't benign."""
    real = [e for e in errors if not any(b in e for b in BENIGN_CONSOLE_ERRORS)]
    assert real == [], "JS errors:\n" + "\n".join(real)


ACTIVE_GAME_ID = "test-game"


def install_active_game(
    app, *, engine_path: str, human_white: bool = True,
    moves_uci: list[str] | None = None,
) -> HumanVsEngine:
    """Plant a fully-initialized HVE on app.state without a real UCI subprocess."""
    hve = HumanVsEngine(
        engine_path=engine_path,
        bus=app.state.event_bus,
        openings=getattr(app.state, "openings", None),
        settings=app.state.settings,
    )
    hve._engine_to_move = AsyncMock()
    hve._board = chess.Board()
    for uci in moves_uci or []:
        hve._board.push_uci(uci)
    hve._human_white = human_white
    hve._clock = ChessClock(TimeControl(60.0, 0.0))
    hve._clock.white_time = 60.0
    hve._clock.black_time = 60.0
    hve._game_id = ACTIVE_GAME_ID
    hve._clock.start_turn()
    app.state.hve = hve
    return hve


@pytest.fixture
def game_api_client(tmp_path):
    # _resolve_engine reads settings.engine_path (a Path) when no registry
    # entry is selected. The path does not need to exist -- _get_hve only
    # passes it as a string into HumanVsEngine.engine_path comparison.
    fake_engine = tmp_path / "engine"
    fake_engine.write_text("")
    settings = Settings(token="t", auth_disabled=True)
    settings.engine_path = fake_engine
    registry = EngineRegistry(path=tmp_path / REGISTRY_FILE)
    app = create_app(settings=settings, engine_registry=registry)
    with TestClient(app) as c:
        yield c, app, str(fake_engine)


# localStorage keys + values that pin a Tournaments UX shell; Studio is the
# default, but seeding it explicitly keeps tests honest if the default flips.
# Arena-era e2e tests must pin Arena, whose DOM they drive.
TOURNAMENT_UX_KEY = "sturddle:tournament:ux"
TOURNAMENT_UX_ARENA = "arena"
TOURNAMENT_UX_STUDIO = "studio"


async def pin_arena_tournament_ux(page) -> None:
    """Seed localStorage so the Arena Tournaments UX mounts. Must be called
    before the page.goto that loads the app, as add_init_script runs on each
    document load."""
    await page.add_init_script(
        f"localStorage.setItem('{TOURNAMENT_UX_KEY}', '{TOURNAMENT_UX_ARENA}')"
    )


async def wait_winbox_settled(page, sel: str, tol_px: float = 2) -> None:
    """WinBox animates maximize/restore: wait until the window's rendered
    width reaches its target (the inline style width)."""
    await page.wait_for_function(
        "([sel, tol]) => { const el = document.querySelector(sel);"
        " return Math.abs(el.getBoundingClientRect().width - parseFloat(el.style.width)) <= tol; }",
        arg=[sel, tol_px],
    )


async def goto_arena_tournaments(page, base: str) -> None:
    """Mount the Arena UX on the engines perspective, tournament list shown."""
    await pin_arena_tournament_ux(page)
    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)
    await page.click('button[data-perspective="engines"]')
    await page.wait_for_selector(".tournament-row")


# Class on a checked menu item (tournaments.js MENU_CHECK_CLASS).
MENU_CHECKED = "tmb-active"
# A fresh workspace opens in Clean (tidy): all four system windows.
CLEAN_WINDOWS = 4
_CLEAN_SUBPIXEL = 0.01
# Clean applied and every window settled: WinBox animates moves/resizes, so
# each window's rendered box must have reached its inline (target) geometry.
CLEAN_SETTLED = f"""() => {{
  const wins = [...document.querySelectorAll('.winbox.sturddle-wb-arena.no-shadow')];
  const near = (a, b) => Math.abs(a - b) < {_CLEAN_SUBPIXEL};
  return wins.length === {CLEAN_WINDOWS} && wins.every((w) => {{
    const r = w.getBoundingClientRect();
    return near(r.top, parseFloat(w.style.top)) && near(r.height, parseFloat(w.style.height));
  }});
}}"""


async def open_arena_workspace(page) -> None:
    """Open the selected tournament's fresh workspace (enables the Window
    menu) and wait for its default Clean layout to settle."""
    await page.click('.tournaments-ribbon .t-workspace')
    await page.wait_for_function(CLEAN_SETTLED)


# Minimal fake fastchess: stays alive so the tournament stays "running".
# Runs until running_tournament's stop() kills it. No timed exit: that would
# end the tournament (closing every watcher) mid-test on a slow machine.
_FAKE_FASTCHESS = "import threading; threading.Event().wait()"
LIVE_ENGINE_A = "Engine A"
LIVE_ENGINE_B = "Engine B"
# The proxy running_tournament registers (a LIVE_ENGINE_A process).
LIVE_PROXY_ID = "proxy-white"
PROXY_POST_PATH = "/internal/proxy"


def on_server_loop(app, coro):
    """Await `coro` on running_tournament's server loop: the orchestrator's
    asyncio state belongs to that loop, and a wait on another never wakes."""
    return asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, app.state.e2e_server_loop))


@contextlib.asynccontextmanager
async def running_tournament(tmp_path, monkeypatch, make_page):
    """Serve the app with a running tournament (fake fastchess) and one
    registered proxy, LIVE_PROXY_ID. Yields (app, base, tournament, proxy
    secret). A clean exit closes make_page's pages before the server stops,
    so their sockets drain while its loop still runs."""
    monkeypatch.setattr(
        FastchessRunner, "detect_binary",
        staticmethod(lambda configured: configured),
    )
    monkeypatch.setattr(
        fc_mod, "build_command",
        lambda spec: [sys.executable, "-c", _FAKE_FASTCHESS],
    )

    port = free_port()
    s = Settings(token="test", auth_disabled=True, port=port)
    s.tournament_root = str(tmp_path / "tournaments")
    s.tournament_fastchess_path = sys.executable
    registry = EngineRegistry(path=tmp_path / REGISTRY_FILE)
    engines = (LIVE_ENGINE_A, LIVE_ENGINE_B)
    paths = {name: make_fake_uci(tmp_path, name) for name in engines}
    for name, path in paths.items():
        registry.add(name=name, path=path)
    app = create_app(settings=s, engine_registry=registry)

    async with run_uvicorn(app, port=port) as (base, server):
        app.state.e2e_server_loop = server.servers[0].get_loop()
        store = app.state.tournament_store
        t = store.create(
            name="live-e2e",
            template={"tc": "5+0.1", "rounds": 1, "games_in_parallel": 1},
            engines=[{"name": name, "cmd": path} for name, path in paths.items()],
        )
        await on_server_loop(app, app.state.tournament_orch.start(t.id))
        try:
            # Posting over HTTP runs proxy_session_started in uvicorn's event
            # loop (not the test's), so the WS subscriber queue is correctly
            # populated for later lines. start() assigned the secret already.
            orch = app.state.tournament_orch
            secret = orch.proxy_secret()
            assert secret, "orchestrator must have a secret while running"
            await post_proxy(base, secret, [], engine_name=LIVE_ENGINE_A)
            assert orch.engine_name_for(LIVE_PROXY_ID) == LIVE_ENGINE_A
            yield app, base, t, secret
            # Not on a failure: the forensics hook still needs the pages.
            await make_page.close_all()
        finally:
            await on_server_loop(app, app.state.tournament_orch.stop(t.id))


async def post_proxy(base, secret, lines, *, proxy_id=LIVE_PROXY_ID, client=None, **extra):
    """Post proxy lines as the engine proxy would. Pass `client` (an
    AsyncClient on `base`) to reuse one connection across many posts."""
    body = {"proxy_id": proxy_id, "secret": secret, "lines": lines, **extra}
    if client is None:
        async with AsyncClient(base_url=base) as http:
            r = await http.post(PROXY_POST_PATH, json=body)
    else:
        r = await client.post(PROXY_POST_PATH, json=body)
    # 200 (want-info gate signaled) or 204 (steady-state); both succeed.
    assert r.is_success, f"proxy post failed: {r.status_code} {r.text}"


async def open_arena_engines_window(page, base: str) -> None:
    """Open the first tournament's Arena workspace plus its Engines window,
    and wait for LIVE_PROXY_ID's row there."""
    await goto_arena_tournaments(page, base)
    await page.click(".tournament-row")
    # Clean opens Engines along with the other system windows.
    await open_arena_workspace(page)
    # The workspace seeds from `proxies_active` on its initial refresh.
    await page.wait_for_selector(
        ".wb-engines .wb-sched-list .wb-sched-live .wb-sched-attach-btn",
    )


def tournament_server_env(tmp_path: Path, engine_names) -> dict[str, str]:
    """Engine registry + SV_* env for an out-of-process server (fastchess is
    never spawned -- tests seed the PGN directly)."""
    registry = EngineRegistry(path=tmp_path / REGISTRY_FILE)
    for name in engine_names:
        registry.add(name=name, path=make_fake_uci(tmp_path, name))
    return {
        **e2e_env(tmp_path),
        "SV_TOURNAMENT_FASTCHESS_PATH": sys.executable,
    }


def _games_pgn(games) -> str:
    """Render (round, white, black, result) tuples as a minimal PGN. The
    header-only scanner only reads the four tags; the movetext is filler."""
    out = []
    for rnd, white, black, result in games:
        out += [
            '[Event "test"]',
            f'[Round "{rnd}"]',
            f'[White "{white}"]',
            f'[Black "{black}"]',
            f'[Result "{result}"]',
            "",
            f"1. e4 e5 {result}",
            "",
        ]
    return "\n".join(out) + "\n"


def seed_tournament(tmp_path: Path, name: str, engines, games, template=None):
    """Create a tournament and write its games.pgn. Returns the Tournament."""
    store = ts_mod.TournamentStore(tmp_path / "tournaments")
    t = store.create(
        name=name,
        template=template or {"tc": "5+0.05"},
        engines=[{"name": e, "cmd": f"/bin/{e}"} for e in engines],
    )
    store.pgn_path(t.id).write_text(_games_pgn(games), encoding="utf-8")
    return t


async def open_studio_tab(page, base: str, panel: str, side: str = "right") -> None:
    """Mount Studio, switch to the engines perspective, open a bottom tab
    (`panel` = its wa-tab-panel name; `side` = "left" or "right" group)."""
    await page.add_init_script(
        f"localStorage.setItem('{TOURNAMENT_UX_KEY}', '{TOURNAMENT_UX_STUDIO}')"
    )
    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)
    await page.click('button[data-perspective="engines"]')
    await page.wait_for_selector(".studio-panel")
    await page.click(f'.studio-bottom-{side} wa-tab[panel="{panel}"]')


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
    monkeypatch.setattr(
        gs, "default_state_path", lambda: tmp_path / "current_game.json"
    )
    monkeypatch.setattr(
        cfg, "default_settings_file", lambda: tmp_path / "settings.json"
    )
    monkeypatch.setattr(
        engines_mod, "default_registry_path", lambda: tmp_path / REGISTRY_FILE
    )
    monkeypatch.setattr(
        ri_mod, "default_imports_dir", lambda: tmp_path / "imports"
    )
    # Patch the canonical symbol and every module-level import binding.
    fake_root = tmp_path / "tournaments"
    monkeypatch.setattr(ts_mod, "default_root", lambda: fake_root)
    monkeypatch.setattr(app_mod, "default_root", lambda: fake_root)
    monkeypatch.setattr(tournaments_api, "default_root", lambda: fake_root)
    monkeypatch.setenv("SV_ENGINE_TMP_ROOT", str(tmp_path / "engine-tmp"))
    monkeypatch.setenv("SV_WALLPAPER_DIR", str(tmp_path / "wallpaper"))
    monkeypatch.setenv("SV_THEMES_DIR", str(tmp_path / "themes"))
    # TestClient's default Host; the DNS-rebinding guard refuses unknown names.
    monkeypatch.setenv("SV_ALLOWED_HOSTS", TEST_CLIENT_HOST)
    # Shipped playbook text only: never the developer's own overrides.
    monkeypatch.setattr(pc_mod, "default_user_dir", lambda: tmp_path / "playbook")
    pc_mod.default_catalog.cache_clear()
