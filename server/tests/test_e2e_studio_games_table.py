"""E2E: Studio Games table on data-table.js (Playwright/Chromium).

Headers are keyboard-reachable (Tab from the columns button lands on "#",
Enter sorts). Row keyboard nav + Enter-to-replay and click-to-replay are
covered elsewhere (test_e2e_table_row_nav.py, test_e2e_list_keyboard_nav.py,
test_e2e_studio_replay_guard.py).

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
GAMES = [
    ("1", ENGINES[0], ENGINES[1], "1-0"),
    ("2", ENGINES[1], ENGINES[0], "0-1"),
    ("3", ENGINES[0], ENGINES[1], "1/2-1/2"),
]
VIEWPORT = {"width": 1400, "height": 900}

ROW = ".studio-history-row"
HEAD = ".studio-history-head-tbl"
NUM_TH = f"{HEAD} th:has(> .th-label:text-is('#'))"

_ROW_NUMS = f"() => [...document.querySelectorAll('{ROW}')].map(tr => tr.dataset.id)"


@pytest.fixture
def server(tmp_path):
    seed_tournament(tmp_path, "games", ENGINES, GAMES)
    with run_uvicorn_subprocess(env_overrides=tournament_server_env(tmp_path, ENGINES)) as base:
        yield base


async def _open_games(page, server):
    await open_studio_tab(page, server, "history")
    await page.wait_for_function(f"() => document.querySelectorAll('{ROW}').length === {len(GAMES)}")


@pytest.mark.asyncio
async def test_header_tab_and_enter_sort(server, make_page):
    """Tab goes columns button -> "#" label; Enter sorts by it, ascending
    then descending."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_games(page, server)

    await page.focus(f"{HEAD} .dt-menu-btn")
    await page.keyboard.press("Tab")
    assert await page.evaluate("() => document.activeElement.textContent") == "#"

    await page.keyboard.press("Enter")
    assert await page.get_attribute(NUM_TH, "aria-sort") == "ascending"
    assert await page.evaluate(_ROW_NUMS) == ["1", "2", "3"]
    await page.keyboard.press("Enter")
    assert await page.get_attribute(NUM_TH, "aria-sort") == "descending"
    assert await page.evaluate(_ROW_NUMS) == ["3", "2", "1"]
    assert_no_page_errors(errors)

