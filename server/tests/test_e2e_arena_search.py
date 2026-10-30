"""E2E: Arena tournament search (Playwright/Chromium).

The ribbon's Search button drops a bar under the menubar; typing filters the
list on tournament or engine names; Esc closes it and restores every row.
Run at a sub-1 device pixel ratio too. Open workspace windows are minimized
while searching (with a toast) and restored on close; a click on the toast
closes the search.

Skipped if Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from .conftest import (  # noqa: E402
    assert_no_page_errors,
    goto_arena_tournaments,
    open_arena_workspace,
    run_uvicorn_subprocess,
    seed_tournament,
    tournament_server_env,
    wait_eval,
    watch_page_errors,
)

A, B, C = "alpha-engine", "beta-engine", "gamma-engine"
VIEWPORT = {"width": 1400, "height": 900}
DPRS = (0.75, 1)
SEARCH_BTN = ".tournaments-ribbon .t-search"
SEARCH_INPUT = ".tournaments-search"
# The bar fades out after closing; its open state is its class.
SEARCH_OPEN = "() => document.querySelector('.tournaments-search-wrap').classList.contains('open')"
TOAST_TEXT = "Windows minimized while searching"
# Arena windows: [open, minimized] counts.
_WINDOWS = "() => { const w = [...document.querySelectorAll('.winbox.sturddle-wb-arena')]; return [w.length, w.filter((e) => e.classList.contains('min')).length]; }"
NAMES = "() => [...document.querySelectorAll('.tournament-row .tournament-name')].map((e) => e.textContent).sort()"


@pytest.fixture
def server(tmp_path):
    seed_tournament(tmp_path, "spring-cup", [A, B], [])
    seed_tournament(tmp_path, "summer-cup", [B, C], [])
    with run_uvicorn_subprocess(env_overrides=tournament_server_env(tmp_path, (A, B, C))) as base:
        yield base


async def _type(page, text):
    await page.locator(SEARCH_INPUT).locator("input").fill(text)


@pytest.mark.asyncio
@pytest.mark.parametrize("dpr", DPRS)
async def test_search_filters_by_name_and_engine(server, make_page, dpr):
    _ctx, page = await make_page(viewport=VIEWPORT, device_scale_factor=dpr)
    errors = watch_page_errors(page)
    await goto_arena_tournaments(page, server)

    await page.click(SEARCH_BTN)
    await page.wait_for_selector(f"{SEARCH_INPUT}", state="visible")
    await _type(page, "SPRING")
    await wait_eval(page, NAMES, ["spring-cup"])
    await _type(page, "gamma")
    await wait_eval(page, NAMES, ["summer-cup"])
    await _type(page, "nothing-matches")
    await wait_eval(page, NAMES, [])
    await page.wait_for_selector(".tournaments-no-match", state="visible")

    await page.keyboard.press("Escape")
    await wait_eval(page, NAMES, ["spring-cup", "summer-cup"])
    await wait_eval(page, SEARCH_OPEN, False)
    assert_no_page_errors(errors)


@pytest.mark.asyncio
@pytest.mark.parametrize("close", ["escape", "toast"])
async def test_search_minimizes_workspace_and_restores(server, make_page, close):
    """Opening search minimizes the workspace windows and says so in a toast;
    closing it (Esc, or a click on the toast) restores them, toast gone."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await goto_arena_tournaments(page, server)
    await page.click(".tournament-row")
    await open_arena_workspace(page)
    opened, _ = await page.evaluate(_WINDOWS)

    await page.click(SEARCH_BTN)
    await wait_eval(page, _WINDOWS, [opened, opened])
    toast = page.get_by_text(TOAST_TEXT)
    await toast.wait_for()

    if close == "escape":
        await page.keyboard.press("Escape")
    else:
        await toast.click()
    await wait_eval(page, SEARCH_OPEN, False)
    await wait_eval(page, _WINDOWS, [opened, 0])
    await toast.wait_for(state="detached")
    assert_no_page_errors(errors)
