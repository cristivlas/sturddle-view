"""E2E: the scroll-edge tracker's snap-back only resets emptied tables.

markScrollEdges resets a scroller to its origin when its content is empty
-- meant for a rowless table kept wide by its colgroup. Judged on any
zero-height first child, it reset scrollers whose real content followed
that child on every scroll, fighting the user.

Skipped if Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from .conftest import (  # noqa: E402
    e2e_env,
    run_uvicorn_subprocess,
    wait_perspective_ready,
    watch_page_errors,
)

# Desktop: mobile skips edge tracking entirely.
VIEWPORT = {"width": 1400, "height": 900}
SCROLLER_PX = 200
CONTENT_PX = 2000
SCROLL_TO_PX = 100

# Build a scroller from `inner`, scroll it on `axis`, then fire the scroll
# event synchronously so the delegated tracker runs before we read back.
SCROLL_AND_READ = f"""([inner, axis]) => {{
  const el = document.createElement('div');
  el.style.cssText = 'position:fixed;top:0;left:0;overflow:auto;'
    + 'width:{SCROLLER_PX}px;height:{SCROLLER_PX}px';
  el.innerHTML = inner;
  document.body.appendChild(el);
  el[axis] = {SCROLL_TO_PX};
  const before = el[axis];
  el.dispatchEvent(new Event('scroll'));
  const after = el[axis];
  el.remove();
  return {{ before, after }};
}}"""

# Zero-height first child, real content after it.
EMPTY_DIV_THEN_CONTENT = f'<div></div><div style="height:{CONTENT_PX}px"></div>'
# Rowless table held wide (a bare col width doesn't widen a rowless table).
ROWLESS_WIDE_TABLE = f'<table style="width:{CONTENT_PX}px"><tbody></tbody></table>'


async def _scroll_and_read(tmp_path, make_page, inner, axis):
    with run_uvicorn_subprocess(env_overrides=e2e_env(tmp_path)) as base:
        _ctx, page = await make_page(viewport=VIEWPORT)
        errors = watch_page_errors(page)
        await page.goto(base + "/")
        await wait_perspective_ready(page)
        result = await page.evaluate(SCROLL_AND_READ, [inner, axis])
        assert not errors, errors
        return result


@pytest.mark.asyncio
async def test_zero_height_first_child_keeps_scroll(tmp_path, make_page):
    """A non-table zero-height first child must not snap the scroller back."""
    r = await _scroll_and_read(tmp_path, make_page, EMPTY_DIV_THEN_CONTENT, "scrollTop")
    assert r["before"] == SCROLL_TO_PX, r
    assert r["after"] == SCROLL_TO_PX, r


@pytest.mark.asyncio
async def test_rowless_table_snaps_back(tmp_path, make_page):
    """The case the snap-back exists for: a stale offset on an emptied table."""
    r = await _scroll_and_read(tmp_path, make_page, ROWLESS_WIDE_TABLE, "scrollLeft")
    assert r["before"] == SCROLL_TO_PX, r
    assert r["after"] == 0, r
