"""E2E: Tabbing into the Arena tournament list shows a focus ring on the
selected row (Playwright/Chromium).

The list is a markSelectable region, whose own outline is suppressed, so the
ring has to land on the current row -- like the data-table rowNav pattern.

Skipped if Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from .conftest import (  # noqa: E402
    assert_no_page_errors,
    goto_arena_tournaments,
    run_uvicorn_subprocess,
    seed_tournament,
    tournament_server_env,
    watch_page_errors,
)

ENGINES = ("engine-A", "engine-B")
VIEWPORT = {"width": 1400, "height": 900}
LIST_SEL = ".tournaments-list"
# Upper bound on Tab presses from the Sort button to the list (menubar +
# ribbon stops); reaching the list sooner ends the walk.
MAX_TABS = 20

_LIST_FOCUSED = f"() => document.activeElement?.matches('{LIST_SEL}') ?? false"
# Per row, in display order: [is .selected, has a visible outline].
_ROW_RINGS = (
    "() => [...document.querySelectorAll('.tournament-row')].map((r) => {"
    " const s = getComputedStyle(r);"
    " return [r.classList.contains('selected'), s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0]; })"
)


@pytest.fixture
def server(tmp_path):
    for name in ("focus-a", "focus-b"):
        seed_tournament(tmp_path, name, list(ENGINES), [])
    with run_uvicorn_subprocess(env_overrides=tournament_server_env(tmp_path, ENGINES)) as base:
        yield base


async def _tab_into_list(page):
    await page.focus(".tmb-sort-btn")
    for _ in range(MAX_TABS):
        if await page.evaluate(_LIST_FOCUSED):
            return
        await page.keyboard.press("Tab")
    assert await page.evaluate(_LIST_FOCUSED), "Tab never reached the tournament list"


@pytest.mark.asyncio
async def test_tab_into_list_rings_selected_row(server, make_page):
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await goto_arena_tournaments(page, server)

    await _tab_into_list(page)
    rings = await page.evaluate(_ROW_RINGS)
    assert [ringed for _sel, ringed in rings] == [sel for sel, _ring in rings], rings
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_shift_arrow_ring_follows_lead(server, make_page):
    """Extending a selection moves the ring to the extended-to row; the
    anchor stays selected but unringed."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await goto_arena_tournaments(page, server)

    await _tab_into_list(page)
    await page.keyboard.press("Home")
    await page.keyboard.press("Shift+ArrowDown")
    rings = await page.evaluate(_ROW_RINGS)
    assert rings == [[True, False], [False, True]], rings
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_mouse_click_shows_no_ring(server, make_page):
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await goto_arena_tournaments(page, server)

    await page.click(".tournament-row")
    rings = await page.evaluate(_ROW_RINGS)
    assert not any(ringed for _sel, ringed in rings), rings
    assert_no_page_errors(errors)
