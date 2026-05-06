"""Slice 9c e2e: clicking an in-progress row in Schedule opens a live
game window that subscribes to a proxy and renders the board.

Uses a fake fastchess (a sleeping Python script) and injects proxy
lines directly via the internal HTTP endpoint, so no real engine
binaries are required.

TODO: this test (and `test_e2e_tournaments_ui::test_tournament_workspace_opens_three_windows`)
broke on the feat/live-pairings branch when Schedule switched from
proxy-id rows to game-id (pair) rows: a single proxy with no peer
never confirms a pair, so no `.wb-sched-live` row appears. Fix by
either driving two proxies into a confirmed pair, or by re-enabling
the commented-out individual-proxy rows in
`web/app/tournament-workspace.js::renderSchedule` for the unpaired-
proxy case.
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

        # Inject a proxy session announcement so the workspace's
        # Schedule has a row to click. Running through HTTP ensures the
        # call happens in uvicorn's event loop (not the test's), so the
        # WS subscriber queue is correctly populated for later lines.
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
                "lines": [],
            })
            assert r.status_code == 204

        # Confirm the proxy is now active before touching the browser.
        deadline = time.time() + 2
        while time.time() < deadline:
            if orch.engine_name_for("proxy-white") == "Engine A":
                break
            time.sleep(0.02)
        assert orch.engine_name_for("proxy-white") == "Engine A"

        if browser is None:
            pytest.skip("chromium not installed")
        ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
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
                await page.click(".tournament-row")
                await page.click(".tournaments-ribbon .t-workspace")
                await page.wait_for_function(
                    "() => document.querySelectorAll('.winbox.sturddle-wb').length === 3",
                    timeout=5000,
                )

                # The workspace seeds from `proxies_active` on its
                # initial refresh — the active proxy should appear
                # immediately as a Schedule row.
                await page.wait_for_selector(
                    ".wb-sched-list .wb-sched-live .wb-sched-attach-btn",
                    timeout=5000,
                )

                # Click the watch button → live game window opens.
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

                # Drive the proxy stream:
                #   1. position → engine learns it's playing Black (FEN
                #      side-to-move = "b").
                #   2. go → triggers orientation flip + clock display.
                #   3. info → eval score renders.
                async with AsyncClient(base_url=base) as http:
                    r = await http.post("/internal/proxy", json={
                        "proxy_id": "proxy-white",
                        "secret": secret,
                        "lines": [
                            "position startpos moves e2e4",
                            "go wtime 300000 btime 300000",
                            "info depth 12 score cp 35 pv e7e5",
                        ],
                    })
                    assert r.status_code == 204, f"proxy post failed: {r.text}"

                await page.wait_for_function(
                    """() => {
                        const e = document.querySelector('.wb-livegame .lg-eval-score-bottom');
                        return e && e.textContent !== '';
                    }""",
                    timeout=5000,
                )

                # Bug 4 regression check: when the engine plays Black,
                # the board must orient with Black at the bottom.
                # cm-chessboard's setOrientation goes through an async
                # animation queue, so wait for it to settle before
                # reading the rendered coord labels.
                top_rank_label = await page.wait_for_function(
                    """() => {
                        const root = document.querySelector('.wb-livegame .lg-board');
                        if (!root) return null;
                        const ranks = [...root.querySelectorAll('text.coordinate.rank')];
                        if (!ranks.length) return null;
                        ranks.sort((a, b) => parseFloat(a.getAttribute('y')) - parseFloat(b.getAttribute('y')));
                        const top = ranks[0].textContent.trim();
                        // Black-at-bottom ⇒ topmost rank label is "1".
                        return top === "1" ? top : false;
                    }""",
                    timeout=3000,
                )
                top_rank_label = await top_rank_label.json_value()
                assert top_rank_label == "1", (
                    f"Bug 4 regression: expected top rank label '1' "
                    f"(black-at-bottom), got {top_rank_label!r}"
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
