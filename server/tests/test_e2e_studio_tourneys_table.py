"""E2E: Studio Tourneys table on data-table.js (Playwright/Chromium).

Multi-select marks survive every body rebuild the table does on its own --
a header sort and a columns-menu change -- not just data renders.

Skipped if Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from .conftest import (  # noqa: E402
    assert_no_page_errors,
    open_studio_tab,
    run_uvicorn_subprocess,
    seed_tournament,
    tournament_server_env,
    watch_page_errors,
)

ENGINES = ("engine-A", "engine-B")
NAMES = ("t-alpha", "t-bravo", "t-charlie")
VIEWPORT = {"width": 1400, "height": 900}

ROW = ".studio-tourney-row"
HEAD = ".studio-tourney-head-tbl"
# Scoped: Studio has several factory tables, each with its own menu.
MENU = ".studio-tourney-table-wrap .dt-menu"

# Names of the rows multi-select marks (the anchor carries .selected instead).
_MARKED = (
    f"() => [...document.querySelectorAll('{ROW}.marked .studio-tourney-name')]"
    ".map(td => td.textContent).sort()"
)


@pytest.fixture
def server(tmp_path):
    for name in NAMES:
        seed_tournament(tmp_path, name, ENGINES, [])
    with run_uvicorn_subprocess(env_overrides=tournament_server_env(tmp_path, ENGINES)) as base:
        yield base


def _row(name):
    return f'{ROW}:has(.studio-tourney-name:text-is("{name}"))'


async def _mark_two(page, server):
    """Open Tourneys; plain-click alpha, Ctrl-click bravo -> bravo marked."""
    await open_studio_tab(page, server, "tourneys")
    await page.wait_for_function(f"() => document.querySelectorAll('{ROW}').length === {len(NAMES)}")
    await page.click(_row(NAMES[0]))
    await page.click(_row(NAMES[1]), modifiers=["Control"])
    assert await page.evaluate(_MARKED) == [NAMES[1]]


@pytest.mark.asyncio
async def test_marks_survive_header_sort(server, make_page):
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _mark_two(page, server)

    await page.click(f'{HEAD} th .th-label:text-is("Name")')
    assert await page.evaluate(_MARKED) == [NAMES[1]]
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_marks_survive_columns_menu_change(server, make_page):
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _mark_two(page, server)

    await page.click(f"{HEAD} .dt-menu-btn")
    await page.wait_for_selector(f"{MENU}:popover-open")
    await page.click(f'{MENU} .dt-menu-col[data-key="created"] .dt-menu-toggle')
    assert await page.evaluate(_MARKED) == [NAMES[1]]
    assert_no_page_errors(errors)
