"""E2E: the rail dock under the moves list holds two stacked windows.

Covers rail capacity and drop targeting (web/app/play-dock-windows.js) and
band sizing (web/app/game-view.js): the band grows to fit a stack, and its
lift grip stops at that floor. Windows reach the rail either by restore
(seeded localStorage) or by dragging a slot header onto the band. Every wait
is a predicate on the DOM/geometry the assertions then read.

Skipped if Playwright is missing.
"""
from __future__ import annotations

import json

import httpx
import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from .conftest import (  # noqa: E402
    assert_no_page_errors,
    e2e_env,
    run_uvicorn_subprocess,
    wait_perspective_ready,
    watch_page_errors,
)


PLAY_PERSP = "#play-perspective"
VIEWPORT = {"width": 1600, "height": 1000}
SETTING_EVAL_GRAPH = "play_show_eval_graph"

RAIL = ".play-rail-dock"
MAIN_DOCK = ".play-dock-left"
SIDE_HOST = ".play-side-host"
RAIL_LIFT_GRIP = ".play-rail-grip"
RAIL_GRIP = f"{RAIL} > .dock-grip"
RAIL_GHOST = f"{RAIL} .dock-ghost"
SLOT = ".dock-slot"
VISIBLE_SLOT = ".dock-slot:not(.dock-slot-hidden)"
SLOT_TITLE = ".dock-slot-title"
SLOT_HEADER = ".dock-slot-header"
SLOT_CLOSE = ".dock-slot-close"

SEARCH_LINES = "Search Lines"
UCI_LOG = "UCI Log"
ANALYSIS = "Analysis"
ENGINE_EVAL = "Engine Eval"

# localStorage contract (web/app/storage-keys.js).
KEY_DEST = "sturddle:play:dockDest"
KEY_GROW = "sturddle:play:dockGrow"
KEY_LIFT = "sturddle:play:railLift"
UCI_OPEN = "sturddle:ucilog:open"
UCI_DOCKED = "sturddle:ucilog:docked"
PV_OPEN = "sturddle:pvtable:open"
PV_DOCKED = "sturddle:pvtable:docked"
AI_OPEN = "sturddle:ai:open"
AI_DOCKED = "sturddle:ai:docked"
OPEN_ON = "1"
DEST_RAIL = "rail"

# Mirrors game-view.js RAIL_SLOT_MIN_REM: room the band gives each stacked
# window, in rem.
RAIL_SLOT_MIN_REM = 5
STACK = 2

# Pointer travel: past the slot-drag undock threshold, then a visible grip
# drag; the lift test goes up a little and down well past its start.
UNDOCK_TRAVEL_PX = 30
HEADER_GRAB_X_PX = 12
GRIP_DRAG_PX = 20
LIFT_UP_PX = 30
LIFT_DOWN_PX = 200
DRAG_STEPS = 10
LAYOUT_SLACK_PX = 2


@pytest.fixture
def server(tmp_path):
    env = e2e_env(tmp_path)
    with run_uvicorn_subprocess(env_overrides=env) as base:
        yield base


def _seed_js(open_keys, rail_docked_keys):
    """Init script: mark windows open and make the rail their home. Guarded
    to http(s) documents -- the blank initial page has no storage."""
    dests = {key: DEST_RAIL for key in rail_docked_keys}
    return (
        "(() => {"
        " if (!location.protocol.startsWith('http')) return;"
        f" for (const k of {json.dumps(open_keys)})"
        f" localStorage.setItem(k, {json.dumps(OPEN_ON)});"
        f" localStorage.setItem({json.dumps(KEY_DEST)}, {json.dumps(json.dumps(dests))});"
        "})();"
    )


async def _open_play(make_page, base, *, open_keys, rail_keys, eval_graph=False):
    """Load Play with `open_keys` windows open and `rail_keys` homed in the
    rail. The eval graph is off unless asked: it would claim a rail slot."""
    httpx.put(f"{base}/settings", json={SETTING_EVAL_GRAPH: eval_graph}).raise_for_status()
    ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await ctx.add_init_script(_seed_js(open_keys, rail_keys))
    await page.goto(base + "/")
    await page.wait_for_selector(PLAY_PERSP)
    await wait_perspective_ready(page)
    return page, errors


async def _titles(page, container, *, visible_only=True):
    """Slot titles in a dock container, top to bottom."""
    slot = VISIBLE_SLOT if visible_only else SLOT
    return await page.evaluate(
        "([slots, title]) => [...document.querySelectorAll(slots)]"
        ".map(s => s.querySelector(title).textContent)",
        [f"{container} {slot}", SLOT_TITLE],
    )


async def _wait_rail_slots(page, count):
    await page.wait_for_function(
        "([sel, n]) => document.querySelectorAll(sel).length === n",
        arg=[f"{RAIL} {SLOT}", count],
    )


async def _rect(page, selector):
    return await page.evaluate(
        "(sel) => { const r = document.querySelector(sel).getBoundingClientRect();"
        " return { x: r.x, y: r.y, height: r.height, top: r.top, bottom: r.bottom }; }",
        selector,
    )


async def _center(page, selector):
    box = await page.locator(selector).bounding_box()
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


async def _stack_floor_px(page):
    """Band height that fits a full stack, at the page's root font size."""
    root_px = await page.evaluate(
        "() => parseFloat(getComputedStyle(document.documentElement).fontSize)"
    )
    return STACK * RAIL_SLOT_MIN_REM * root_px


async def _wait_band(page, test_js, px):
    """Wait until `test_js` (over the band height `h` and `px`) holds."""
    await page.wait_for_function(
        "([sel, px]) => { const h = document.querySelector(sel)"
        f".getBoundingClientRect().height; return {test_js}; }}",
        arg=[RAIL, px],
    )


async def _wait_band_at_least(page, px):
    await _wait_band(page, "h >= px", px)


async def _wait_band_below(page, px):
    await _wait_band(page, "h < px", px)


async def _wait_band_near(page, px):
    await _wait_band(page, f"Math.abs(h - px) <= {LAYOUT_SLACK_PX}", px)


async def _storage_json(page, key):
    return json.loads(await page.evaluate("(k) => localStorage.getItem(k)", key))


async def _click_slot_close(page, title):
    await page.evaluate(
        "([slot, titleSel, closeSel, title]) => [...document.querySelectorAll(slot)]"
        ".find(s => s.querySelector(titleSel)?.textContent === title)"
        "?.querySelector(closeSel)?.click()",
        [SLOT, SLOT_TITLE, SLOT_CLOSE, title],
    )


@pytest.mark.asyncio
async def test_rail_holds_two_stacked_windows(server, make_page):
    """Two windows homed in the rail both restore into it, in dock order
    with a grip between. The band grows to fit them, the side rail yields
    instead of overlapping, and the user's saved lift is left alone."""
    page, errors = await _open_play(
        make_page, server,
        open_keys=[UCI_OPEN, PV_OPEN], rail_keys=[UCI_DOCKED, PV_DOCKED],
    )
    await _wait_rail_slots(page, STACK)
    assert await _titles(page, RAIL) == [SEARCH_LINES, UCI_LOG]
    assert await _titles(page, MAIN_DOCK) == []
    assert await page.locator(RAIL_GRIP).count() == 1
    await _wait_band_at_least(page, await _stack_floor_px(page))
    side = await _rect(page, SIDE_HOST)
    rail = await _rect(page, RAIL)
    assert side["bottom"] <= rail["top"], (side, rail)
    assert await page.evaluate("(k) => localStorage.getItem(k)", KEY_LIFT) is None
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_hidden_eval_strip_yields_to_a_second_window(server, make_page):
    """The eval strip is the rail's default tenant but stays hidden until it
    has samples. Two windows homed there push it to the main dock instead of
    leaving a third slot in the band."""
    page, errors = await _open_play(
        make_page, server,
        open_keys=[UCI_OPEN, PV_OPEN], rail_keys=[UCI_DOCKED, PV_DOCKED],
        eval_graph=True,
    )
    await _wait_rail_slots(page, STACK)
    assert await _titles(page, RAIL, visible_only=False) == [SEARCH_LINES, UCI_LOG]
    assert await _titles(page, MAIN_DOCK, visible_only=False) == [ENGINE_EVAL]
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_full_rail_sends_a_third_window_to_the_main_dock(server, make_page):
    """Three windows homed in the rail: the band takes two, the third
    falls back to the main dock instead of overflowing it."""
    page, errors = await _open_play(
        make_page, server,
        open_keys=[UCI_OPEN, PV_OPEN, AI_OPEN],
        rail_keys=[UCI_DOCKED, PV_DOCKED, AI_DOCKED],
    )
    await _wait_rail_slots(page, STACK)
    assert await _titles(page, RAIL) == [SEARCH_LINES, UCI_LOG]
    assert await _titles(page, MAIN_DOCK) == [ANALYSIS]
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_drop_onto_occupied_rail_docks_as_second_slot(server, make_page):
    """Dragging a docked window over a rail that already holds one previews
    the stacked size, and releasing docks it as the second slot and
    remembers the rail as its home."""
    page, errors = await _open_play(
        make_page, server,
        open_keys=[UCI_OPEN, PV_OPEN], rail_keys=[PV_DOCKED],
    )
    await page.wait_for_selector(f"{MAIN_DOCK} {SLOT}")
    assert await _titles(page, RAIL) == [SEARCH_LINES]
    floor = await _stack_floor_px(page)

    header = await _rect(page, f"{MAIN_DOCK} {SLOT_HEADER}")
    grab_x = header["x"] + HEADER_GRAB_X_PX
    grab_y = header["y"] + header["height"] / 2
    await page.mouse.move(grab_x, grab_y)
    await page.mouse.down()
    await page.mouse.move(grab_x + UNDOCK_TRAVEL_PX, grab_y + UNDOCK_TRAVEL_PX)
    rail_x, rail_y = await _center(page, RAIL)
    await page.mouse.move(rail_x, rail_y, steps=DRAG_STEPS)
    await page.wait_for_selector(RAIL_GHOST, state="attached")
    await _wait_band_at_least(page, floor)
    await page.mouse.up()

    await _wait_rail_slots(page, STACK)
    assert await _titles(page, RAIL) == [SEARCH_LINES, UCI_LOG]
    assert (await _storage_json(page, KEY_DEST))[UCI_DOCKED] == DEST_RAIL
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_rail_grip_resizes_the_stacked_slots(server, make_page):
    """The grip between the two rail slots moves their boundary and saves
    each window's share."""
    page, errors = await _open_play(
        make_page, server,
        open_keys=[UCI_OPEN, PV_OPEN], rail_keys=[UCI_DOCKED, PV_DOCKED],
    )
    await _wait_band_at_least(page, await _stack_floor_px(page))
    top_slot = f"{RAIL} {SLOT}"
    before = (await _rect(page, top_slot))["height"]

    grip_x, grip_y = await _center(page, RAIL_GRIP)
    await page.mouse.move(grip_x, grip_y)
    await page.mouse.down()
    await page.mouse.move(grip_x, grip_y + GRIP_DRAG_PX, steps=DRAG_STEPS)
    await page.mouse.up()

    after = (await _rect(page, top_slot))["height"]
    assert abs(after - before - GRIP_DRAG_PX) <= LAYOUT_SLACK_PX, (before, after)
    assert {PV_DOCKED, UCI_DOCKED} <= set(await _storage_json(page, KEY_GROW))
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_band_returns_to_bare_size_when_the_stack_breaks_up(server, make_page):
    """The extra height belongs to the stack: closing one window gives it
    back and drops the grip."""
    page, errors = await _open_play(
        make_page, server,
        open_keys=[UCI_OPEN, PV_OPEN], rail_keys=[UCI_DOCKED, PV_DOCKED],
    )
    floor = await _stack_floor_px(page)
    await _wait_band_at_least(page, floor)

    await _click_slot_close(page, UCI_LOG)
    await _wait_rail_slots(page, 1)
    await _wait_band_below(page, floor)
    assert await page.locator(RAIL_GRIP).count() == 0
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_lift_grip_stops_at_the_stack_floor(server, make_page):
    """Grabbing the lift grip at the stacked floor: up lifts at once (no
    dead zone), and down past the start cannot shrink the stack."""
    page, errors = await _open_play(
        make_page, server,
        open_keys=[UCI_OPEN, PV_OPEN], rail_keys=[UCI_DOCKED, PV_DOCKED],
    )
    floor = await _stack_floor_px(page)
    await _wait_band_near(page, floor)

    grip_x, grip_y = await _center(page, RAIL_LIFT_GRIP)
    await page.mouse.move(grip_x, grip_y)
    await page.mouse.down()
    await page.mouse.move(grip_x, grip_y - LIFT_UP_PX, steps=DRAG_STEPS)
    await _wait_band_near(page, floor + LIFT_UP_PX)
    await page.mouse.move(grip_x, grip_y + LIFT_DOWN_PX, steps=DRAG_STEPS)
    await _wait_band_near(page, floor)
    await page.mouse.up()
    assert_no_page_errors(errors)
