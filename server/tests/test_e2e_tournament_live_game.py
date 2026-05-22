"""Slice 9c e2e: clicking a row in the Engines window opens a live
game window that subscribes to a proxy and renders the board.

Uses a fake fastchess (a sleeping Python script) and injects proxy
lines directly via the internal HTTP endpoint, so no real engine
binaries are required.

The single-proxy attach goes through the Engines window (proxy_id
WS path), not Live Games (game_id WS path). Live Games requires two
proxies to confirm a pair and is covered by other tests.
"""
from __future__ import annotations

import sys

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.app import create_app  # noqa: E402
from sturddle_view.config import Settings  # noqa: E402
from sturddle_view.engines import EngineRegistry  # noqa: E402
from sturddle_view.tournament import fastchess as fc_mod  # noqa: E402
from sturddle_view.tournament.fastchess import FastchessRunner  # noqa: E402

from .conftest import free_port, run_uvicorn, wait_perspective_ready  # noqa: E402


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


@pytest.mark.asyncio
async def test_live_game_window_attaches_during_run(tmp_path, monkeypatch, make_page):
    from httpx import AsyncClient

    monkeypatch.setattr(
        FastchessRunner, "detect_binary",
        staticmethod(lambda configured: configured),
    )
    monkeypatch.setattr(
        fc_mod, "build_command",
        lambda spec: [sys.executable, "-c", _FAKE_FASTCHESS, "--sleep", "60"],
    )

    port = free_port()
    s = Settings(token="test", auth_disabled=True, port=port)
    s.tournament_root = str(tmp_path / "tournaments")
    s.tournament_fastchess_path = sys.executable
    registry = EngineRegistry(path=tmp_path / "engines.json")
    registry.add(name="Engine A", path=sys.executable)
    registry.add(name="Engine B", path=sys.executable)
    app = create_app(settings=s, engine_registry=registry)

    with run_uvicorn(app, port=port) as (base, _s):
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
        try:
            # Inject a proxy session announcement so the workspace's
            # Schedule has a row to click. Running through HTTP ensures the
            # call happens in uvicorn's event loop (not the test's), so the
            # WS subscriber queue is correctly populated for later lines.
            orch = app.state.tournament_orch
            # ``await orch.start(t.id)`` (above) has already assigned the
            # secret synchronously before returning, so no poll is needed.
            secret = orch.proxy_secret()
            assert secret, "orchestrator must have a secret while running"

            async with AsyncClient(base_url=base) as http:
                r = await http.post("/internal/proxy", json={
                    "proxy_id": "proxy-white",
                    "secret": secret,
                    "engine_name": "Engine A",
                    "lines": [],
                })
                assert r.status_code == 204

            # The 204 above means proxy_session_started ran in the uvicorn
            # loop; the proxy is registered before we touch the browser.
            assert orch.engine_name_for("proxy-white") == "Engine A"

            _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
            page_errors: list[str] = []
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            page.on("console", lambda msg: page_errors.append(
                f"console.{msg.type}: {msg.text}"
            ) if msg.type == "error" else None)

            await page.goto(f"{base}/")
            await page.wait_for_selector("#play-perspective")
            await wait_perspective_ready(page)
            await page.click('button[data-perspective="engines"]')
            await page.wait_for_selector(".tournament-row")

            # Open workspace. Default opens 3 windows
            # (Standings + Live Games + Event log; the log window
            # auto-opens for running tournaments via initWorkspace).
            await page.click(".tournament-row")
            await page.click(".tournaments-ribbon .t-workspace")
            await page.wait_for_function(
                "() => document.querySelectorAll('.winbox.sturddle-wb').length === 3",
            )

            # Open the Engines window via the workspace JS API.
            # Hovering the nested submenu (Window → Tournament →
            # Engines) is brittle in Playwright; the API call is
            # what the menu handler invokes anyway.
            await page.evaluate(
                """async () => {
                    const m = await import('/ui/app/tournament-workspace.js');
                    m.getActiveWorkspace().openSystemWindow('engines');
                }"""
            )
            await page.wait_for_function(
                "() => document.querySelectorAll('.winbox.sturddle-wb').length === 4",
            )

            # The workspace seeds from `proxies_active` on its
            # initial refresh — the active proxy should appear
            # immediately as an Engines row.
            await page.wait_for_selector(
                ".wb-engines .wb-sched-list .wb-sched-live .wb-sched-attach-btn",
            )

            # Click the watch button → live game window opens.
            await page.click(
                ".wb-engines .wb-sched-list .wb-sched-live .wb-sched-attach-btn"
            )
            await page.wait_for_selector(".wb-livegame .lg-board")

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
            )
            top_rank_label = await top_rank_label.json_value()
            assert top_rank_label == "1", (
                f"Bug 4 regression: expected top rank label '1' "
                f"(black-at-bottom), got {top_rank_label!r}"
            )

            # Close-on-terminal: stopping the tournament closes
            # stale live windows (proxy-id attaches are always
            # stale) but keeps standard windows so the user can
            # review final state. 5 .winbox up before stop:
            # Standings + Live Games + Event log + Engines (4
            # standard) + 1 live (watch). After stop: 4 standard,
            # 0 live.
            assert await page.locator(".winbox.sturddle-wb").count() == 5
            assert await page.locator(".winbox.sturddle-wb-live").count() == 1

            await app.state.tournament_orch.stop(t.id)

            # Live window goes away (proxy-id, stale on terminal).
            await page.wait_for_function(
                "() => document.querySelectorAll('.winbox.sturddle-wb-live').length === 0",
            )
            # Standard windows remain.
            assert await page.locator(".winbox.sturddle-wb").count() == 4

            assert page_errors == [], "JS errors:\n" + "\n".join(page_errors)
        finally:
            try:
                await app.state.tournament_orch.stop(t.id)
            except Exception:
                pass
