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
    CLEAN_WINDOWS,
    MENU_CHECKED,
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
SUBPIXEL = 0.01
# Clean (tidy) layout: Engine Instances top-left, Standings top-right.
LEFT_WIN = ".winbox.sturddle-wb-arena:has(.wb-engines)"
RIGHT_WIN = ".winbox.sturddle-wb-arena.sturddle-wb-standings"

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
_BOARD_TIDY_DONE = f"""() => document.querySelectorAll('.winbox.sturddle-wb-arena.no-shadow').length === {CLEAN_WINDOWS}
  && !!document.querySelector('.winbox.sturddle-wb-live.no-shadow')"""


@pytest.mark.asyncio
@pytest.mark.parametrize("dpr", DPRS)
async def test_system_window_rows_touch(server, make_page, dpr):
    """Clean layout: each upper window's bottom is the lower one's top."""
    _ctx, page = await make_page(viewport=VIEWPORT, device_scale_factor=dpr)
    errors = watch_page_errors(page)
    await goto_arena_tournaments(page, server)
    await page.click(".tournament-row")
    await open_arena_workspace(page)

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
        await page.wait_for_function(_BOARD_TIDY_DONE)

        s = await page.evaluate(_SPANS)
        assert s["wb-livegame"][1] == s["wb-engines"][0], (dpr, s["wb-livegame"], s["wb-engines"])
        assert_no_page_errors(errors)


# The lowest matching window's target CSS-px bottom (inline style; see
# _SPANS) and whether WinBox has any of them maximized.
_LOWEST_BOTTOM = """(sel) => {
  const wins = [...document.querySelectorAll(sel)];
  return [
    Math.max(...wins.map((w) => parseFloat(w.style.top) + parseFloat(w.style.height))),
    wins.some((w) => w.classList.contains('max')),
  ];
}"""
LAYOUT_ITEMS = {"snap": ".tmb-snap", "tile": ".tmb-tile"}
ARENA_WIN = ".winbox.sturddle-wb-arena"
STANDINGS_WIN = ".winbox.sturddle-wb-standings"


async def _bottom_ends_on_list_border(page, dpr, sel, expect_max=False):
    bottom, is_max = await page.evaluate(_LOWEST_BOTTOM, sel)
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
    # Clean already drops shadows; the checked menu item marks the switch.
    await page.wait_for_selector(f"{LAYOUT_ITEMS[layout]}.{MENU_CHECKED}", state="attached")

    await _bottom_ends_on_list_border(page, dpr, ARENA_WIN)
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
    await page.click(f"{STANDINGS_WIN} .wb-max")
    await page.wait_for_selector(f"{STANDINGS_WIN}.max")

    await _bottom_ends_on_list_border(page, dpr, STANDINGS_WIN, expect_max=True)
    assert_no_page_errors(errors)


# A window's target CSS-px bottom (inline style; see _SPANS) and the viewport
# height.
_BOTTOM_AND_VIEWPORT = """(sel) => {
  const w = document.querySelector(sel);
  return [parseFloat(w.style.top) + parseFloat(w.style.height), window.innerHeight];
}"""
# Computed z-index of the minimized window, the highest of the other
# (non-minimized) Arena windows, and the menubar dropdowns'.
_MIN_Z_VS_OTHERS = """(sel) => {
  const z = (w) => parseInt(getComputedStyle(w).zIndex, 10) || 0;
  const others = [...document.querySelectorAll('.winbox.sturddle-wb-arena:not(.min)')];
  return [z(document.querySelector(sel)), Math.max(...others.map(z)), z(document.querySelector('.tmb-dropdown'))];
}"""


async def _minimize_standings(page, server):
    await goto_arena_tournaments(page, server)
    await page.click(".tournament-row")
    await open_arena_workspace(page)
    await page.click(f"{STANDINGS_WIN} .wb-min")
    await page.wait_for_selector(f"{STANDINGS_WIN}.min")


@pytest.mark.asyncio
@pytest.mark.parametrize("dpr", DPRS)
async def test_minimized_window_docks_in_footer_strip(server, make_page, dpr):
    """A minimized window docks at the viewport bottom (the strip the layouts
    reserve), not on the list's bottom border that fences maximize."""
    _ctx, page = await make_page(viewport=VIEWPORT, device_scale_factor=dpr)
    errors = watch_page_errors(page)
    await _minimize_standings(page, server)

    bottom, viewport_h = await page.evaluate(_BOTTOM_AND_VIEWPORT, STANDINGS_WIN)
    assert bottom == pytest.approx(viewport_h, abs=SUBPIXEL), (dpr, bottom, viewport_h)
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_minimized_window_stays_above_others(server, make_page):
    """Focusing another window doesn't bury a minimized bar under it; the
    menubar dropdowns still open over it."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _minimize_standings(page, server)
    await page.click(f"{LEFT_WIN} .wb-title")

    min_z, others_z, menu_z = await page.evaluate(_MIN_Z_VS_OTHERS, STANDINGS_WIN)
    assert others_z < min_z < menu_z, (others_z, min_z, menu_z)
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_maximize_from_minimized_ends_on_list_border(server, make_page):
    """Docking in the footer strip doesn't drop the maximize fence: a
    minimized window maximized straight from its bar still stops at the
    list's bottom border."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _minimize_standings(page, server)
    await page.click(f"{STANDINGS_WIN} .wb-max")
    await page.wait_for_selector(f"{STANDINGS_WIN}.max")

    await _bottom_ends_on_list_border(page, 1, STANDINGS_WIN, expect_max=True)
    assert_no_page_errors(errors)


# Minimized bars' CSS-px span: [left, right, top] across all of them.
_MIN_STRIP = """() => {
  const rs = [...document.querySelectorAll('.winbox.min')].map((w) => w.getBoundingClientRect());
  return [Math.min(...rs.map((r) => r.left)), Math.max(...rs.map((r) => r.right)), rs[0].top];
}"""
# Distinct colors along each device-px row, between x0 and x1.
_ROW_COLORS = """async ([b64, ys, x0, x1]) => {
  const img = new Image();
  img.src = 'data:image/png;base64,' + b64;
  await img.decode();
  const c = document.createElement('canvas');
  c.width = img.width; c.height = img.height;
  const g = c.getContext('2d');
  g.drawImage(img, 0, 0);
  return ys.map((y) => {
    const d = g.getImageData(x0, y, x1 - x0, 1).data;
    const seen = new Set();
    for (let i = 0; i < d.length; i += 4) seen.add(`${d[i]},${d[i + 1]},${d[i + 2]}`);
    return [...seen];
  });
}"""
MINIMIZE_ORDER = (STANDINGS_WIN, LEFT_WIN, ".winbox.sturddle-wb-arena:has(.wb-eventlog)")
# Integer DPRs only: a fractional one blends every edge across device pixels
# (a seam's as much as any other), so its rows aren't one color either way.
CRISP_DPRS = (1, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("dpr", CRISP_DPRS)
async def test_minimized_bars_share_one_seam(server, make_page, dpr):
    """Side-by-side bars don't double their edges where they meet: the rows
    along the strip's top edge are each one color end to end, with no
    brighter spot where neighboring bars' translucent edges cross."""
    _ctx, page = await make_page(viewport=VIEWPORT, device_scale_factor=dpr)
    errors = watch_page_errors(page)
    await goto_arena_tournaments(page, server)
    await page.click(".tournament-row")
    await open_arena_workspace(page)
    for sel in MINIMIZE_ORDER:
        await page.click(f"{sel}:not(.min) .wb-min")
        await page.wait_for_selector(f"{sel}.min")
    await page.mouse.move(VIEWPORT["width"] / 2, 0)
    # WinBox slides the bars into place.
    await page.wait_for_timeout(500)

    left, right, top = await page.evaluate(_MIN_STRIP)
    shot = await page.screenshot()
    # The outermost device-px row just above the bars and the first inside.
    ys = [round(top * dpr) - 1, round(top * dpr)]
    rows = await page.evaluate(
        _ROW_COLORS, [base64.b64encode(shot).decode(), ys, round(left * dpr) + 2, round(right * dpr) - 2],
    )
    assert all(len(colors) == 1 for colors in rows), (dpr, rows)
    assert_no_page_errors(errors)
