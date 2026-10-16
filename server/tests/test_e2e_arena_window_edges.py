"""E2E: Arena windows line up with the tournament list's border and with each
other, across device pixel ratios incl. below 1 (Playwright/Chromium).

Windows draw their outline outside their box; the workspace area is inset by
that outline so it lands on the list's side borders. Rounding at fractional
DPR is where a 1px misfit shows, so the side check compares rendered pixels:
on a scanline through a list row and one through the Clean layout's windows,
the first line after the ribbon (left) / the empty gutter (right) must occupy
the same device-pixel columns. In Clean, stacked rows touch (system rows, and
the live-board row over them), and the bottom row's outline lands on the
list's bottom border.

Skipped if Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import base64

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from .conftest import (  # noqa: E402
    assert_no_page_errors,
    goto_arena_tournaments,
    open_arena_engines_window,
    open_arena_workspace,
    running_tournament,
    run_uvicorn_subprocess,
    seed_tournament,
    tournament_server_env,
    watch_page_errors,
)

ENGINES = ("engine-A", "engine-B")
VIEWPORT = {"width": 1400, "height": 900}
DPRS = (0.75, 0.8, 0.9, 1, 1.25, 1.5)
# Clean (tidy) layout: Engine Instances top-left, Standings top-right.
TIDY_WINDOWS = 4
SUBPIXEL = 0.01
LEFT_WIN = ".winbox.sturddle-wb-arena:has(.wb-engines)"
RIGHT_WIN = ".winbox.sturddle-wb-arena.sturddle-wb-standings"

# Clean applied and every window settled: WinBox animates moves/resizes, so
# each window's rendered box must have reached its inline (target) geometry.
_TIDY_DONE = f"""() => {{
  const wins = [...document.querySelectorAll('.winbox.sturddle-wb-arena.no-shadow')];
  const near = (a, b) => Math.abs(a - b) < {SUBPIXEL};
  return wins.length === {TIDY_WINDOWS} && wins.every((w) => {{
    const r = w.getBoundingClientRect();
    return near(r.top, parseFloat(w.style.top)) && near(r.height, parseFloat(w.style.height));
  }});
}}"""
# Device px to back off from the ribbon's right edge: clear of it, on its fill.
RIBBON_INSET_DPX = 3
_RIBBON_RIGHT = "() => document.querySelector('.tournaments-ribbon').getBoundingClientRect().right"
# CSS y of the middle of an element's box.
_MID_Y = "(sel) => { const r = document.querySelector(sel).getBoundingClientRect(); return (r.top + r.bottom) / 2; }"
_BODY_MID_Y = (
    "(sel) => { const r = document.querySelector(sel).querySelector('.wb-body').getBoundingClientRect();"
    " return (r.top + r.bottom) / 2; }"
)
# Decode the screenshot in the page; for each device-px scanline, return the
# first line after the edge background on each side: [start, end) columns,
# measured inward from the scan start. Scans start on the edge background:
# just inside the ribbon's right edge (clear of its buttons) on the left,
# 1 column in from the side over the empty gutter on the right.
_EDGE_RUNS = """async ([b64, rows, leftX0]) => {
  const img = new Image();
  img.src = 'data:image/png;base64,' + b64;
  await img.decode();
  const c = document.createElement('canvas');
  c.width = img.width; c.height = img.height;
  const g = c.getContext('2d');
  g.drawImage(img, 0, 0);
  const px = (x, y) => Array.from(g.getImageData(x, y, 1, 1).data).join(',');
  const firstRun = (y, step) => {
    const x0 = step > 0 ? leftX0 : img.width - 2;
    const bg = px(x0, y);
    let x = x0;
    while (px(x, y) === bg) x += step;
    const line = px(x, y);
    const start = Math.abs(x - x0);
    while (px(x, y) === line) x += step;
    return [start, Math.abs(x - x0)];
  };
  return rows.map((y) => ({ left: firstRun(y, 1), right: firstRun(y, -1) }));
}"""


@pytest.fixture
def server(tmp_path):
    seed_tournament(tmp_path, "edges", list(ENGINES), [("1", *ENGINES, "1-0")])
    with run_uvicorn_subprocess(env_overrides=tournament_server_env(tmp_path, ENGINES)) as base:
        yield base


@pytest.mark.asyncio
@pytest.mark.parametrize("dpr", DPRS)
async def test_window_edges_match_list_border(server, make_page, dpr):
    _ctx, page = await make_page(viewport=VIEWPORT, device_scale_factor=dpr)
    errors = watch_page_errors(page)
    await goto_arena_tournaments(page, server)
    await page.click(".tournament-row")
    await open_arena_workspace(page)
    await page.click(".tmb-window-btn")
    await page.click(".tmb-tidy")
    await page.wait_for_function(_TIDY_DONE)
    # Move the pointer off the list so no row shows its hover tint.
    await page.mouse.move(VIEWPORT["width"] / 2, 0)

    list_y = await page.evaluate(_MID_Y, ".tournament-row")
    left_y = await page.evaluate(_BODY_MID_Y, LEFT_WIN)
    right_y = await page.evaluate(_BODY_MID_Y, RIGHT_WIN)
    shot = await page.screenshot()
    rows = [round(y * dpr) for y in (list_y, left_y, right_y)]
    left_x0 = int(await page.evaluate(_RIBBON_RIGHT) * dpr) - RIBBON_INSET_DPX
    lst, left_win, right_win = await page.evaluate(
        _EDGE_RUNS, [base64.b64encode(shot).decode(), rows, left_x0],
    )
    assert left_win["left"] == lst["left"], (dpr, lst, left_win)
    assert right_win["right"] == lst["right"], (dpr, lst, right_win)
    assert_no_page_errors(errors)


# Each Clean-layout window's target CSS-px [top, bottom], keyed by its body's
# class, from WinBox's inline style: the bounding box animates there, so a
# mid-transition read would be stale.
_SPANS = """() => {
  const spans = {};
  for (const w of document.querySelectorAll('.winbox.sturddle-wb-arena, .winbox.sturddle-wb-live')) {
    const top = parseFloat(w.style.top);
    spans[w.querySelector('.wb-body > div').className.split(' ')[0]] = [top, top + parseFloat(w.style.height)];
  }
  return spans;
}"""
_LIST_BOTTOM_AND_OUTLINE = """() => [
  document.querySelector('.tournaments-list').getBoundingClientRect().bottom,
  parseFloat(getComputedStyle(document.querySelector('.winbox.sturddle-wb-arena')).outlineWidth),
]"""
_BOARD_TIDY_DONE = f"""() => document.querySelectorAll('.winbox.sturddle-wb-arena.no-shadow').length === {TIDY_WINDOWS}
  && !!document.querySelector('.winbox.sturddle-wb-live.no-shadow')"""


async def _tidy(page, done):
    await page.click(".tmb-window-btn")
    await page.click(".tmb-tidy")
    await page.wait_for_function(done)


@pytest.mark.asyncio
@pytest.mark.parametrize("dpr", DPRS)
async def test_system_window_rows_touch(server, make_page, dpr):
    """Clean layout: each upper window's bottom is the lower one's top."""
    _ctx, page = await make_page(viewport=VIEWPORT, device_scale_factor=dpr)
    errors = watch_page_errors(page)
    await goto_arena_tournaments(page, server)
    await page.click(".tournament-row")
    await open_arena_workspace(page)
    await _tidy(page, _TIDY_DONE)

    s = await page.evaluate(_SPANS)
    assert s["wb-engines"][1] == s["wb-schedule"][0], (dpr, s)
    assert s["wb-standings-view"][1] == s["wb-eventlog"][0], (dpr, s)
    # The bottom row's outline lands on the list's bottom border.
    list_bottom, outline = await page.evaluate(_LIST_BOTTOM_AND_OUTLINE)
    # WinBox keeps sizes to 3 decimals: compare well below a device pixel.
    assert s["wb-schedule"][1] + outline == pytest.approx(list_bottom, abs=SUBPIXEL), (dpr, s, list_bottom)
    assert s["wb-eventlog"][1] + outline == pytest.approx(list_bottom, abs=SUBPIXEL), (dpr, s, list_bottom)
    assert_no_page_errors(errors)


@pytest.mark.asyncio
@pytest.mark.parametrize("dpr", DPRS)
async def test_board_row_touches_system_windows(tmp_path, monkeypatch, make_page, dpr):
    """Clean layout with a live board: the board row's bottom is the system
    windows' top -- no gap between them."""
    async with running_tournament(tmp_path, monkeypatch) as (_app, base, _t, _secret):
        _ctx, page = await make_page(viewport=VIEWPORT, device_scale_factor=dpr)
        errors = watch_page_errors(page)
        await open_arena_engines_window(page, base)
        await page.click(".wb-engines .wb-sched-list .wb-sched-live .wb-sched-attach-btn")
        await _tidy(page, _BOARD_TIDY_DONE)

        s = await page.evaluate(_SPANS)
        assert s["wb-livegame"][1] == s["wb-engines"][0], (dpr, s["wb-livegame"], s["wb-engines"])
        assert_no_page_errors(errors)


# The Standings window's target CSS-px bottom (inline style; see _SPANS) and
# whether WinBox has it maximized.
_STANDINGS_BOTTOM = """() => {
  const w = document.querySelector('.winbox.sturddle-wb-standings');
  return [parseFloat(w.style.top) + parseFloat(w.style.height), w.classList.contains('max')];
}"""
LAYOUT_ITEMS = {"snap": ".tmb-snap", "tile": ".tmb-tile"}


async def _standings_ends_on_list_border(page, dpr, expect_max=False):
    bottom, is_max = await page.evaluate(_STANDINGS_BOTTOM)
    list_bottom, outline = await page.evaluate(_LIST_BOTTOM_AND_OUTLINE)
    assert is_max == expect_max
    assert bottom + outline == pytest.approx(list_bottom, abs=SUBPIXEL), (dpr, bottom, list_bottom)


@pytest.mark.asyncio
@pytest.mark.parametrize("dpr", DPRS)
@pytest.mark.parametrize("layout", sorted(LAYOUT_ITEMS))
async def test_snap_and_tile_end_on_list_border(server, make_page, layout, dpr):
    """Snap / Tile, nothing minimized: the bottom window's outline lands on
    the list's bottom border, as in Clean -- not at the viewport's bottom."""
    _ctx, page = await make_page(viewport=VIEWPORT, device_scale_factor=dpr)
    errors = watch_page_errors(page)
    await goto_arena_tournaments(page, server)
    await page.click(".tournament-row")
    await open_arena_workspace(page)
    await page.click(".tmb-window-btn")
    await page.click(LAYOUT_ITEMS[layout])
    await page.wait_for_function("() => !!document.querySelector('.winbox.sturddle-wb-standings.no-shadow')")

    await _standings_ends_on_list_border(page, dpr)
    assert_no_page_errors(errors)


@pytest.mark.asyncio
@pytest.mark.parametrize("dpr", DPRS)
async def test_maximized_window_ends_on_list_border(server, make_page, dpr):
    """A maximized window stops at the list's bottom border too."""
    _ctx, page = await make_page(viewport=VIEWPORT, device_scale_factor=dpr)
    errors = watch_page_errors(page)
    await goto_arena_tournaments(page, server)
    await page.click(".tournament-row")
    await open_arena_workspace(page)
    await page.click(".winbox.sturddle-wb-standings .wb-max")
    await page.wait_for_function("() => !!document.querySelector('.winbox.sturddle-wb-standings.max')")

    await _standings_ends_on_list_border(page, dpr, expect_max=True)
    assert_no_page_errors(errors)
