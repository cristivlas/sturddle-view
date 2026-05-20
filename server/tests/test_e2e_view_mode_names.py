"""End-to-end test: view mode shows PGN player names in the clock area.

Regression for a listener-order race where play.js's board_update handler
called ``view.setHumanWhite(...)`` after GameView's handler had already
written the PGN names, clobbering them with ``Human`` / ``<engine name>``.

Drives a real browser via Playwright. Skipped if Playwright or its Chromium
isn't available so unit-only test runs aren't blocked.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.async_api")

from sturddle_view.app import create_app  # noqa: E402
from sturddle_view.config import Settings  # noqa: E402
from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import run_uvicorn  # noqa: E402

WHITE_NAME = "Celeris 2.0 64-bit"
BLACK_NAME = "Panda 1.1 64-bit"
ENGINE_NAME = "MyEngine 1.0"


@pytest.fixture
def server(tmp_path):
    settings = Settings(token="test-token", auth_disabled=True)
    settings.pgn_dir = tmp_path / "pgn"
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry)
    with run_uvicorn(app) as (base, _s):
        yield base, app


@pytest.mark.asyncio
async def test_view_mode_clock_names_after_hard_reload(server, page):
    """In view mode after a hard reload, the clock-area name labels must
    reflect the PGN's White/Black headers, not the play-mode placeholders.
    """
    base, app = server

    from sturddle_view.play.human_vs_engine import HumanVsEngine, ViewModeParams

    hve = HumanVsEngine(
        engine_path="/nonexistent",
        bus=app.state.event_bus,
        openings=getattr(app.state, "openings", None),
        settings=app.state.settings,
    )
    hve._engine_name = ENGINE_NAME
    await hve.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "c7c5", "g1f3", "d7d6"],
        clock_history=None,
        white_name=WHITE_NAME,
        black_name=BLACK_NAME,
    ))
    app.state.hve = hve

    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")
    # view-controls become visible only after play.js's board_update
    # handler runs (it toggles view-mode UI based on the viewing flag) --
    # which is the same handler that used to clobber the PGN names.
    # Once it's visible BOTH listeners have run on the first board_update.
    await page.wait_for_function(
        "() => getComputedStyle(document.querySelector('#view-controls'))"
        ".display !== 'none'",
    )

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
