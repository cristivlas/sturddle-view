"""E2E: the browser must never restore saved form state into wa-* controls.

Skipped if Playwright is missing.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from .conftest import e2e_env, run_uvicorn_subprocess, wait_perspective_ready  # noqa: E402


PLAY_PERSP = "#play-perspective"
SETTINGS_BTN = "#settings-btn"
ENGINES_TAB = "wa-dialog wa-tab[panel='engines']"
SEARCH_BTN = "wa-dialog .engines-search-btn"
SEARCH_INPUT = "wa-dialog .engines-search"
DIALOG_INPUTS = "wa-dialog wa-input"
LEAK_PREFIX = "leak"

# Give every dialog wa-input a distinct value and let Lit flush so the
# value reaches ElementInternals (that is what the browser snapshots).
LEAK_JS = f"""
  async () => {{
    const els = [...document.querySelectorAll({DIALOG_INPUTS!r})];
    els.forEach((el, i) => {{ el.value = {LEAK_PREFIX!r} + i; }});
    await Promise.all(els.map((el) => el.updateComplete));
    return els.length;
  }}
"""


@pytest.fixture
def server(tmp_path):
    env = e2e_env(tmp_path)
    with run_uvicorn_subprocess(env_overrides=env) as base:
        yield base


async def _open_app(page, base: str) -> None:
    await page.goto(base + "/")
    await page.wait_for_selector(PLAY_PERSP)
    await wait_perspective_ready(page)


@pytest.mark.asyncio
async def test_engines_search_stays_empty_after_history_restore(server, page):
    """Type into Settings fields, leave, come back through history, open
    Engines search: the box must be empty, not someone else's text."""
    await _open_app(page, server)
    await page.click(SETTINGS_BTN)
    await page.wait_for_selector(ENGINES_TAB)
    assert await page.evaluate(LEAK_JS) > 0

    # Playwright's Chromium has bfcache off: go_back is a full load with
    # form-state restore, the same path as a session restore or tab discard.
    await page.goto("about:blank")
    await page.go_back()
    await page.wait_for_selector(PLAY_PERSP)
    await wait_perspective_ready(page)

    await page.click(SETTINGS_BTN)
    await page.click(ENGINES_TAB)
    await page.click(SEARCH_BTN)
    value = await page.locator(SEARCH_INPUT).evaluate("(el) => el.value")
    assert not value, f"engines search restored foreign form state: {value!r}"
