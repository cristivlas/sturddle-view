"""Slice 9c e2e: clicking an in-progress row in Schedule opens a live
game window that subscribes to a proxy and renders the board.

Uses a fake fastchess (a sleeping Python script) and injects proxy
lines directly via the internal HTTP endpoint, so no real engine
binaries are required.
"""
from __future__ import annotations

import socket
import sys
import threading
import time

import pytest

pytest.importorskip("playwright.async_api")

from sturddle_view.app import create_app  # noqa: E402
from sturddle_view.config import Settings  # noqa: E402
from sturddle_view.engines import EngineRegistry  # noqa: E402
from sturddle_view.tournament import fastchess as fc_mod  # noqa: E402
from sturddle_view.tournament.fastchess import FastchessRunner  # noqa: E402


# Minimal fake fastchess: stays alive so the tournament stays "running".
_FAKE_FASTCHESS = r"""
import sys, time
i = 1
while i < len(sys.argv):
    if sys.argv[i] == "--sleep":
        time.sleep(float(sys.argv[i + 1])); i += 2
    else:
        i += 1
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.asyncio
async def test_live_game_window_attaches_during_run(tmp_path, monkeypatch, browser):
    import uvicorn
    from httpx import AsyncClient

    monkeypatch.setattr(
        FastchessRunner, "detect_binary",
        staticmethod(lambda configured: configured),
    )
    monkeypatch.setattr(
        fc_mod, "build_command",
        lambda spec: [sys.executable, "-c", _FAKE_FASTCHESS, "--sleep", "60"],
    )

    port = _free_port()
    s = Settings(token="test", auth_disabled=True, port=port)
    s.tournament_root = str(tmp_path / "tournaments")
    s.tournament_fastchess_path = sys.executable
    registry = EngineRegistry(path=tmp_path / "engines.json")
    registry.add(name="Engine A", path=sys.executable)
    registry.add(name="Engine B", path=sys.executable)
    app = create_app(settings=s, engine_registry=registry)

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws="wsproto")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline and not server.started:
        time.sleep(0.05)
    assert server.started

    base = f"http://127.0.0.1:{port}"

    try:
        # Create and start a tournament.
        store = app.state.tournament_store
        t = store.create(
            name="live-e2e",
            template={"tc": "5+0.1", "rounds": 1, "games_in_parallel": 1},
            engines=[
                {"name": "Engine A", "cmd": sys.executable},
                {"name": "Engine B", "cmd": sys.executable},
            ],
        )
        await app.state.tournament_orch.start(t.id)

        # Inject two proxy sessions via the HTTP endpoint so the pair_index
        # creates a paired game. Running through HTTP ensures the calls happen
        # in uvicorn's event loop (not the test's), so the queues the WS
        # subscribers drain are correctly populated.
        orch = app.state.tournament_orch
        deadline = time.time() + 5
        secret = None
        while time.time() < deadline:
            secret = orch.proxy_secret()
            if secret:
                break
            time.sleep(0.05)
        assert secret, "orchestrator must have a secret while running"

        async with AsyncClient(base_url=base) as http:
            r = await http.post("/internal/proxy", json={
                "proxy_id": "proxy-white",
                "secret": secret,
                "engine_name": "Engine A",
                "lines": ["position startpos"],
            })
            assert r.status_code == 204
            r = await http.post("/internal/proxy", json={
                "proxy_id": "proxy-black",
                "secret": secret,
                "engine_name": "Engine B",
                "lines": ["position startpos moves e2e4"],
            })
            assert r.status_code == 204

        # Confirm pairing happened before touching the browser.
        deadline = time.time() + 2
        while time.time() < deadline:
            if orch._pair_index.game_id_for("proxy-white") is not None:
                break
            time.sleep(0.02)
        assert orch._pair_index.game_id_for("proxy-white") is not None

        if browser is None:
            pytest.skip("chromium not installed")
        ctx = await browser.new_context()
        page = await ctx.new_page()
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on("console", lambda msg: page_errors.append(
            f"console.{msg.type}: {msg.text}"
        ) if msg.type == "error" else None)

        try:
                await page.goto(f"{base}/")
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

                # The workspace seeds from `games_in_progress` on its initial
                # refresh — the paired game should appear immediately.
                await page.wait_for_selector(
                    ".wb-sched-list .wb-sched-live .wb-sched-attach-btn",
                    timeout=5000,
                )

                # Click the first attach button → live game window opens.
                await page.click(
                    ".wb-sched-list .wb-sched-live .wb-sched-attach-btn"
                )
                await page.wait_for_selector(".wb-livegame .lg-board", timeout=5000)

                # WS connects → status becomes "live".
                await page.wait_for_function(
                    """() => {
                        const e = document.querySelector('.wb-livegame .lg-status');
                        return e && /live|ended/i.test(e.textContent);
                    }""",
                    timeout=5000,
                )

                # Determine which proxy the browser subscribed to by reading
                # the button's title attribute (= proxy_id).
                proxy_id_for_ws = await page.evaluate(
                    "() => document.querySelector('.wb-sched-attach-btn')?.title"
                )
                assert proxy_id_for_ws, "could not determine proxy_id from button"

                # Push an info line with an eval score via HTTP so it runs
                # in uvicorn's loop and reaches the WS subscriber's queue.
                async with AsyncClient(base_url=base) as http:
                    r = await http.post("/internal/proxy", json={
                        "proxy_id": proxy_id_for_ws,
                        "secret": secret,
                        "lines": ["info depth 12 score cp 35 pv e2e4 e7e5"],
                    })
                    assert r.status_code == 204, f"proxy post failed: {r.text}"

                await page.wait_for_function(
                    """() => {
                        const e = document.querySelector('.wb-livegame .lg-eval-score');
                        return e && e.textContent !== '—' && e.textContent !== '';
                    }""",
                    timeout=5000,
                )

                assert page_errors == [], "JS errors:\n" + "\n".join(page_errors)
        finally:
            await ctx.close()
    finally:
        try:
            await app.state.tournament_orch.stop(t.id)
        except Exception:
            pass
        server.should_exit = True
        server.force_exit = True
        thread.join(timeout=2)
