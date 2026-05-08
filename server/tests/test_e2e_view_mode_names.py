"""End-to-end test: view mode shows PGN player names in the clock area.

Regression for a listener-order race where play.js's board_update handler
called ``view.setHumanWhite(...)`` after GameView's handler had already
written the PGN names, clobbering them with ``Human`` / ``<engine name>``.

Drives a real browser via Playwright. Skipped if Playwright or its Chromium
isn't available so unit-only test runs aren't blocked.
"""
from __future__ import annotations

import socket
import threading
import time

import pytest

pytest.importorskip("playwright.async_api")

from sturddle_view.app import create_app  # noqa: E402
from sturddle_view.config import Settings  # noqa: E402
from sturddle_view.engines import EngineRegistry  # noqa: E402

WHITE_NAME = "Celeris 2.0 64-bit"
BLACK_NAME = "Panda 1.1 64-bit"
ENGINE_NAME = "MyEngine 1.0"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path):
    import uvicorn

    settings = Settings(token="test-token", auth_disabled=True)
    settings.pgn_dir = tmp_path / "pgn"
    registry = EngineRegistry(path=tmp_path / "engines.json")
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


@pytest.mark.asyncio
async def test_view_mode_clock_names_after_hard_reload(server, browser):
    """In view mode after a hard reload, the clock-area name labels must
    reflect the PGN's White/Black headers, not the play-mode placeholders.
    """
    if browser is None:
        pytest.skip("chromium not installed")
    base, app = server

    from sturddle_view.play.human_vs_engine import HumanVsEngine

    hve = HumanVsEngine(
        engine_path="/nonexistent",
        bus=app.state.event_bus,
        openings=getattr(app.state, "openings", None),
        settings=app.state.settings,
    )
    hve._engine_name = ENGINE_NAME
    await hve.enter_view_mode(
        start_fen=None,
        moves_uci=["e2e4", "c7c5", "g1f3", "d7d6"],
        clock_history=None,
        white_name=WHITE_NAME,
        black_name=BLACK_NAME,
    )
    app.state.hve = hve

    ctx = await browser.new_context()
    page = await ctx.new_page()
    try:
        await page.goto(base + "/")
        await page.wait_for_selector("#play-perspective", timeout=5000)
        # Wait past the auto /game/sync (200ms) and let the play.js
        # board_update listener run -- that's the one that used to clobber.
        await page.wait_for_timeout(1500)

        names = await page.evaluate(
            """() => ({
                top: document.querySelector('.clock-name[data-side="top"]')?.textContent ?? null,
                bottom: document.querySelector('.clock-name[data-side="bottom"]')?.textContent ?? null,
            })"""
        )
        # Bottom defaults to white when not flipped; top is black.
        assert names["bottom"] == WHITE_NAME, (
            f"bottom clock name should be PGN white ({WHITE_NAME!r}), got {names['bottom']!r}"
        )
        assert names["top"] == BLACK_NAME, (
            f"top clock name should be PGN black ({BLACK_NAME!r}), got {names['top']!r}"
        )
        # And explicitly NOT the play-mode placeholders.
        assert names["bottom"] != "Human"
        assert names["top"] != ENGINE_NAME
    finally:
        await ctx.close()
