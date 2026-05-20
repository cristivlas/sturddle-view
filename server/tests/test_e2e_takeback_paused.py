"""End-to-end: takeback button must be enabled while the game is paused.

Server allows takeback while paused; the play ribbon must not disable the
button. Drives a real browser via Playwright.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

pytest.importorskip("playwright.async_api")

from sturddle_view.app import create_app  # noqa: E402
from sturddle_view.config import Settings  # noqa: E402
from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import run_uvicorn  # noqa: E402


@pytest.fixture
def server(tmp_path):
    settings = Settings(token="test-token", auth_disabled=True)
    settings.pgn_dir = tmp_path / "pgn"
    registry = EngineRegistry(path=tmp_path / "engines.json")
    e = registry.add(name="MyEngine", path="/nonexistent/engine")
    registry.select(e.id)
    app = create_app(settings=settings, engine_registry=registry)
    with run_uvicorn(app) as (base, _s):
        yield base, app


@pytest.mark.asyncio
async def test_takeback_button_enabled_while_paused(server, browser):
    if browser is None:
        pytest.skip("chromium not installed")
    base, app = server

    from sturddle_view.play.human_vs_engine import HumanVsEngine, TimeControl

    hve = HumanVsEngine(
        engine_path="/nonexistent/engine",
        bus=app.state.event_bus,
        openings=getattr(app.state, "openings", None),
        settings=app.state.settings,
    )
    # No real engine: stub ensure + suppress engine kicks.
    class _Stub:
        def send_line(self, _): pass
        async def quit(self): return None
    async def _ensure():
        hve._engine = _Stub()
        return hve._engine
    hve._ensure_engine = _ensure
    hve._engine_to_move = AsyncMock()

    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    await hve.submit_move("e2e4")
    # Inject the engine's reply directly so it's the human's turn again,
    # which is required by pause().
    import chess
    async with hve._lock:
        hve._clock.append_snapshot()
        hve._consume_turn_time()
        hve._board.push(chess.Move.from_uci("e7e5"))
        await hve._publish_board()
        await hve._publish_clock()
    await hve.pause()
    assert hve.is_paused
    app.state.hve = hve

    ctx = await browser.new_context()
    page = await ctx.new_page()
    try:
        await page.goto(base + "/")
        await page.wait_for_selector("#play-perspective", timeout=5000)
        # First wait for the Resume affordance (icon=forward-step) so we know
        # the client has applied paused=true. Then assert takeback is enabled.
        await page.wait_for_function(
            "() => document.querySelector('#pause wa-icon')?.getAttribute('name') === 'forward-step'",
            timeout=5000,
        )
        disabled = await page.evaluate(
            "() => document.querySelector('#takeback').disabled"
        )
        assert disabled is False, "takeback button must be enabled while paused"
    finally:
        await ctx.close()
