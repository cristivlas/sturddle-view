"""E2E: data-table.js traits on the Studio Standings table (Playwright/Chromium).

Columns menu (hide/show, reorder, fit, reset), layout persistence across a
reload, hidden sort columns dropping out of the row order, and keyboard
access (tab order + Enter to sort, arrow-key menu navigation).

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
    wait_perspective_ready,
    watch_page_errors,
)

WHITE_WIN, BLACK_WIN, DRAW = "1-0", "0-1", "1/2-1/2"

# Long names so the Engine column truncates at its default width (fit test).
ENGINE_A = "engine-with-a-deliberately-long-name-A"
ENGINE_B = "engine-with-a-deliberately-long-name-B"
ENGINES = (ENGINE_A, ENGINE_B)

# A: 3 wins, B: 1 win, 1 draw -> A leads on W and %; B leads on L.
GAMES = [
    ("1", ENGINE_A, ENGINE_B, WHITE_WIN),
    ("2", ENGINE_A, ENGINE_B, WHITE_WIN),
    ("3", ENGINE_B, ENGINE_A, BLACK_WIN),
    ("4", ENGINE_B, ENGINE_A, WHITE_WIN),
    ("5", ENGINE_A, ENGINE_B, DRAW),
]

DEFAULT_LABELS = ["Engine", "G", "W", "L", "D", "Pts", "%", "Elo", "Ordo"]
VIEWPORT = {"width": 1400, "height": 900}

HEAD = ".wb-standings-head-tbl"
MENU_BTN = f"{HEAD} .dt-menu-btn"
MENU = ".dt-menu"

_LABELS = f"() => [...document.querySelectorAll('{HEAD} th .th-label')].map(e => e.textContent)"
_ROW_NAMES = (
    "() => [...document.querySelectorAll('.wb-standings-body-tbl tbody td.wb-eng-name')]"
    ".map(e => e.textContent)"
)
_CELLS_PER_ROW = (
    "() => [...document.querySelectorAll('.wb-standings-body-tbl tbody tr')]"
    ".map(tr => tr.cells.length)"
)
# Rendered px width of each header column, keyed by label.
_HEAD_WIDTHS = (
    f"() => Object.fromEntries([...document.querySelectorAll('{HEAD} th')]"
    ".map(th => [th.querySelector('.th-label').textContent, th.getBoundingClientRect().width]))"
)
_NAME_TRUNCATED = (
    "() => { const td = document.querySelector('.wb-standings-body-tbl tbody td.wb-eng-name');"
    " return td.scrollWidth > td.clientWidth; }"
)


def _menu_item(key, cls):
    return f'{MENU} .dt-menu-col[data-key="{key}"] .{cls}'


def _th_at(label):
    """Header cell for `label`, by its position in the default layout."""
    return f"{HEAD} th:nth-child({DEFAULT_LABELS.index(label) + 1})"


# Label -> aria-sort for every shown header with an active sort arrow.
_SORTED_HEADERS = (
    f"() => Object.fromEntries([...document.querySelectorAll('{HEAD} th')]"
    ".filter(th => th.getAttribute('aria-sort') !== 'none')"
    ".map(th => [th.querySelector('.th-label').textContent, th.getAttribute('aria-sort')]))"
)


async def _open_standings(page, base):
    await open_studio_tab(page, base, "standings")
    await _wait_rows(page)


async def _wait_rows(page):
    await page.wait_for_function(
        "() => document.querySelectorAll('.wb-standings-body-tbl tbody tr').length === 2",
    )


async def _reload_standings(page):
    """Reload; the persisted perspective + Studio tab reopen Standings."""
    await page.reload()
    await wait_perspective_ready(page)
    await _wait_rows(page)


async def _open_menu(page):
    await page.click(MENU_BTN)
    await page.wait_for_selector(f"{MENU}:popover-open")


@pytest.fixture
def server(tmp_path):
    seed_tournament(tmp_path, "dt", list(ENGINES), GAMES)
    with run_uvicorn_subprocess(env_overrides=tournament_server_env(tmp_path, ENGINES)) as base:
        yield base


@pytest.mark.asyncio
async def test_hide_column_persists_and_engine_locked(server, make_page):
    """Hiding W drops its header and cells and survives a reload; the
    always-on Engine column can't be hidden."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_standings(page, server)
    await _open_menu(page)

    assert await page.is_disabled(_menu_item("name", "dt-menu-toggle"))
    await page.click(_menu_item("wins", "dt-menu-toggle"))

    expected = [lbl for lbl in DEFAULT_LABELS if lbl != "W"]
    assert await page.evaluate(_LABELS) == expected
    assert await page.evaluate(_CELLS_PER_ROW) == [len(expected)] * 2

    await _reload_standings(page)
    assert await page.evaluate(_LABELS) == expected
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_reorder_then_reset(server, make_page):
    """Moving G down swaps it past W; Reset restores the default layout."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_standings(page, server)
    await _open_menu(page)

    await page.click(f'{_menu_item("games", "dt-menu-move")}[data-dir="1"]')
    assert await page.evaluate(_LABELS) == ["Engine", "W", "G", *DEFAULT_LABELS[3:]]

    await page.click(_menu_item("draws", "dt-menu-toggle"))
    await page.click(f'{MENU} [data-action="reset"]')
    assert await page.evaluate(_LABELS) == DEFAULT_LABELS
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_reorder_skips_hidden_neighbor(server, make_page):
    """A shown column moves past hidden neighbors, so every move shows."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_standings(page, server)
    await _open_menu(page)

    await page.click(_menu_item("wins", "dt-menu-toggle"))
    await page.click(f'{_menu_item("games", "dt-menu-move")}[data-dir="1"]')
    assert await page.evaluate(_LABELS) == ["Engine", "L", "G", "D", "Pts", "%", "Elo", "Ordo"]
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_fit_columns_widens_content_and_persists(server, make_page):
    """Fit sizes columns by content: the truncated long-name Engine column
    grows, the one-digit G column shrinks, and the widths survive a reload."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_standings(page, server)
    assert await page.evaluate(_NAME_TRUNCATED), "precondition: Engine column truncates"
    before = await page.evaluate(_HEAD_WIDTHS)

    await _open_menu(page)
    await page.click(f'{MENU} [data-action="fit"]')
    after = await page.evaluate(_HEAD_WIDTHS)
    assert after["Engine"] > before["Engine"]
    assert after["G"] < before["G"]

    await _reload_standings(page)
    reloaded = await page.evaluate(_HEAD_WIDTHS)
    for label, width in after.items():
        assert reloaded[label] == pytest.approx(width, abs=1), label
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_hidden_sort_column_drops_out_of_order(server, make_page):
    """Sorting W ascending puts B first; hiding W reverts to the deeper
    default (% desc, A first) and moves the arrow to %; showing W again
    restores its sort and arrow."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_standings(page, server)

    w_label = f'{_th_at("W")} .th-label'
    await page.click(w_label)  # W opens descending
    await page.click(w_label)  # -> ascending
    assert await page.evaluate(_ROW_NAMES) == [ENGINE_B, ENGINE_A]

    await _open_menu(page)
    await page.click(_menu_item("wins", "dt-menu-toggle"))
    assert await page.evaluate(_ROW_NAMES) == [ENGINE_A, ENGINE_B]
    assert await page.evaluate(_SORTED_HEADERS) == {"%": "descending"}

    await page.click(_menu_item("wins", "dt-menu-toggle"))
    assert await page.evaluate(_ROW_NAMES) == [ENGINE_B, ENGINE_A]
    assert await page.evaluate(_SORTED_HEADERS) == {"W": "ascending"}
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_keyboard_tab_order_and_enter(server, make_page):
    """Tab goes menu button -> Engine label; Enter on a label sorts; Enter on
    the menu button opens the menu with focus inside, without sorting; Esc
    closes it and returns focus to the button."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_standings(page, server)
    engine_th = _th_at("Engine")

    await page.focus(MENU_BTN)
    await page.keyboard.press("Tab")
    assert await page.evaluate(
        "() => document.activeElement.classList.contains('th-label')"
        " && document.activeElement.textContent"
    ) == "Engine"

    await page.keyboard.press("Enter")
    assert await page.get_attribute(engine_th, "aria-sort") == "ascending"

    await page.focus(MENU_BTN)
    await page.keyboard.press("Enter")
    await page.wait_for_selector(f"{MENU}:popover-open")
    assert await page.get_attribute(engine_th, "aria-sort") == "ascending"
    assert await page.evaluate(f"() => document.querySelector('{MENU}').contains(document.activeElement)")

    await page.keyboard.press("Escape")
    await page.wait_for_selector(f"{MENU}:not(:popover-open)", state="attached")
    assert await page.evaluate("() => document.activeElement.classList.contains('dt-menu-btn')")
    assert_no_page_errors(errors)


_FOCUSED_FID = "() => document.activeElement.dataset.fid"


@pytest.mark.asyncio
async def test_columns_menu_arrow_keys(server, make_page):
    """Opening focuses the first enabled control (Engine's down arrow);
    Up/Down keep the column across rows, Left/Right step within a row,
    Home/End jump, Enter activates, Tab closes and moves on from the button
    to the next tab stop (the Engine label)."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_standings(page, server)
    await _open_menu(page)
    assert await page.evaluate(_FOCUSED_FID) == "name:1"

    for key, fid in (
        ("ArrowDown", "games:1"),
        ("ArrowLeft", "games:-1"),
        ("ArrowLeft", "games:toggle"),
    ):
        await page.keyboard.press(key)
        assert await page.evaluate(_FOCUSED_FID) == fid, key

    await page.keyboard.press("Enter")
    assert "G" not in await page.evaluate(_LABELS)
    assert await page.evaluate(_FOCUSED_FID) == "games:toggle"

    await page.keyboard.press("End")
    assert await page.evaluate(_FOCUSED_FID) == "reset"
    await page.keyboard.press("Home")
    assert await page.evaluate(_FOCUSED_FID) == "name:1"

    await page.keyboard.press("Tab")
    await page.wait_for_selector(f"{MENU}:not(:popover-open)", state="attached")
    assert await page.evaluate(
        "() => document.activeElement.classList.contains('th-label')"
        " && document.activeElement.textContent"
    ) == "Engine"
    assert_no_page_errors(errors)
