"""E2E: Play-perspective debug windows dock/undock/restore lifecycle.

Covers the UCI Log and Search Lines windows refactored into the
createDockableWindow factory (web/app/play-dock-windows.js).
Skipped if Playwright is missing.
"""
from __future__ import annotations


import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from .conftest import assert_no_page_errors, e2e_env, run_uvicorn_subprocess, wait_winbox_settled  # noqa: E402


PLAY_PERSP = "#play-perspective"
DOCK_LEFT = ".play-dock-left"
RAIL_DOCK = ".play-rail-dock"
PV_BTN = "#pv-table-btn"
UCI_BTN = "#uci-log-btn"
BOARD_RIBBON = ".board-ribbon"
SHOW_UCI = "Show UCI Log"
HIDE_UCI = "Hide UCI Log"
UCI_WB = ".winbox.sturddle-wb-uci-log"
PV_WB = ".winbox.sturddle-wb-pvtable"
COARSE_POINTER_MQ = "(pointer: coarse)"
DEFAULT_VIEWPORT = {"width": 1600, "height": 1000}
# 1366x768 laptop: taskbar + browser chrome leave a ~620px-tall viewport,
# under --bp-mobile-h-play but far wider than --bp-mobile.
SHORT_VIEWPORT = {"width": 1366, "height": 620}
# Shorter still: the board sits at its minimum and Play outgrows the window.
OVERFLOW_VIEWPORT = {"width": 1366, "height": 500}
WHEEL_PX = 400
# Fixed overlays (dock column, side rail) vs what they line up with.
_GEOM_JS = """() => {
  const top = (sel) => document.querySelector(sel).getBoundingClientRect().top;
  const clockBot = document.querySelector('.clock-row.clock-bottom').getBoundingClientRect().bottom;
  return {
    dockOffset: top('.play-dock-left') - top('.clock-row.clock-top'),
    railOffset: top('.play-side-host') - top('.play-board-host'),
    clockBot, clockTop: top('.clock-row.clock-top'), viewH: window.innerHeight,
    headerTop: top('body > header'),
  };
}"""
_FOOTER_COVERS_DOCK_JS = """() => {
  const d = document.querySelector('.play-dock-left').getBoundingClientRect();
  const f = document.querySelector('#app-footer').getBoundingClientRect();
  return !!document.elementFromPoint(d.left + d.width / 2, f.top + f.height / 2)?.closest('#app-footer');
}"""


@pytest.fixture
def server(tmp_path):
    env = e2e_env(tmp_path)
    with run_uvicorn_subprocess(env_overrides=env) as base:
        yield base


async def _new_page(make_page, viewport=DEFAULT_VIEWPORT, **ctx_kwargs):
    ctx, page = await make_page(viewport=viewport, **ctx_kwargs)
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(f"console.{msg.type}: {msg.text}")
            if msg.type == "error" else None)
    return ctx, page, errors


async def _dock_displayed(page):
    return await page.evaluate(
        f"() => getComputedStyle(document.querySelector('{DOCK_LEFT}')).display !== 'none'"
    )


async def _goto_play(page, base):
    await page.goto(base + "/")
    await page.wait_for_selector(PLAY_PERSP)
    # Wait for mount to finish: button click handlers are attached
    # after the perspective controller resolves its ``ready`` promise,
    # which is when ``#perspective-root.is-pending`` is removed.
    await page.wait_for_function(
        "() => !document.querySelector('#perspective-root')?.classList.contains('is-pending')",
    )


async def _snapshot(page):
    return await page.evaluate("""
      () => {
        const dock = document.querySelector('.play-dock-left');
        const slots = Array.from(document.querySelectorAll('.play-dock-left .dock-slot')).map(s => ({
          title: s.querySelector('.dock-slot-title')?.textContent,
          hasBody: !!s.querySelector('.dock-slot-body')?.firstElementChild,
        }));
        const wbs = Array.from(document.querySelectorAll('.winbox.sturddle-wb')).map(w => ({
          cls: w.className,
          title: w.querySelector('.wb-title')?.textContent,
        }));
        return {
          dockEmpty: dock?.classList.contains('dock-empty') ?? null,
          slots,
          wbs,
          ucilogOpen: localStorage.getItem('sturddle:ucilog:open'),
          pvtableOpen: localStorage.getItem('sturddle:pvtable:open'),
          ucilogDocked: localStorage.getItem('sturddle:ucilog:docked'),
          pvtableDocked: localStorage.getItem('sturddle:pvtable:docked'),
        };
      }
    """)


async def _slot_titles(page):
    s = await _snapshot(page)
    return [x["title"] for x in s["slots"]]


async def _click_slot_undock(page, title):
    await page.evaluate(
        "(t) => { const s = [...document.querySelectorAll('.dock-slot')]"
        ".find(x => x.querySelector('.dock-slot-title')?.textContent === t);"
        " s?.querySelector('.dock-slot-undock')?.click(); }",
        title,
    )


async def _click_wb_dock(page, wb_class):
    await page.evaluate(
        "(c) => document.querySelector('.winbox.' + c + ' .wb-dock-ctrl')?.click()",
        wb_class,
    )


@pytest.mark.asyncio
async def test_default_docked_on_first_open(server, make_page):
    """Both windows dock by default; Search Lines slot sits above UCI Log."""
    _ctx, page, errors = await _new_page(make_page)
    await _goto_play(page, server)
    await page.click(PV_BTN)
    await page.click(UCI_BTN)
    await page.wait_for_selector(f"{DOCK_LEFT} .dock-slot")
    titles = await _slot_titles(page)
    assert titles == ["Search Lines", "UCI Log"], titles
    s = await _snapshot(page)
    assert s["ucilogOpen"] == "1" and s["pvtableOpen"] == "1"
    assert s["wbs"] == []
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_undock_via_slot_button(server, make_page):
    """Clicking the slot's undock control opens a WinBox and removes the slot."""
    _ctx, page, errors = await _new_page(make_page)
    await _goto_play(page, server)
    await page.click(UCI_BTN)
    await page.wait_for_selector(f"{DOCK_LEFT} .dock-slot")
    await _click_slot_undock(page, "UCI Log")
    await page.wait_for_selector(UCI_WB)
    s = await _snapshot(page)
    assert not any(x["title"] == "UCI Log" for x in s["slots"])
    assert s["ucilogDocked"] == "0"
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_redock_via_winbox_control(server, make_page):
    """The WinBox dock control returns the window to its slot in correct order."""
    _ctx, page, errors = await _new_page(make_page)
    await _goto_play(page, server)
    await page.click(PV_BTN)
    await page.click(UCI_BTN)
    await page.wait_for_selector(f"{DOCK_LEFT} .dock-slot")
    await _click_slot_undock(page, "UCI Log")
    await page.wait_for_selector(UCI_WB)
    await _click_wb_dock(page, "sturddle-wb-uci-log")
    await page.wait_for_function(
        "() => !document.querySelector('.winbox.sturddle-wb-uci-log')",
    )
    titles = await _slot_titles(page)
    assert titles == ["Search Lines", "UCI Log"], titles
    s = await _snapshot(page)
    assert s["ucilogDocked"] == "1"
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_close_via_ribbon_tears_down(server, make_page):
    """Toggling the ribbon button while open closes the window and clears state."""
    _ctx, page, errors = await _new_page(make_page)
    await _goto_play(page, server)
    await page.click(PV_BTN)
    await page.wait_for_selector(f"{DOCK_LEFT} .dock-slot")
    await page.click(PV_BTN)  # toggle off
    await page.wait_for_function(
        f"() => !document.querySelector('{DOCK_LEFT} .dock-slot')",
    )
    s = await _snapshot(page)
    assert s["slots"] == []
    assert s["dockEmpty"] is True
    assert s["pvtableOpen"] == "0"
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_nav_away_and_back_restores(server, make_page):
    """closeDebugWindows on unmount, restoreDebugWindows on remount."""
    _ctx, page, errors = await _new_page(make_page)
    await _goto_play(page, server)
    await page.click(PV_BTN)
    await page.click(UCI_BTN)
    await page.wait_for_selector(f"{DOCK_LEFT} .dock-slot")

    # Nav away to engines -- Play perspective unmounts, dock element gone.
    await page.click('button[data-perspective="engines"]')
    await page.wait_for_function(
        "() => !document.querySelector('.play-dock-left')",
    )
    s = await _snapshot(page)
    assert s["slots"] == []
    # open flags remain so restore knows to reopen
    assert s["ucilogOpen"] == "1" and s["pvtableOpen"] == "1"

    # Nav back -- both windows restored, docked, in correct order.
    await page.click('button[data-perspective="play"]')
    await page.wait_for_selector(f"{DOCK_LEFT} .dock-slot")
    titles = await _slot_titles(page)
    assert titles == ["Search Lines", "UCI Log"], titles
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_short_laptop_keeps_desktop_layout(server, make_page):
    """A short-but-wide mouse viewport is a laptop, not a phone: the
    short-height gate must not stack Play into the mobile layout."""
    _ctx, page, errors = await _new_page(make_page, SHORT_VIEWPORT)
    await _goto_play(page, server)
    assert not await page.evaluate(f"() => matchMedia('{COARSE_POINTER_MQ}').matches")
    # A short rail may overflow the button into its menu (hidden): dispatch.
    await page.dispatch_event(PV_BTN, "click")
    await page.wait_for_selector(f"{DOCK_LEFT} .dock-slot")
    assert await _dock_displayed(page)
    assert await page.evaluate(f"() => getComputedStyle(document.querySelector('{RAIL_DOCK}')).display !== 'none'"), (
        "the rail dock is desktop layout too"
    )
    # So is the right-side ribbon mirror: the rail hangs on the right edge.
    await page.evaluate("() => { document.body.dataset.ribbonSide = 'right'; }")
    assert await page.evaluate(f"() => getComputedStyle(document.querySelector('{BOARD_RIBBON}')).right") == "0px"
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_debug_window_buttons_say_show_or_hide(server, make_page):
    """The UCI Log button's label names what a click does: Show while the
    window is closed, Hide while open, however it closed."""
    _ctx, page, errors = await _new_page(make_page)
    await _goto_play(page, server)
    assert await page.get_attribute(UCI_BTN, "aria-label") == SHOW_UCI
    await page.click(UCI_BTN)
    await page.wait_for_selector(f'{UCI_BTN}[aria-label="{HIDE_UCI}"]')
    # Closed from its own X, not the button: hovering the button relabels it.
    await page.click(f"{DOCK_LEFT} .dock-slot-close")
    await page.wait_for_selector(f"{DOCK_LEFT} .dock-slot", state="detached")
    await page.hover(UCI_BTN)
    assert await page.get_attribute(UCI_BTN, "title") == SHOW_UCI
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_short_laptop_overflow_scrolls_with_overlays_aligned(server, make_page):
    """A desktop Play taller than the window scrolls, even with the wheel
    over a docked panel, until the bottom clock row shows; the header stays
    put, and the fixed dock column and side rail scroll along with the board
    and clocks."""
    _ctx, page, errors = await _new_page(make_page, OVERFLOW_VIEWPORT)
    await _goto_play(page, server)
    await page.dispatch_event(PV_BTN, "click")
    await page.wait_for_selector(f"{DOCK_LEFT} .dock-slot")
    before = await page.evaluate(_GEOM_JS)
    assert before["clockBot"] > before["viewH"], before
    # The dock runs past the window bottom: the footer covers it there.
    assert await page.evaluate(_FOOTER_COVERS_DOCK_JS), "dock paints over the footer"

    await page.mouse.move(*await _center(page, f"{DOCK_LEFT} .dock-slot-body"))
    await page.mouse.wheel(0, WHEEL_PX)
    await page.wait_for_function(
        f"() => ({_GEOM_JS})().clockTop < {before['clockTop']}",
    )
    await page.wait_for_function(
        f"""() => {{ const g = ({_GEOM_JS})();
          return g.clockBot <= g.viewH && g.headerTop === 0
            && Math.abs(g.dockOffset - {before['dockOffset']}) <= 1
            && Math.abs(g.railOffset - {before['railOffset']}) <= 1; }}""",
    )
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_short_touch_viewport_stacks_mobile(server, make_page):
    """Same short viewport on a touch device (landscape phone) stays mobile."""
    _ctx, page, errors = await _new_page(make_page, SHORT_VIEWPORT, has_touch=True)
    await _goto_play(page, server)
    assert await page.evaluate(f"() => matchMedia('{COARSE_POINTER_MQ}').matches")
    assert not await _dock_displayed(page)
    assert_no_page_errors(errors)


# Pointer moves for the drag to the dock (enough past the dock threshold).
DOCK_DRAG_STEPS = 12


async def _center(page, sel):
    return await page.locator(sel).evaluate(
        "(el) => { const r = el.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; }"
    )


@pytest.mark.asyncio
async def test_drag_maximized_float_onto_dock_docks(server, make_page):
    """A maximized floating window dragged by its title bar un-maximizes under
    the pointer and stays dockable: the drop ghost shows over the dock and
    releasing there docks it."""
    _ctx, page, errors = await _new_page(make_page)
    await _goto_play(page, server)
    await page.click(PV_BTN)
    await page.click(UCI_BTN)
    await page.wait_for_selector(f"{DOCK_LEFT} .dock-slot")
    await _click_slot_undock(page, "UCI Log")
    await page.wait_for_selector(UCI_WB)
    await page.click(f"{UCI_WB} .wb-max")
    await page.wait_for_selector(f"{UCI_WB}.max")
    await wait_winbox_settled(page, UCI_WB)

    x0, y0 = await _center(page, f"{UCI_WB} .wb-title")
    x1, y1 = await _center(page, DOCK_LEFT)
    await page.mouse.move(x0, y0)
    await page.mouse.down()
    await page.mouse.move(x1, y1, steps=DOCK_DRAG_STEPS)
    assert await page.locator(f"{DOCK_LEFT} .dock-ghost").count() == 1, "no drop ghost over the dock"
    await page.mouse.up()

    await page.wait_for_function("() => !document.querySelector('.winbox.sturddle-wb-uci-log')")
    assert "UCI Log" in await _slot_titles(page)
    assert_no_page_errors(errors)
