"""E2E: engine rows sorted by name break ties by proxy id, in Studio's
Engines tab and Arena's Engine Instances window (Playwright/Chromium).

Two proxies share an engine name; the server snapshot lists them by plain
string order (proxy-10 before proxy-2), so only a numeric proxy-id tiebreak
yields proxy-2 first. Z-A mirrors A-Z exactly.

Skipped if Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from .conftest import (  # noqa: E402
    LIVE_ENGINE_B,
    LIVE_PROXY_ID,
    assert_no_page_errors,
    open_arena_engines_window,
    open_studio_tab,
    post_proxy,
    running_tournament,
    wait_eval,
    watch_page_errors,
)

# Same engine name; plain string order puts DUP_10 first.
DUP_2 = "proxy-2"
DUP_10 = "proxy-10"
# LIVE_PROXY_ID runs LIVE_ENGINE_A, which sorts before LIVE_ENGINE_B.
EXPECTED_ASC = [LIVE_PROXY_ID, DUP_2, DUP_10]
EXPECTED_DESC = EXPECTED_ASC[::-1]

STUDIO_LIST = ".studio-pane-engines .wb-sched-list"
STUDIO_ENGINES_TAB = '.studio-bottom-left wa-tab[panel="engines"]'
ARENA_LIST = ".wb-engines .wb-sched-list"
ARENA_ASC_BTN = ".wb-sort-asc-ctrl"
ARENA_DESC_BTN = ".wb-sort-desc-ctrl"

_ROWS_ARE = "([sel, n]) => document.querySelectorAll(`${sel} li`).length === n"
_ROW_PIDS = """(sel) => [...document.querySelectorAll(`${sel} li`)]
  .map((li) => li.querySelector('[data-attach-key]').dataset.attachKey)"""


async def _add_dups(base, secret):
    for pid in (DUP_2, DUP_10):
        await post_proxy(base, secret, [], proxy_id=pid, engine_name=LIVE_ENGINE_B)


@pytest.mark.asyncio
async def test_studio_engines_sort_ties_by_proxy_id(tmp_path, monkeypatch, make_page):
    async with running_tournament(tmp_path, monkeypatch) as (_app, base, _t, secret):
        await _add_dups(base, secret)
        _ctx, page = await make_page()
        errors = watch_page_errors(page)
        await open_studio_tab(page, base, "engines", side="left")
        await page.wait_for_function(_ROWS_ARE, arg=[STUDIO_LIST, len(EXPECTED_ASC)])

        await page.dblclick(STUDIO_ENGINES_TAB)
        await wait_eval(page, _ROW_PIDS, EXPECTED_ASC, STUDIO_LIST)
        await page.dblclick(STUDIO_ENGINES_TAB)
        await wait_eval(page, _ROW_PIDS, EXPECTED_DESC, STUDIO_LIST)
        assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_arena_engines_sort_ties_by_proxy_id(tmp_path, monkeypatch, make_page):
    async with running_tournament(tmp_path, monkeypatch) as (_app, base, _t, secret):
        await _add_dups(base, secret)
        _ctx, page = await make_page()
        errors = watch_page_errors(page)
        await open_arena_engines_window(page, base)
        await page.wait_for_function(_ROWS_ARE, arg=[ARENA_LIST, len(EXPECTED_ASC)])

        await page.click(ARENA_ASC_BTN)
        await wait_eval(page, _ROW_PIDS, EXPECTED_ASC, ARENA_LIST)
        await page.click(ARENA_DESC_BTN)
        await wait_eval(page, _ROW_PIDS, EXPECTED_DESC, ARENA_LIST)
        assert_no_page_errors(errors)
