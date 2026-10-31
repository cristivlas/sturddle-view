"""E2E: Arena menubar dropdowns follow the menu keyboard pattern
(menu-nav.js; Playwright/Chromium).

Opening focuses the first item, arrows move, Right/Escape open and close
the Tournament submenu, Escape closes the menu back to its button, Tab
closes it and moves on to the next tab stop. Also: opening the Standings
columns menu closes an open Arena menu.

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

ENGINES = ("engine-A", "engine-B")
VIEWPORT = {"width": 1400, "height": 900}

_FOCUSED_TEXT = "() => document.activeElement.textContent.trim()"
_OPEN_MENUS = "() => [...document.querySelectorAll('.tmb-menu.open, .tmb-dd-submenu.open')].length"


@pytest.fixture
def server(tmp_path):
    # One game so the workspace Standings table (and its columns button) shows.
    seed_tournament(tmp_path, "menus", list(ENGINES), [("1", *ENGINES, "1-0")])
    with run_uvicorn_subprocess(env_overrides=tournament_server_env(tmp_path, ENGINES)) as base:
        yield base


async def _press_expect(page, key, focused):
    await page.keyboard.press(key)
    await wait_eval(page, _FOCUSED_TEXT, focused)


@pytest.mark.asyncio
async def test_sort_menu_keys(server, make_page):
    """Enter opens Sort on its first item; arrows move; Tab closes it and
    lands where Tab from the Sort button itself would."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await goto_arena_tournaments(page, server)

    # Reference: the tab stop after the Sort button, menu closed.
    await page.focus(".tmb-sort-btn")
    await page.keyboard.press("Tab")
    await page.wait_for_function("() => !document.activeElement.matches('.tmb-sort-btn')")
    after_sort = await page.evaluate_handle("() => document.activeElement")

    await page.focus(".tmb-sort-btn")
    await _press_expect(page, "Enter", "Name")
    await _press_expect(page, "ArrowDown", "Status")
    await _press_expect(page, "End", "Started")
    await _press_expect(page, "Home", "Name")
    await page.keyboard.press("Tab")
    await page.wait_for_function("(el) => document.activeElement === el", arg=after_sort)
    await wait_eval(page, _OPEN_MENUS, 0)
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_window_menu_submenu_keys(server, make_page):
    """Right opens the Tournament submenu on its first item, Escape closes
    just the submenu, Escape again closes the menu back to its button."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await goto_arena_tournaments(page, server)
    await page.click(".tournament-row")
    await open_arena_workspace(page)

    await page.focus(".tmb-window-btn")
    await _press_expect(page, "Enter", "Tournament")
    await _press_expect(page, "ArrowRight", "Standings")
    await _press_expect(page, "ArrowDown", "Crosstable")
    await _press_expect(page, "Escape", "Tournament")
    await wait_eval(page, _OPEN_MENUS, 1)
    await _press_expect(page, "ArrowDown", "Snap")
    await _press_expect(page, "Escape", "Window")
    await wait_eval(page, _OPEN_MENUS, 0)
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_columns_button_closes_open_arena_menu(server, make_page):
    """Clicking the Standings columns button while an Arena menu is open
    opens the columns menu and closes the Arena one (one menu at a time)."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await goto_arena_tournaments(page, server)
    await page.click(".tournament-row")
    await open_arena_workspace(page)
    columns_btn = ".wb-standings-head-tbl .dt-menu-btn"
    await page.wait_for_selector(columns_btn)

    await page.click(".tmb-sort-btn")
    await wait_eval(page, _OPEN_MENUS, 1)
    # The Sort dropdown overlaps the button at this viewport, so dispatch the
    # click on it directly: the bug is that click's propagation.
    await page.dispatch_event(columns_btn, "click")
    await page.wait_for_selector(".dt-menu:popover-open")
    await wait_eval(page, _OPEN_MENUS, 0)
    assert_no_page_errors(errors)
