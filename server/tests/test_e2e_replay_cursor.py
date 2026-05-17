"""Regression test: replaying a NEW import while a PREVIOUS import has a
non-zero view cursor must NOT propagate the old cursor into the new game.

Bug: play.js cached _cachedBoardUpdate replay on remount re-emits the
previous game's settled board_update. play.js's handler in turn fires a
synthetic /view/goto POST (via syncCommentsVisibility opening the
commentary window), and that POST lands on the NEW server game --
corrupting its cursor to the old value.
"""
from __future__ import annotations

import socket
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.async_api")

from sturddle_view.app import create_app  # noqa: E402
from sturddle_view.config import Settings  # noqa: E402
from sturddle_view.engines import EngineRegistry  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_fake_uci(root: Path, name: str) -> str:
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


@pytest.fixture
def server(tmp_path):
    import uvicorn
    settings = Settings(token="test-token", auth_disabled=True)
    settings.pgn_dir = tmp_path / "pgn"
    registry = EngineRegistry(path=tmp_path / "engines.json")
    eng = registry.add(name="FakeEngine", path=_make_fake_uci(tmp_path, "FakeEngine"))
    registry.select(eng.id)
    app = create_app(settings=settings, engine_registry=registry)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws="wsproto")
    s = uvicorn.Server(config)
    thread = threading.Thread(target=s.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline and not s.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", app
    s.should_exit = True
    s.force_exit = True
    thread.join(timeout=2)


_PGN_A = """\
[Event "A"]
[Site "?"]
[Date "????.??.??"]
[Round "?"]
[White "WA"]
[Black "BA"]
[Result "*"]

1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7 6. Re1 b5 7. Bb3 d6
8. c3 O-O 9. h3 Nb8 10. d4 *
"""

_PGN_B = """\
[Event "B"]
[Site "?"]
[Date "????.??.??"]
[Round "?"]
[White "WB"]
[Black "BB"]
[Result "*"]

1. d4 d5 2. c4 e6 3. Nc3 Nf6 4. Bg5 Be7 5. e3 O-O 6. Nf3 Nbd7
7. Rc1 c6 8. Bd3 dxc4 9. Bxc4 Nd5 10. Bxe7 *
"""


@pytest.mark.asyncio
async def test_replay_while_old_cursor_nonzero_does_not_corrupt_new_game(server, browser):
    """Repro the production Replay bug: cursor=N on game A, import game B,
    remount play -- new game's cursor must stay at 0."""
    if browser is None:
        pytest.skip("chromium not installed")
    base, app = server

    ctx = await browser.new_context()
    page = await ctx.new_page()
    try:
        await page.goto(base + "/")
        await page.wait_for_selector("#play-perspective", timeout=5000)
        await page.wait_for_timeout(500)

        # Ensure the failure mode's prerequisite: commentary window is on.
        # The bug only repros when syncCommentsVisibility() decides to open
        # the commentary window on board_update.
        await page.evaluate(
            "fetch('/settings', {method:'PUT',"
            " headers:{'Content-Type':'application/json'},"
            " body: JSON.stringify({view_show_pgn_comments: true})})"
        )
        await page.wait_for_timeout(200)

        # Import game A and navigate cursor to a non-zero ply.
        import_status = await page.evaluate(
            "async (pgn) => { const r = await fetch('/game/import', {method:'POST',"
            " headers:{'Content-Type':'application/json'},"
            " body: JSON.stringify({text: pgn, format: 'pgn'})});"
            " return { status: r.status, body: await r.text() }; }",
            _PGN_A,
        )
        assert import_status["status"] == 200, f"import A failed: {import_status}"
        await page.wait_for_timeout(200)
        # Tell client to sync to the new game id (mirrors normal UI flow).
        await page.evaluate("fetch('/game/sync', {method:'POST'})")
        await page.wait_for_timeout(200)
        goto_status = await page.evaluate(
            "async () => { const r = await fetch('/game/view/goto', {method:'POST',"
            " headers:{'Content-Type':'application/json'},"
            " body: JSON.stringify({ply: 10})});"
            " return { status: r.status, body: await r.text() }; }"
        )
        assert goto_status["status"] == 200, f"goto failed: {goto_status}"
        await page.wait_for_timeout(300)
        assert app.state.hve._view_cursor == 10, "precondition: game A cursor at 10"

        # Switch away from play (simulates user opening tournament window
        # while play perspective is unmounted -- as the Replay button does).
        await page.click('button[data-perspective="engines"]')
        await page.wait_for_timeout(300)

        # Replay: import game B + activate play (mirror tournament-live-game).
        await page.evaluate(
            "async (pgn) => {"
            " await fetch('/game/import', {method:'POST',"
            "  headers:{'Content-Type':'application/json'},"
            "  body: JSON.stringify({text: pgn, format: 'pgn'})});"
            " window.dispatchEvent(new CustomEvent("
            "  'sturddle:activate-perspective', {detail:{id:'play'}}));"
            "}",
            _PGN_B,
        )
        # Wait past the mount's /game/sync + any spurious POST.
        await page.wait_for_timeout(1500)

        # Assertion: server's view cursor on game B must be 0. Anything
        # else means a stale cached board_update from game A fired a
        # synthetic /view/goto against game B.
        assert app.state.hve._view_cursor == 0, (
            f"new game cursor corrupted to {app.state.hve._view_cursor}"
        )
    finally:
        await ctx.close()
