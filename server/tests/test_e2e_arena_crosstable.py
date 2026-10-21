"""E2E: the Arena Standings window's Crosstable view (Playwright/Chromium).

Window > Tournament > Crosstable switches the Standings window to the grid:
engines ranked by Ordo, each cell the row engine's score vs the column
engine (W-D-L by color in its tooltip), blank on the diagonal; G, Pts and
Ordo are the only optional columns (G starts hidden, even under a saved
layout that predates it), and a saved column order can't move them ahead of
the engine columns. The title-bar toggle flips back to Standings,
and the menu checks the view the open window shows.

Skipped if Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from .conftest import (  # noqa: E402
    MENU_CHECKED,
    assert_no_page_errors,
    goto_arena_tournaments,
    open_arena_workspace,
    run_uvicorn_subprocess,
    seed_tournament,
    tournament_server_env,
    watch_page_errors,
)

A, B, C = "engine-A", "engine-B", "engine-C"
ENGINES = (A, B, C)
# A: 1.5/2 vs B, 1/1 vs C. B: 1/1 vs C. C loses everything (unrated, last).
GAMES = [
    ("1", A, B, "1-0"),
    ("2", B, A, "1/2-1/2"),
    ("3", A, C, "1-0"),
    ("4", C, B, "0-1"),
]
VIEWPORT = {"width": 1400, "height": 900}
TITLE = ".winbox.sturddle-wb-standings .wb-title"
XT_ROWS = ".xt-body-tbl tbody tr"
TOGGLE = ".wb-view-toggle-ctrl"
# Grid column index of Ordo (G hidden): name, one per engine, Pts, then Ordo.
ORDO_COL = 1 + len(ENGINES) + 1

# Each body row's cells' text, name cell first.
_GRID = f"""() => [...document.querySelectorAll('{XT_ROWS}')]
  .map((tr) => [...tr.cells].map((td) => td.textContent.trim()))"""
# Crosstable column layout (storage-keys.js TOURNAMENTS_CROSSTABLE_LAYOUT),
# seeded with Pts / Ordo ahead of the engine columns.
LAYOUT_KEY = "sturddle:tournaments:crosstableLayout"
SCRAMBLED_ORDER = '["pts", "ordo", "name", "r1", "r2", "r3"]'
_HEAD_LABELS = "() => [...document.querySelectorAll('.xt-head-tbl th .th-label')].map((e) => e.textContent)"
G_TOGGLE = '.dt-menu:popover-open .dt-menu-col[data-key="games"] .dt-menu-toggle'
# Open columns menu's toggles: label -> enabled.
_MENU_TOGGLES = """() => Object.fromEntries([...document.querySelectorAll('.dt-menu:popover-open .dt-menu-toggle')]
  .map((b) => [b.textContent.trim(), !b.disabled]))"""


@pytest.fixture
def server(tmp_path):
    seed_tournament(tmp_path, "xt", list(ENGINES), GAMES)
    with run_uvicorn_subprocess(env_overrides=tournament_server_env(tmp_path, ENGINES)) as base:
        yield base


async def _open_workspace(make_page, server, init_script=None):
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    if init_script:
        await page.add_init_script(init_script)
    await goto_arena_tournaments(page, server)
    await page.click(".tournament-row")
    await open_arena_workspace(page)
    return page, errors


async def _menu_crosstable(page):
    """Window > Tournament > Crosstable, by keyboard (menu-nav.js)."""
    await page.focus(".tmb-window-btn")
    await page.keyboard.press("Enter")
    await page.keyboard.press("ArrowRight")
    await page.keyboard.press("ArrowDown")
    await page.keyboard.press("Enter")


async def _checked(page, sel):
    return await page.locator(sel).evaluate(f"(el) => el.classList.contains('{MENU_CHECKED}')")


@pytest.mark.asyncio
async def test_menu_shows_crosstable_grid(server, make_page):
    """The menu switches the window to the ranked grid; a cell's tooltip splits
    W-D-L by color; only G / Pts / Ordo can be hidden, and G starts hidden."""
    page, errors = await _open_workspace(make_page, server)
    await _menu_crosstable(page)
    await page.wait_for_selector(XT_ROWS)

    assert await page.locator(TITLE).text_content() == "Crosstable"
    grid = await page.evaluate(_GRID)
    # Name, the three score columns, Pts (Ordo values aside from C's are fit output).
    assert [row[:ORDO_COL] for row in grid] == [
        [f"1{A}", "", "1.5", "1", "2.5"],
        [f"2{B}", "0.5", "", "1", "1.5"],
        [f"3{C}", "0", "0", "", "0"],
    ]
    assert grid[2][ORDO_COL] == "--"
    tip = await page.locator(f"{XT_ROWS} >> nth=0 >> td >> nth=2").get_attribute("title")
    assert tip == f"{A} vs {B}: +1 =1 -0\nWhite +1 =0 -0\nBlack +0 =1 -0"

    await page.click(".xt-head-tbl .dt-menu-btn")
    await page.wait_for_selector(".dt-menu:popover-open")
    assert await page.evaluate(_MENU_TOGGLES) == {
        "Engine": False, "1": False, "2": False, "3": False, "G": True, "Pts": True, "Ordo": True,
    }
    # Showing G adds each engine's game count after the score columns.
    await page.click(G_TOGGLE)
    assert [row[len(ENGINES) + 1] for row in await page.evaluate(_GRID)] == ["3", "3", "2"]
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_saved_order_ignored_without_reorder(server, make_page):
    """A non-reorderable table keeps its declared column order over a saved
    one: engine columns come and go with the field, and a saved order would
    otherwise leave new ones after Pts / Ordo. G, unknown to that saved
    layout, still starts hidden."""
    page, errors = await _open_workspace(
        make_page, server,
        f"localStorage.setItem('{LAYOUT_KEY}', JSON.stringify({{ order: {SCRAMBLED_ORDER} }}))",
    )
    await _menu_crosstable(page)
    await page.wait_for_selector(XT_ROWS)

    assert await page.evaluate(_HEAD_LABELS) == ["Engine", "1", "2", "3", "Pts", "Ordo"]
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_toggle_flips_view_and_menu_checks_it(server, make_page):
    """The title-bar toggle flips back to Standings; the reopened Window menu
    checks Standings, not Crosstable."""
    page, errors = await _open_workspace(make_page, server)
    await _menu_crosstable(page)
    await page.wait_for_selector(XT_ROWS)

    await page.click(TOGGLE)
    assert await page.locator(TITLE).text_content() == "Standings"
    assert await page.locator(".wb-standings").is_visible()
    assert not await page.locator(".wb-crosstable").is_visible()

    await page.click(".tmb-window-btn")
    assert await _checked(page, ".tmb-sys-standings")
    assert not await _checked(page, ".tmb-sys-crosstable")
    assert_no_page_errors(errors)
