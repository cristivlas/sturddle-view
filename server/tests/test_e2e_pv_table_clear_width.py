"""E2E: clearing a pv-table resets its width and scroll.

fitTableToPvContent sets the table's min-width from the longest PV line.
clear() emptied the rows without re-fitting, so an emptied table stayed wide
and scrolled sideways; a new search's lines, landing before any layout,
kept that stale offset. The tournament live-game panels clear on every
go/bestmove, so they hit it often.

Exercises the module directly via page.evaluate. Skipped if Playwright is
missing.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from .conftest import e2e_env, run_uvicorn_subprocess  # noqa: E402

VIEWPORT = {"width": 1400, "height": 900}
# Wider than the fixed columns, far narrower than the long PV.
HOST_PX = 500
COL_WIDTHS_KEY = "sv-test:pvtable:colWidths"
LONG_PV = " ".join(["Nf3 Nf6 Ng1 Ng8"] * 40)

# Build a table, grow it with one long line, scroll it right, then clear.
# fit is rAF-coalesced; a later rAF callback runs after it in the same frame.
FILL_SCROLL_CLEAR = f"""async () => {{
  const {{ createPvTable }} = await import('/ui/app/pv-table.js');
  const host = document.createElement('div');
  host.style.cssText = 'position:fixed;top:0;left:0;width:{HOST_PX}px;height:{HOST_PX}px';
  document.body.appendChild(host);
  const t = createPvTable({{ colWidthsKey: '{COL_WIDTHS_KEY}' }});
  host.appendChild(t.el);
  const info = {{ depth: 5, seldepth: 5, score: {{ cp: 20 }}, nodes: 1, nps: 1 }};
  t.update(info, '{LONG_PV}');
  await new Promise((r) => requestAnimationFrame(() => r()));
  const scroller = t.el.querySelector('.wb-pvtable-scroll');
  scroller.scrollLeft = scroller.scrollWidth;
  const filled = {{ scrollLeft: scroller.scrollLeft, overflow: scroller.scrollWidth - scroller.clientWidth }};
  t.clear();
  const cleared = {{ scrollLeft: scroller.scrollLeft, overflow: scroller.scrollWidth - scroller.clientWidth }};
  // New search: the long line lands right after the clear, before any layout.
  t.update(info, '{LONG_PV}');
  scroller.scrollLeft = scroller.scrollWidth;
  t.clear();
  t.update(info, '{LONG_PV}');
  await new Promise((r) => requestAnimationFrame(() => r()));
  const refilled = {{ scrollLeft: scroller.scrollLeft }};
  t.dispose();
  host.remove();
  return {{ filled, cleared, refilled }};
}}"""


@pytest.mark.asyncio
async def test_clear_resets_pv_width_and_scroll(tmp_path, make_page):
    with run_uvicorn_subprocess(env_overrides=e2e_env(tmp_path)) as base:
        _ctx, page = await make_page(viewport=VIEWPORT)
        await page.goto(base + "/")
        r = await page.evaluate(FILL_SCROLL_CLEAR)

    assert r["filled"]["overflow"] > 0 and r["filled"]["scrollLeft"] > 0, r
    assert r["cleared"]["overflow"] <= 0, r
    assert r["cleared"]["scrollLeft"] == 0, r
    assert r["refilled"]["scrollLeft"] == 0, r
