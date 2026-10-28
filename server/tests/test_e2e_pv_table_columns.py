"""E2E: the Search Lines (PV) table's Columns menu (column-header.js).

Eval/Nodes/NPS hide and reorder; Depth always shows; PV stays last, shown
and unmovable. A layout change moves the existing cells, so values keep
updating while hidden. Tables on one layoutKey follow each other live; the
pre-layout px-array widths migrate; the first column carries the Columns
button's width on top of its stored width.

Exercises the module directly via page.evaluate. Skipped if Playwright is
missing.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from .conftest import assert_no_page_errors, e2e_env, run_uvicorn_subprocess, watch_page_errors  # noqa: E402

VIEWPORT = {"width": 1400, "height": 900}
HOST_PX = 600
LAYOUT_KEY = "sv-test:pvtable:layout"
HOST_ATTR = "data-pv-host"
# Above the app chrome (the header would take the clicks).
HOST_Z = 1000
MENU = ".dt-menu:popover-open"
LEGACY_DEPTH_PX = 70
PX_TOLERANCE = 1

# Mount `count` tables on LAYOUT_KEY, each fed one line; exposed on window.
MOUNT = f"""async (count) => {{
  const {{ createPvTable }} = await import('/ui/app/pv-table.js');
  window.__pvt = [];
  for (let i = 0; i < count; i++) {{
    const host = document.createElement('div');
    host.setAttribute('{HOST_ATTR}', String(i));
    host.style.cssText = `position:fixed;z-index:{HOST_Z};top:${{i * {HOST_PX // 2}}}px;left:0;width:{HOST_PX}px;height:{HOST_PX // 2}px`;
    document.body.appendChild(host);
    const t = createPvTable({{ layoutKey: '{LAYOUT_KEY}' }});
    host.appendChild(t.el);
    t.update({{ depth: 3, score: {{ cp: 20 }}, nodes: 1000, nps: 2000 }}, '1. e4 e5');
    window.__pvt.push(t);
  }}
}}"""

# Header labels and first-row cell texts of table `i`.
STATE = f"""(i) => {{
  const host = document.querySelector('[{HOST_ATTR}="' + i + '"]');
  return {{
    labels: Array.from(host.querySelectorAll('.wb-pvtable-head-tbl th .th-label'), (s) => s.textContent),
    cells: Array.from(host.querySelector('.wb-pvtable-tbl tbody tr').cells, (td) => td.textContent),
  }};
}}"""


# The Columns button's px (with margin) and the first column's applied px.
WIDTHS = """() => {
  const btn = document.querySelector('.wb-pvtable-head-tbl .dt-menu-btn');
  return {
    btnPx: btn.getBoundingClientRect().width + parseFloat(getComputedStyle(btn).marginRight),
    depthPx: parseFloat(document.querySelector('.wb-pvtable-head-tbl col').style.width),
  };
}"""


def _menu_item(key, cls):
    return f'{MENU} .dt-menu-col[data-key="{key}"] .{cls}'


async def _mount(page, base, count=1, legacy=None):
    await page.goto(base + "/")
    if legacy is not None:
        await page.evaluate("([k, v]) => localStorage.setItem(k, JSON.stringify(v))", [LAYOUT_KEY, legacy])
    await page.evaluate(MOUNT, count)


async def _open_menu(page, i=0):
    await page.click(f'[{HOST_ATTR}="{i}"] .dt-menu-btn')
    await page.wait_for_selector(MENU)


@pytest.fixture
def server(tmp_path):
    with run_uvicorn_subprocess(env_overrides=e2e_env(tmp_path)) as base:
        yield base


@pytest.mark.asyncio
async def test_hidden_column_keeps_updating_and_returns(server, make_page):
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _mount(page, server)
    await _open_menu(page)
    await page.click(_menu_item("eval", "dt-menu-toggle"))
    s = await page.evaluate(STATE, 0)
    assert s["labels"] == ["Depth", "Nodes", "NPS", "PV"], s
    assert s["cells"][:3] == ["3", "1K", "2K"], s

    # An update while hidden lands in the detached cell.
    await page.evaluate("() => window.__pvt[0].update({ depth: 3, score: { cp: -50 } }, null)")
    await page.click(_menu_item("eval", "dt-menu-toggle"))
    s = await page.evaluate(STATE, 0)
    assert s["labels"] == ["Depth", "Eval", "Nodes", "NPS", "PV"], s
    assert s["cells"][1] == "-0.50", s
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_depth_and_pv_locked_pv_unmovable(server, make_page):
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _mount(page, server)
    await _open_menu(page)
    assert await page.is_disabled(_menu_item("depth", "dt-menu-toggle"))
    assert await page.is_disabled(_menu_item("pv", "dt-menu-toggle"))
    assert await page.locator(_menu_item("pv", "dt-menu-move")).count() == 0
    # Nothing moves past PV: NPS, just ahead of it, can't move down.
    assert await page.is_disabled(f'{_menu_item("nps", "dt-menu-move")}[data-dir="1"]')
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_reorder_moves_existing_cells(server, make_page):
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _mount(page, server)
    await page.evaluate("() => { window.__npsTd = document.querySelector('.wb-pvtable-tbl tbody tr').cells[3]; }")
    await _open_menu(page)
    await page.click(f'{_menu_item("nps", "dt-menu-move")}[data-dir="-1"]')
    s = await page.evaluate(STATE, 0)
    assert s["labels"] == ["Depth", "Eval", "NPS", "Nodes", "PV"], s
    assert s["cells"][:4] == ["3", "0.20", "2K", "1K"], s
    same = await page.evaluate(
        "() => document.querySelector('.wb-pvtable-tbl tbody tr').cells[2] === window.__npsTd")
    assert same, "the NPS cell moved, not rebuilt"
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_tables_on_one_key_follow_each_other(server, make_page):
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _mount(page, server, count=2)
    await _open_menu(page, 0)
    await page.click(_menu_item("nodes", "dt-menu-toggle"))
    other = await page.evaluate(STATE, 1)
    assert other["labels"] == ["Depth", "Eval", "NPS", "PV"], other
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_legacy_widths_migrate_with_button_room(server, make_page):
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _mount(page, server, legacy=[LEGACY_DEPTH_PX, 50, 55, 45])
    # The button room lands once the table has a box (ResizeObserver).
    await page.wait_for_function(
        f"() => ({WIDTHS})().btnPx > 0 && Math.abs(({WIDTHS})().depthPx - {LEGACY_DEPTH_PX} - ({WIDTHS})().btnPx) <= {PX_TOLERANCE}")
    assert_no_page_errors(errors)
