"""E2E: data-table.js rowNav trait -- every factory table body is one Tab
stop, arrows move the current row, and the current row shows a ring while the
body has keyboard focus (Playwright/Chromium).

Selection-backed tables (Engines, Tourneys) move their selection; the others
(Standings, Head-to-Head, Games) move a highlight-only cursor. Games opens its
cursor row on Enter.

Skipped if Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import sys

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import (  # noqa: E402
    REGISTRY_FILE,
    assert_no_page_errors,
    e2e_env,
    open_studio_tab,
    run_uvicorn_subprocess,
    seed_tournament,
    tournament_server_env,
    wait_perspective_ready,
    watch_page_errors,
)

VIEWPORT = {"width": 1400, "height": 900}
ENGINES = ("engine-A", "engine-B")
GAMES = [
    ("1", ENGINES[0], ENGINES[1], "1-0"),
    ("2", ENGINES[1], ENGINES[0], "0-1"),
    ("3", ENGINES[0], ENGINES[1], "1/2-1/2"),
]
CURRENT = 'tr[aria-selected="true"]'

# Focused element's classes, and the current row's data-id + ring style.
_STATE = f"""(body) => {{
  const cur = document.querySelector(`${{body}} {CURRENT}`);
  return {{
    bodyFocused: document.activeElement === document.querySelector(body),
    current: cur ? cur.dataset.id : null,
    ring: cur ? getComputedStyle(cur).outlineStyle : null,
  }};
}}"""


async def _state(page, body):
    return await page.evaluate(_STATE, body)


@pytest.fixture
def tourney_server(tmp_path):
    seed_tournament(tmp_path, "nav", ENGINES, GAMES)
    with run_uvicorn_subprocess(env_overrides=tournament_server_env(tmp_path, ENGINES)) as base:
        yield base


@pytest.mark.asyncio
async def test_engines_selection_row_ring(tmp_path, make_page):
    """Tab from the last header lands on the Engines body; the selected row
    rings, and ArrowDown moves the selection (and the ring)."""
    reg = EngineRegistry(path=tmp_path / REGISTRY_FILE)
    for n in ("eng-one", "eng-two", "eng-three"):
        reg.add(name=n, path=sys.executable)
    with run_uvicorn_subprocess(env_overrides=e2e_env(tmp_path)) as base:
        _ctx, page = await make_page(viewport=VIEWPORT)
        errors = watch_page_errors(page)
        await page.goto(base + "/")
        await page.wait_for_selector("#play-perspective")
        await wait_perspective_ready(page)
        await page.evaluate(
            "window.dispatchEvent(new CustomEvent('sturddle:open-settings', { detail: { tab: 'engines' } }))"
        )
        await page.locator(".engines-list-item").first.wait_for()
        body = ".engines-body-tbl"
        await page.focus(".engines-head-tbl th:last-child .th-label")
        await page.keyboard.press("Tab")
        first = await _state(page, body)
        assert first["bodyFocused"] and first["current"] and first["ring"] == "solid", first

        await page.keyboard.press("ArrowDown")
        moved = await _state(page, body)
        assert moved["current"] not in (None, first["current"]) and moved["ring"] == "solid", moved
        assert await page.evaluate(
            f"() => document.querySelector('{body} {CURRENT}').classList.contains('focused')"
        )

        # Hover a non-current row, then arrow: its hover tint goes (one
        # highlight at a time) and comes back once the mouse moves.
        await page.keyboard.press("Home")
        last = page.locator(f"{body} tr.engines-list-item").last
        box = await last.bounding_box()
        await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        tinted = await last.evaluate("(el) => getComputedStyle(el).backgroundColor")
        await page.keyboard.press("ArrowDown")
        assert await last.evaluate("(el) => getComputedStyle(el).backgroundColor") != tinted
        await page.mouse.move(box["x"] + box["width"] / 2 + 1, box["y"] + box["height"] / 2)
        assert await last.evaluate("(el) => getComputedStyle(el).backgroundColor") == tinted
        assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_standings_cursor_row(tourney_server, make_page):
    """Standings has no selection: Tab lands on the body and puts a ringed
    cursor on the first row; ArrowDown moves it to the next; a click moves it
    back."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await open_studio_tab(page, tourney_server, "standings")
    await page.wait_for_selector(".wb-standings-body-tbl tbody tr")
    body = ".wb-standings-body-tbl"
    await page.focus(".wb-standings-head-tbl th:last-child .th-label")
    await page.keyboard.press("Tab")
    one = await _state(page, body)
    assert one["bodyFocused"], one
    await page.keyboard.press("ArrowDown")
    two = await _state(page, body)
    assert one["ring"] == two["ring"] == "solid"
    assert None not in (one["current"], two["current"]) and one["current"] != two["current"]

    # A click moves the cursor too (Enter after a click acts on that row).
    await page.click(f"{body} tbody tr[data-id='{one['current']}']")
    assert (await _state(page, body))["current"] == one["current"]
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_h2h_reachable_with_cursor(tourney_server, make_page):
    """Head-to-Head was unreachable by Tab; now Tab from its tab enters the
    body with a ringed cursor on the first stat row (group rows are skipped),
    and arrows move it."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await open_studio_tab(page, tourney_server, "h2h")
    await page.wait_for_selector(".h2h-tbl tbody tr")
    body = ".h2h-body-tbl"
    await page.focus('.studio-bottom-right wa-tab[panel="h2h"]')
    await page.keyboard.press("Tab")
    first = await _state(page, body)
    assert first["bodyFocused"] and first["current"] and first["ring"] == "solid", first

    await page.keyboard.press("ArrowDown")
    cur = await _state(page, body)
    assert cur["current"] not in (None, first["current"]) and cur["ring"] == "solid", cur
    assert not await page.evaluate(f"() => document.querySelector('{body} {CURRENT}').classList.contains('h2h-group')")
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_tourneys_selection_row_ring(tourney_server, make_page):
    """Tourneys: Tab from the last header lands on the body; the selected
    tournament's row rings."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await open_studio_tab(page, tourney_server, "tourneys")
    await page.wait_for_selector(".studio-tourney-row")
    body = ".studio-tourney-body-tbl"
    # Completed (last) isn't sortable, so start from the last focusable label.
    await page.locator(".studio-tourney-head-tbl .th-label[tabindex]").last.focus()
    await page.keyboard.press("Tab")
    st = await _state(page, body)
    assert st["bodyFocused"] and st["current"] and st["ring"] == "solid", st
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_games_cursor_enter_replays(tourney_server, make_page):
    """Games: one Tab stop (the body) with the cursor on game 1; ArrowDown
    moves it to game 2 and Enter replays that game."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await open_studio_tab(page, tourney_server, "history")
    await page.wait_for_function(f"() => document.querySelectorAll('.studio-history-row').length === {len(GAMES)}")
    body = ".studio-history-body-tbl"
    await page.focus(".studio-history-head-tbl th:last-child .th-label")
    await page.keyboard.press("Tab")
    assert (await _state(page, body))["bodyFocused"]

    assert (await _state(page, body))["current"] == "1"
    await page.keyboard.press("ArrowDown")
    assert (await _state(page, body))["current"] == "2"
    async with page.expect_request(lambda r: r.url.endswith("/games/2/pgn")):
        await page.keyboard.press("Enter")
    assert_no_page_errors(errors)
