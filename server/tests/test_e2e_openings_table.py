"""E2E: Import dialog Openings table on data-table.js (Playwright/Chromium).

Header sort and the ribbon A-Z/Z-A buttons share one single-column sort; a
search filters the list; a pick made while filtered survives closing the
search (the full list re-renders with the pick still marked). Keyboard row
nav is covered in test_e2e_list_keyboard_nav.py.

Skipped if Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from .conftest import (  # noqa: E402
    assert_no_page_errors,
    e2e_env,
    run_uvicorn_subprocess,
    wait_eval,
    wait_eval_not,
    wait_perspective_ready,
    watch_page_errors,
)

VIEWPORT = {"width": 1400, "height": 900}
ROW = "tr.openings-list-item"
NAME_TH = ".openings-head-tbl th:nth-child(2)"
NEEDLE = "Sicilian"
ACTIVE = "is-active"

_ROW_NAMES = f"() => [...document.querySelectorAll('{ROW} .openings-list-name')].map(td => td.textContent)"
_SELECTED_NAME = f"() => document.querySelector('{ROW}.selected .openings-list-name')?.textContent ?? null"
_HAS_CLASS = "([sel, cls]) => document.querySelector(sel).classList.contains(cls)"


async def _search(page, text):
    """wa-input can't take fill(): set .value on the host and fire input."""
    await page.locator(".openings-search").evaluate(
        "(el, t) => { el.value = t; el.dispatchEvent(new Event('input', { bubbles: true, composed: true })); }",
        text,
    )


@pytest.fixture
def server(tmp_path):
    with run_uvicorn_subprocess(env_overrides=e2e_env(tmp_path)) as base:
        yield base


async def _open_openings(page, base):
    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)
    await page.click("#import-pos")
    await page.click('wa-tab[panel="openings"]')
    await page.wait_for_function(f"() => document.querySelectorAll('{ROW}').length > 1")


@pytest.mark.asyncio
async def test_header_and_ribbon_share_sort(server, make_page):
    """Sorting Name from the header lights the ribbon A-Z button; the ribbon
    Z-A button flips the header to descending."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_openings(page, server)

    await page.click(f"{NAME_TH} .th-label")
    await page.wait_for_selector(f'{NAME_TH}[aria-sort="ascending"]')
    await wait_eval(page, _HAS_CLASS, True, [".openings-sort-asc", ACTIVE])

    await page.click(".openings-sort-desc")
    await page.wait_for_selector(f'{NAME_TH}[aria-sort="descending"]')
    names = await page.evaluate(_ROW_NAMES)
    assert names[0].lower() >= names[-1].lower()
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_pick_survives_closing_search(server, make_page):
    """Filter, pick a match, close the search: the full list comes back with
    the same opening still selected."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_openings(page, server)
    total = len(await page.evaluate(_ROW_NAMES))

    await page.click(".openings-search-btn")
    await _search(page, NEEDLE)
    await page.wait_for_function(f"(total) => ({_ROW_NAMES})().length < total", arg=total)
    names = await page.evaluate(_ROW_NAMES)
    assert 0 < len(names) < total and all(NEEDLE.lower() in n.lower() for n in names)

    await page.locator(ROW).nth(1).click()
    await wait_eval_not(page, _SELECTED_NAME, None)
    picked = await page.evaluate(_SELECTED_NAME)
    assert picked and NEEDLE.lower() in picked.lower()

    await page.keyboard.press("Escape")
    await page.wait_for_function(f"() => document.querySelectorAll('{ROW}').length === {total}")
    await wait_eval(page, _SELECTED_NAME, picked)
    assert_no_page_errors(errors)
