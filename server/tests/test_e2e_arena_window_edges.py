"""E2E: Arena windows line up with the tournament list's border and with each
other, across device pixel ratios incl. below 1 (Playwright/Chromium).

Windows draw their outline outside their box; the workspace area is inset by
that outline so it lands on the list's side borders: the Clean layout's
windows' outer outline edges are the list border box's sides. In Clean,
stacked rows touch (system rows, and the live-board row over them), and the
bottom row's outline lands on the list's bottom border. All checks are layout
geometry, never rendered pixels.

Skipped if Playwright/Chromium isn't installed.
"""
from __future__ import annotations

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
RESIZED_VIEWPORT = {"width": 1400, "height": 700}
# Below 1 and fractional: where border snapping moves layout. Checks that
# only read WinBox's inline CSS-px math run at the default DPR.
DPRS = (0.75, 1.25)
SUBPIXEL = 0.01
# Clean (tidy) layout: Engine Instances top-left, Standings top-right.
LEFT_WIN = ".winbox.sturddle-wb-arena:has(.wb-engines)"
RIGHT_WIN = ".winbox.sturddle-wb-arena.sturddle-wb-standings"

# A JS expression: element el's CSS-px outline width. Chromium reports an
# unset outline's default width, so style none counts as 0.
_OUTLINE_W = "(getComputedStyle(el).outlineStyle === 'none' ? 0 : parseFloat(getComputedStyle(el).outlineWidth))"
# CSS-px outer [left, right]: the box plus its outline.
_OUTER_X = f"""(sel) => {{
  const el = document.querySelector(sel);
  const r = el.getBoundingClientRect();
  const ow = {_OUTLINE_W};
  return [r.left - ow, r.right + ow];
}}"""
LIST = ".tournaments-list"


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
    lst = await page.evaluate(_OUTER_X, LIST)
    left_win = await page.evaluate(_OUTER_X, LEFT_WIN)
    right_win = await page.evaluate(_OUTER_X, RIGHT_WIN)
    assert left_win[0] == pytest.approx(lst[0], abs=SUBPIXEL), (dpr, lst, left_win)
    assert right_win[1] == pytest.approx(lst[1], abs=SUBPIXEL), (dpr, lst, right_win)
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
_LIST_BOTTOM_AND_OUTLINE = f"""() => {{
  const el = document.querySelector('.winbox.sturddle-wb-arena:not(.min)');
  return [document.querySelector('{LIST}').getBoundingClientRect().bottom, {_OUTLINE_W}];
}}"""
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
async def test_board_row_touches_system_windows(tmp_path, monkeypatch, make_page):
    """Clean layout with a live board: the board row's bottom is the system
    windows' top -- no gap between them."""
    async with running_tournament(tmp_path, monkeypatch) as (_app, base, _t, _secret):
        _ctx, page = await make_page(viewport=VIEWPORT)
        errors = watch_page_errors(page)
        await open_arena_engines_window(page, base)
        await page.click(".wb-engines .wb-sched-list .wb-sched-live .wb-sched-attach-btn")
        await page.wait_for_function(_BOARD_TIDY_DONE)

        s = await page.evaluate(_SPANS)
        assert s["wb-livegame"][1] == s["wb-engines"][0], (s["wb-livegame"], s["wb-engines"])
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


async def _bottom_ends_on_list_border(page, sel, expect_max=False):
    bottom, is_max = await page.evaluate(_LOWEST_BOTTOM, sel)
    list_bottom, outline = await page.evaluate(_LIST_BOTTOM_AND_OUTLINE)
    assert is_max == expect_max
    assert bottom + outline == pytest.approx(list_bottom, abs=SUBPIXEL), (bottom, list_bottom)


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", sorted(LAYOUT_ITEMS))
async def test_snap_and_tile_end_on_list_border(server, make_page, layout):
    """Snap / Tile, nothing minimized: the bottom window's outline lands on
    the list's bottom border, as in Clean -- not at the viewport's bottom."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await goto_arena_tournaments(page, server)
    await page.click(".tournament-row")
    await open_arena_workspace(page)
    await page.click(".tmb-window-btn")
    await page.click(LAYOUT_ITEMS[layout])
    # Clean already drops shadows; the checked menu item marks the switch.
    await page.wait_for_selector(f"{LAYOUT_ITEMS[layout]}.{MENU_CHECKED}", state="attached")

    await _bottom_ends_on_list_border(page, ARENA_WIN)
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
async def test_minimized_window_docks_in_footer_strip(server, make_page):
    """A minimized window docks at the viewport bottom (the strip the layouts
    reserve), not on the list's bottom border that fences maximize."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _minimize_standings(page, server)

    bottom, viewport_h = await page.evaluate(_BOTTOM_AND_VIEWPORT, STANDINGS_WIN)
    assert bottom == pytest.approx(viewport_h, abs=SUBPIXEL), (bottom, viewport_h)
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_minimized_window_stays_above_others(server, make_page):
    """Focusing another window doesn't bury a minimized bar under it; the
    menubar dropdowns still open over it."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _minimize_standings(page, server)
    await page.click(f"{LEFT_WIN} .wb-title")
    await page.wait_for_selector(f"{LEFT_WIN}.focus")

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

    await _bottom_ends_on_list_border(page, STANDINGS_WIN, expect_max=True)
    assert_no_page_errors(errors)


# Minimized bars left to right: CSS-px left, right, outline style, the width
# of the edge drawn inside the box (::before), and whether the bar is on top
# mid-overlap with the previous one (true for the first).
_MIN_BARS = """() => {
  const ws = [...document.querySelectorAll('.winbox.min')]
    .sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
  return ws.map((w, i) => {
    const r = w.getBoundingClientRect();
    const prev = ws[i - 1]?.getBoundingClientRect();
    const onTop = !prev || document.elementFromPoint((r.left + prev.right) / 2, (r.top + r.bottom) / 2)
      ?.closest('.winbox') === w;
    return [r.left, r.right, getComputedStyle(w).outlineStyle,
      parseFloat(getComputedStyle(w, '::before').borderLeftWidth), onTop];
  });
}"""
MINIMIZE_ORDER = (STANDINGS_WIN, LEFT_WIN, ".winbox.sturddle-wb-arena:has(.wb-eventlog)")
# WinBox slides minimized bars into place: settled once each one's rendered
# box sits at its target (inline style) left/top.
_MIN_BARS_SETTLED = f"""() => [...document.querySelectorAll('.winbox.min')].every((w) => {{
  const r = w.getBoundingClientRect();
  return Math.abs(r.left - parseFloat(w.style.left)) < {SUBPIXEL} && Math.abs(r.top - parseFloat(w.style.top)) < {SUBPIXEL};
}})"""


@pytest.mark.asyncio
async def test_minimized_bars_share_one_seam(server, make_page):
    """Side-by-side bars don't double their edges where they meet: each draws
    its edge inside its box (no outside outline), and each next bar overlaps
    the previous by exactly that edge, on top of it, so one edge shows."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await goto_arena_tournaments(page, server)
    await page.click(".tournament-row")
    await open_arena_workspace(page)
    for sel in MINIMIZE_ORDER:
        await page.click(f"{sel}:not(.min) .wb-min")
        await page.wait_for_selector(f"{sel}.min")
    await page.wait_for_function(_MIN_BARS_SETTLED)

    bars = await page.evaluate(_MIN_BARS)
    assert len(bars) == len(MINIMIZE_ORDER), bars
    assert all(style == "none" and edge > 0 and on_top for _l, _r, style, edge, on_top in bars), bars
    for (_l, prev_right, _s, edge, _t), (next_left, *_rest) in zip(bars, bars[1:]):
        assert prev_right - next_left == pytest.approx(edge, abs=SUBPIXEL), bars
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_maximized_window_ends_on_list_border_after_resize(server, make_page):
    """The maximize fence follows the list: after a viewport resize, a
    window created before it still stops at the list's bottom border."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await goto_arena_tournaments(page, server)
    await page.click(".tournament-row")
    await open_arena_workspace(page)
    await page.set_viewport_size(RESIZED_VIEWPORT)
    await page.click(f"{STANDINGS_WIN} .wb-max")
    await page.wait_for_selector(f"{STANDINGS_WIN}.max")

    await _bottom_ends_on_list_border(page, STANDINGS_WIN, expect_max=True)
    assert_no_page_errors(errors)


LOG_WIN = ".winbox.sturddle-wb-arena:has(.wb-eventlog)"
# Inline bottom edge of a window (its layout target, not the animated box).
_INLINE_BOTTOM = """(sel) => {
  const w = document.querySelector(sel);
  return parseFloat(w.style.top) + parseFloat(w.style.height);
}"""


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ("max", "min"))
async def test_min_or_max_window_survives_a_resize_in_clean(server, make_page, state):
    """Maximize or minimize, then resize: the debounced refit re-lays out
    Clean around the window instead of restoring it into its tile
    (docs/tournament-workspace.md T-S1 / T-S2)."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await goto_arena_tournaments(page, server)
    await page.click(".tournament-row")
    await open_arena_workspace(page)
    await page.click(f"{STANDINGS_WIN} .wb-{state}")
    await page.wait_for_selector(f"{STANDINGS_WIN}.{state}")
    log_bottom = await page.evaluate(_INLINE_BOTTOM, LOG_WIN)
    await page.set_viewport_size(RESIZED_VIEWPORT)
    # The refit has run once Clean re-tiled the other windows to the new edge.
    await page.wait_for_function(
        f"([sel, before]) => ({_INLINE_BOTTOM})(sel) !== before", arg=[LOG_WIN, log_bottom],
    )

    assert await page.locator(f"{STANDINGS_WIN}.{state}").count() == 1
    if state == "max":
        await _bottom_ends_on_list_border(page, STANDINGS_WIN, expect_max=True)
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_closed_window_stays_closed_after_a_resize_in_clean(server, make_page):
    """X-close, then resize: the refit tiles what is open and does not reopen
    the closed window (docs/tournament-workspace.md T-S4)."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await goto_arena_tournaments(page, server)
    await page.click(".tournament-row")
    await open_arena_workspace(page)
    await page.click(f"{LEFT_WIN} .wb-close")
    await page.wait_for_selector(LEFT_WIN, state="detached")
    log_bottom = await page.evaluate(_INLINE_BOTTOM, LOG_WIN)
    await page.set_viewport_size(RESIZED_VIEWPORT)
    await page.wait_for_function(
        f"([sel, before]) => ({_INLINE_BOTTOM})(sel) !== before", arg=[LOG_WIN, log_bottom],
    )

    assert await page.locator(LEFT_WIN).count() == 0
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_maximized_window_ends_on_list_border_when_built_unlaid(server, make_page):
    """The maximize fence is read at maximize, not at creation: windows built
    while the list has no box (no bound to measure) still stop at its
    bottom border once it's laid out."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await goto_arena_tournaments(page, server)
    await page.click(".tournament-row")
    await page.evaluate(f"() => {{ document.querySelector('{LIST}').style.display = 'none'; }}")
    await open_arena_workspace(page)
    await page.evaluate(f"() => {{ document.querySelector('{LIST}').style.display = ''; }}")
    await page.click(f"{STANDINGS_WIN} .wb-max")
    await page.wait_for_selector(f"{STANDINGS_WIN}.max")

    await _bottom_ends_on_list_border(page, STANDINGS_WIN, expect_max=True)
    assert_no_page_errors(errors)


# Hides the list from page load on (style id HIDE_LIST_ID) so a workspace
# restored at startup is built before the list has a box.
HIDE_LIST_ID = "test-hide-list"
_HIDE_LIST_AT_LOAD = f"""document.addEventListener('DOMContentLoaded', () => {{
  const s = document.createElement('style');
  s.id = '{HIDE_LIST_ID}';
  s.textContent = '{LIST} {{ display: none !important; }}';
  document.head.append(s);
}});"""
_ENDS_ON_LIST_BORDER = f"""(sel) => {{
  const el = document.querySelector(sel);
  const list = document.querySelector('{LIST}').getBoundingClientRect().bottom;
  return Math.abs(el.getBoundingClientRect().bottom + {_OUTLINE_W} - list) < {SUBPIXEL};
}}"""


@pytest.mark.asyncio
async def test_restored_maximized_window_refits_once_list_is_laid_out(server, make_page):
    """A window restored already maximized, before the list had a box,
    refits to the list's bottom border once the list lays out. Snap, as
    Clean re-tiles a restored maximized window."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await goto_arena_tournaments(page, server)
    await page.click(".tournament-row")
    await open_arena_workspace(page)
    await page.click(".tmb-window-btn")
    await page.click(LAYOUT_ITEMS["snap"])
    await page.wait_for_selector(f"{LAYOUT_ITEMS['snap']}.{MENU_CHECKED}", state="attached")
    await page.click(f"{STANDINGS_WIN} .wb-max")
    await page.wait_for_selector(f"{STANDINGS_WIN}.max")
    await page.add_init_script(_HIDE_LIST_AT_LOAD)
    await page.reload()
    await page.wait_for_selector(f"{STANDINGS_WIN}.max")
    await page.evaluate(f"() => document.getElementById('{HIDE_LIST_ID}').remove()")

    await page.wait_for_function(_ENDS_ON_LIST_BORDER, arg=STANDINGS_WIN)
    assert_no_page_errors(errors)


# Maximized window's target right edge (inline style: WinBox's own math,
# whatever CSS stretches it to) and the workspace's right edge, from the
# ribbon's current width.
_MAX_RIGHT_VS_WORKSPACE = f"""(sel) => {{
  const el = document.querySelector(sel);
  const ribbonW = document.querySelector('.tournaments-ribbon').getBoundingClientRect().width;
  return [parseFloat(el.style.left) + parseFloat(el.style.width), Math.floor(window.innerWidth) - Math.ceil(ribbonW) - {_OUTLINE_W}];
}}"""


@pytest.mark.asyncio
async def test_maximize_right_fence_follows_ribbon_width(server, make_page):
    """The right maximize fence is read at maximize: after the ribbon
    widens, a maximized window still stops at its reserve."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await goto_arena_tournaments(page, server)
    await page.click(".tournament-row")
    await open_arena_workspace(page)
    await page.evaluate("() => { document.querySelector('.tournaments-ribbon').style.width = '80px'; }")
    await page.click(f"{STANDINGS_WIN} .wb-max")
    await page.wait_for_selector(f"{STANDINGS_WIN}.max")

    right, workspace_right = await page.evaluate(_MAX_RIGHT_VS_WORKSPACE, STANDINGS_WIN)
    assert right == pytest.approx(workspace_right, abs=SUBPIXEL), (right, workspace_right)
    assert_no_page_errors(errors)
