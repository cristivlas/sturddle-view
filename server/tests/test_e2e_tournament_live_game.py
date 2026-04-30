"""Slice 9c e2e: clicking an in-progress row in Schedule opens a live
game window that subscribes to a proxy and renders the board.

Heavyweight test — runs real fastchess + real engines + real browser.
Skipped if either is missing.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright  # noqa: E402

from sturddle_view.app import create_app  # noqa: E402
from sturddle_view.config import Settings  # noqa: E402
from sturddle_view.engines import EngineRegistry  # noqa: E402


FASTCHESS_PATH = Path.home() / "Projects" / "fastchess" / "fastchess"
ENGINE_CANDIDATES = [
    Path.home() / "Projects" / "sturddle-2" / "dist" / "sturddle-2.5.0-Linux-x86_64",
    Path.home() / "Projects" / "sturddle-2" / "dist" / "sturddle-2.4.0-Linux-x86_64",
]


def _have_real_setup() -> bool:
    if not FASTCHESS_PATH.is_file() or not os.access(FASTCHESS_PATH, os.X_OK):
        return False
    return all(p.is_file() and os.access(p, os.X_OK) for p in ENGINE_CANDIDATES)


pytestmark = pytest.mark.skipif(
    not _have_real_setup(),
    reason="real fastchess + sturddle not available on this machine",
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.asyncio
async def test_live_game_window_attaches_during_run(tmp_path):
    import uvicorn

    port = _free_port()
    s = Settings(token="test", auth_disabled=True, port=port)
    s.tournament_root = str(tmp_path / "tournaments")
    s.tournament_fastchess_path = str(FASTCHESS_PATH)
    registry = EngineRegistry(path=tmp_path / "engines.json")
    registry.add(name="Sturddle 2.5.0", path=str(ENGINE_CANDIDATES[0]))
    registry.add(name="Sturddle 2.4.0", path=str(ENGINE_CANDIDATES[1]))
    app = create_app(settings=s, engine_registry=registry)

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline and not server.started:
        time.sleep(0.05)
    assert server.started

    try:
        # Pre-create + start a tournament with a slow TC so we have
        # plenty of time to see in-progress games in the browser.
        store = app.state.tournament_store
        t = store.create(
            name="live-e2e",
            template={
                "tc": "5+0.1",
                "rounds": 1,
                "games_in_parallel": 1,
                "hash": 16,
                "threads": 1,
            },
            engines=[
                {"name": "Sturddle 2.5.0", "cmd": str(ENGINE_CANDIDATES[0])},
                {"name": "Sturddle 2.4.0", "cmd": str(ENGINE_CANDIDATES[1])},
            ],
        )
        await app.state.tournament_orch.start(t.id)

        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch()
            except Exception as e:
                pytest.skip(f"chromium not installed: {e}")
            ctx = await browser.new_context()
            page = await ctx.new_page()
            page_errors: list[str] = []
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            page.on("console", lambda msg: page_errors.append(
                f"console.{msg.type}: {msg.text}"
            ) if msg.type == "error" else None)

            try:
                await page.goto(f"http://127.0.0.1:{port}/")
                await page.wait_for_selector("#play-perspective", timeout=5000)
                await page.click('button[data-perspective="engines"]')
                await page.click('#engines-perspective wa-tab[panel="tournaments"]')
                await page.wait_for_selector(".tournament-row", timeout=5000)
                # Open workspace.
                await page.click(".tournament-row .row-workspace")
                await page.wait_for_function(
                    "() => document.querySelectorAll('.winbox.sturddle-wb').length === 3",
                    timeout=5000,
                )

                # Wait for the game_paired event → an in-progress row to
                # appear in the Schedule.
                await page.wait_for_selector(
                    ".wb-sched-list .wb-sched-live .wb-sched-attach-btn",
                    timeout=30000,
                )

                # Click the first attach button → a live game window opens.
                await page.click(
                    ".wb-sched-list .wb-sched-live .wb-sched-attach-btn"
                )
                await page.wait_for_selector(".wb-livegame .lg-board", timeout=5000)
                # The status pill should report "live" once WS opens.
                await page.wait_for_function(
                    """() => {
                        const e = document.querySelector('.wb-livegame .lg-status');
                        return e && /live|ended/i.test(e.textContent);
                    }""",
                    timeout=5000,
                )

                # Wait a few seconds for moves to arrive — the board should
                # render at least one piece beyond the starting position.
                # Cheap proof: an `info` line lands and we set the eval.
                await page.wait_for_function(
                    """() => {
                        const e = document.querySelector('.wb-livegame .lg-eval-score');
                        return e && e.textContent !== '—' && e.textContent !== '';
                    }""",
                    timeout=20000,
                )

                # No JS errors during the run.
                assert page_errors == [], "JS errors:\n" + "\n".join(page_errors)
            finally:
                await browser.close()
    finally:
        # Clean shutdown of the tournament.
        try:
            await app.state.tournament_orch.stop(t.id)
        except Exception:
            pass
        server.should_exit = True
        thread.join(timeout=10)
