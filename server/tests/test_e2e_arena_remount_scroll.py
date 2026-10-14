"""E2E: remounting Arena opens the Engines and Live Games windows' lists at
the top (Playwright/Chromium).

A freshly built window body is empty, so it reads as "scrolled to the
bottom"; the live-list follow-the-tail logic must not take that as the user
pinning the list to the bottom.

Skipped if Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from .conftest import (  # noqa: E402
    LIVE_ENGINE_A,
    LIVE_ENGINE_B,
    assert_no_page_errors,
    open_arena_engines_window,
    post_proxy,
    running_tournament,
    wait_perspective_ready,
    watch_page_errors,
)

VIEWPORT = {"width": 1400, "height": 900}
# White first moves, one per pair: distinct so every pair confirms on its own.
FIRST_MOVES = (
    "e2e4", "d2d4", "c2c4", "g1f3", "b1c3", "f2f4", "b2b3",
    "g2g3", "a2a3", "h2h3", "e2e3", "d2d3", "c2c3", "b2b4",
)
ENGINES_LIST = ".wb-engines .wb-sched-list"
GAMES_LIST = ".wb-schedule .wb-sched-list"
_ROWS_ARE = "([sel, n]) => document.querySelectorAll(`${sel} li`).length === n"
# The list's scroller (its window body's parent): scrollTop, and whether the
# content overflows at all (else "at the top" proves nothing).
_SCROLL = """(sel) => {
  const s = document.querySelector(sel).parentElement.parentElement;
  return { top: s.scrollTop, overflows: s.scrollHeight > s.clientHeight };
}"""
_PERSPECTIVE_SETTLED = (
    "(id) => !!document.querySelector(id)"
    " && !document.querySelector('#perspective-root')?.classList.contains('is-pending')"
)


async def _start_pairs(base, secret):
    """One confirmed white/black pair per FIRST_MOVES entry."""
    async with AsyncClient(base_url=base) as http:
        for i, mv in enumerate(FIRST_MOVES):
            white, black = f"pw-{i}", f"pb-{i}"
            await post_proxy(base, secret, [], proxy_id=white, engine_name=LIVE_ENGINE_A, client=http)
            await post_proxy(base, secret, [], proxy_id=black, engine_name=LIVE_ENGINE_B, client=http)
            await post_proxy(base, secret, ["position startpos", f"bestmove {mv}"], proxy_id=white, client=http)
            await post_proxy(base, secret, [f"position startpos moves {mv}"], proxy_id=black, client=http)


@pytest.mark.asyncio
async def test_remount_opens_live_lists_at_top(tmp_path, monkeypatch, make_page):
    async with running_tournament(tmp_path, monkeypatch) as (_app, base, _t, secret):
        _ctx, page = await make_page(viewport=VIEWPORT)
        errors = watch_page_errors(page)
        await open_arena_engines_window(page, base)
        await _start_pairs(base, secret)
        await page.wait_for_function(_ROWS_ARE, arg=[GAMES_LIST, len(FIRST_MOVES)])

        await page.click('button[data-perspective="play"]')
        await page.wait_for_function(_PERSPECTIVE_SETTLED, arg="#play-perspective")
        await page.click('button[data-perspective="engines"]')
        await wait_perspective_ready(page)
        await page.wait_for_function(_ROWS_ARE, arg=[GAMES_LIST, len(FIRST_MOVES)])
        await page.wait_for_selector(f"{ENGINES_LIST} li")

        for sel in (ENGINES_LIST, GAMES_LIST):
            state = await page.evaluate(_SCROLL, sel)
            assert state["overflows"], f"precondition: {sel} overflows its window"
            assert state["top"] == 0, (sel, state)
        assert_no_page_errors(errors)
