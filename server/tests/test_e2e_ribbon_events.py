"""End-to-end test: the settings-changed window CustomEvent must reach its
listener.

The ribbon UI is wired together by window-level CustomEvents (APP_EVT in
web/app/app-events.js). They fire only on real UI actions and have no unit
coverage, so a desync between a dispatch site and a listener (e.g. one side
left on a stale string literal after the names were centralized) fails
silently -- a dead listener, no error.

This drives the action and asserts the LISTENER'S EFFECT: changing ribbon
side dispatches settings-changed, whose listener (refreshRibbonSide) sets
body[data-ribbon-side]. Verified to fail if the listener name is desynced
from the dispatch.

(layout-changed has no single-path observable -- syncFloatState and the
board/dock relayout listeners are each also driven by resize / ribbon-active
/ media-query change, so no DOM outcome isolates that one listener. Left to
manual/integration coverage rather than a test that can't actually catch a
desync.)

The ribbon-side control lives in the settings "display" tab and is hidden on
mobile viewports; this runs at the desktop default so the row is present.

Drives a real browser via Playwright.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import run_uvicorn_subprocess, wait_perspective_ready  # noqa: E402


# The ribbon-side <wa-select> has no stable id; reach it by its option set
# (the only select carrying a "float" option in the settings dialog).
_SET_RIBBON_SIDE_JS = """(side) => {
  const sel = [...document.querySelectorAll('wa-select')].find(
    s => [...s.querySelectorAll('wa-option')].some(o => o.value === 'float')
  );
  if (!sel) return false;
  sel.value = side;
  sel.dispatchEvent(new Event('change'));
  return true;
}"""

_RIBBON_SELECT_PRESENT_JS = (
    "() => [...document.querySelectorAll('wa-select')].some("
    "s => [...s.querySelectorAll('wa-option')].some(o => o.value === 'float'))"
)


@pytest.fixture
def server(tmp_path):
    # A fake engine keeps API calls from rejecting with no_engine_configured;
    # the ribbon UI never spawns it.
    registry_path = tmp_path / "engines.json"
    seed = EngineRegistry(path=registry_path)
    e = seed.add(name="MyEngine", path="/nonexistent/engine")
    seed.select(e.id)
    env = {
        "SV_PGN_DIR": str(tmp_path / "pgn"),
        "SV_TOURNAMENT_ROOT": str(tmp_path / "tournaments"),
        "SV_ENGINE_REGISTRY_PATH": str(registry_path),
        "SV_IMPORTS_DIR": str(tmp_path / "imports"),
        "SV_SETTINGS_FILE": str(tmp_path / "settings.json"),
        "SV_GAME_STATE_PATH": str(tmp_path / "current_game.json"),
    }
    with run_uvicorn_subprocess(env_overrides=env) as base:
        yield base


async def _open_settings_display(page, base):
    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)
    # Open the settings dialog straight to the "display" tab, where the
    # ribbon-side control lives (same dispatch the app uses internally).
    await page.evaluate(
        "() => window.dispatchEvent(new CustomEvent('sturddle:open-settings', "
        "{ detail: { tab: 'display' } }))"
    )
    await page.wait_for_function(_RIBBON_SELECT_PRESENT_JS)


@pytest.mark.asyncio
async def test_settings_change_event_updates_ribbon_side(server, page):
    """Changing ribbon side dispatches settings-changed; refreshRibbonSide
    must react and set body[data-ribbon-side]. A dead listener leaves the
    attribute stale (verified: this fails if the listener name is desynced)."""
    await _open_settings_display(page, server)

    assert await page.evaluate(_SET_RIBBON_SIDE_JS, "right") is True
    # refreshRibbonSide GETs /settings then applies -- wait for the effect.
    await page.wait_for_function(
        "() => document.body.dataset.ribbonSide === 'right'",
    )

    assert await page.evaluate(_SET_RIBBON_SIDE_JS, "left") is True
    await page.wait_for_function(
        "() => document.body.dataset.ribbonSide === 'left'",
    )
