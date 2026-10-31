"""E2E: dragging a maximized WinBox restores it under the pointer
(Playwright/Chromium).

WinBox on its own restores a dragged maximized window at the left, centre or
right of the screen (by which third the pointer is in), ignoring where the
title bar was grabbed, so a narrow window could land away from the cursor.
The app keeps the pointer at the same relative spot on the title bar, and
the bar keeps WinBox's move cursor while maximized.

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
    wait_winbox_settled,
    watch_page_errors,
)

ENGINES = ("engine-A", "engine-B")
VIEWPORT = {"width": 1400, "height": 900}
# Restored width to shrink the window to first (WinBox's minimum): well
# under a third of the maximized area -- the case where WinBox's
# left/centre/right placement misses the pointer.
NARROW_PX = 320
WIN = ".winbox.sturddle-wb-standings"
# Pointer travel per drag step, and steps taken.
STEP_PX = 6
STEPS = 3
# Grab fractions across the maximized drag area: just right of one third
# (WinBox's placement misses the pointer) and near the right end (a
# whole-window fraction would land on the restored window's controls).
GRAB_FRACS = [0.34, 0.9]
# Relative title-bar spot drift allowed after the drag (px).
TOL_PX = 2


@pytest.fixture
def server(tmp_path):
    seed_tournament(tmp_path, "drag", ENGINES, [("1", *ENGINES, "1-0")])
    with run_uvicorn_subprocess(env_overrides=tournament_server_env(tmp_path, ENGINES)) as base:
        yield base


MOVE_CURSOR = "move"


async def _cursor_at(page, x, y):
    """Cursor the element under (x, y) shows -- what the user sees."""
    return await page.evaluate("([x, y]) => getComputedStyle(document.elementFromPoint(x, y)).cursor", [x, y])


async def _rect(page, sel):
    return await page.locator(sel).evaluate(
        "(el) => { const r = el.getBoundingClientRect(); return { l: r.left, t: r.top, w: r.width, h: r.height }; }"
    )


# The title bar's drag span: header minus its right-floated controls.
_SPAN = (
    "(sel) => { const w = document.querySelector(sel);"
    " const h = w.querySelector('.wb-header').getBoundingClientRect();"
    " const c = w.querySelector('.wb-control').getBoundingClientRect();"
    " return { l: h.left, t: h.top, w: c.left - h.left, h: h.height }; }"
)
# Element under a point: inside the window's drag area (not its controls)?
_ON_DRAG_AREA = (
    "([x, y, sel]) => !!document.elementFromPoint(x, y)?.closest(`${sel} .wb-drag`)"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("grab_frac", GRAB_FRACS)
async def test_drag_from_maximized_keeps_pointer_on_title_bar(server, make_page, grab_frac):
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await goto_arena_tournaments(page, server)
    await page.click(".tournament-row")
    await open_arena_workspace(page)

    # Shrink the restored window by dragging its right edge in.
    edge = await _rect(page, f"{WIN} .wb-e")
    win = await _rect(page, WIN)
    ex, ey = edge["l"] + edge["w"] / 2, edge["t"] + edge["h"] / 2
    await page.mouse.move(ex, ey)
    await page.mouse.down()
    await page.mouse.move(ex - (win["w"] - NARROW_PX), ey, steps=STEPS)
    await page.mouse.up()
    await page.wait_for_function(
        "([sel, w, tol]) => Math.abs(document.querySelector(sel).getBoundingClientRect().width - w) <= tol",
        arg=[WIN, NARROW_PX, TOL_PX],
    )

    await page.click(f"{WIN} .wb-max")
    await page.wait_for_selector(f"{WIN}.max")
    # WinBox animates maximize: let it settle before grabbing the title bar.
    await wait_winbox_settled(page, WIN, TOL_PX)
    bar = await page.evaluate(_SPAN, WIN)
    x0 = bar["l"] + bar["w"] * grab_frac
    y0 = bar["t"] + bar["h"] / 2
    # A maximized title bar is still a drag handle: WinBox's move cursor.
    assert await _cursor_at(page, x0, y0) == MOVE_CURSOR

    await page.mouse.move(x0, y0)
    await page.mouse.down()
    for i in range(1, STEPS + 1):
        await page.mouse.move(x0 + i * STEP_PX, y0 + i * STEP_PX)
    x1, y1 = x0 + STEPS * STEP_PX, y0 + STEPS * STEP_PX

    await page.wait_for_selector(f"{WIN}:not(.max)")
    after = await page.evaluate(_SPAN, WIN)
    assert await _cursor_at(page, x1, y1) == MOVE_CURSOR
    await page.mouse.up()
    assert after["l"] <= x1 <= after["l"] + after["w"], (x1, after)
    assert after["t"] <= y1 <= after["t"] + after["h"], (y1, after)
    await wait_eval(page, _ON_DRAG_AREA, True, [x1, y1, WIN])  # pointer stays on the drag area
    # Same relative spot on the (now narrower) drag area.
    assert (x1 - after["l"]) / after["w"] == pytest.approx(grab_frac, abs=TOL_PX / after["w"])
    assert_no_page_errors(errors)
