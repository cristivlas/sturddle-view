"""E2E: the Import dialog's visible tab tracks its state.

wa-tab-group activates its first tab only from an IntersectionObserver
callback; when that callback was missed the dialog opened with no tab and no
panel. It must open on PGN regardless, and a code-driven switch (an .epd file
loaded from the PGN tab) must move the visible tab too.

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

VIEWPORT = {"width": 1400, "height": 900}

IMPORT_BTN = "#import-pos"
TABS = ".import-pos-tabs"
FILE_INPUT = "wa-dialog input[type=file]"
STATUS = ".import-pos-status"
PGN = "pgn"
FEN = "fen"
EPD_FILE = "pos.epd"
EPD_TEXT = "4k3/8/8/8/8/8/8/4K3 w - -"

# An IntersectionObserver that never reports: the missed-callback case.
SILENT_IO_JS = """
window.IntersectionObserver = class {
  observe() {} unobserve() {} disconnect() {} takeRecords() { return []; }
};
"""

# Flag the Import dialog's own wa-after-show (selects inside bubble theirs too).
ARM_SHOWN_JS = """(sel) => {
  window.__importShown = false;
  document.addEventListener('wa-after-show', (e) => {
    if (e.target.querySelector?.(sel)) window.__importShown = true;
  });
}"""

ACTIVE_JS = """(sel) => {
  const g = document.querySelector(sel);
  return {
    tabs: [...g.querySelectorAll('wa-tab')].filter((t) => t.active).map((t) => t.panel),
    panels: [...g.querySelectorAll('wa-tab-panel')].filter((p) => p.active).map((p) => p.name),
  };
}"""


def _panel(fmt):
    return f'{TABS} wa-tab-panel[name="{fmt}"]'


def _textarea(fmt):
    return f"{_panel(fmt)} wa-textarea"


async def _open_import(page, base):
    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)
    await page.evaluate(ARM_SHOWN_JS, TABS)
    await page.click(IMPORT_BTN)
    await page.wait_for_function("() => window.__importShown")


@pytest.mark.asyncio
async def test_import_opens_on_pgn_without_intersection_callback(tmp_path, make_page):
    with run_uvicorn_subprocess(env_overrides=e2e_env(tmp_path)) as base:
        _ctx, page = await make_page(viewport=VIEWPORT)
        errors = watch_page_errors(page)
        await page.add_init_script(SILENT_IO_JS)
        await _open_import(page, base)

        assert await page.evaluate(ACTIVE_JS, TABS) == {"tabs": [PGN], "panels": [PGN]}
        assert await page.locator(_textarea(PGN)).is_visible()
        assert not errors, errors


@pytest.mark.asyncio
async def test_epd_file_from_pgn_tab_switches_visible_tab(tmp_path, make_page):
    with run_uvicorn_subprocess(env_overrides=e2e_env(tmp_path)) as base:
        _ctx, page = await make_page(viewport=VIEWPORT)
        errors = watch_page_errors(page)
        await _open_import(page, base)

        await page.set_input_files(FILE_INPUT, files=[{
            "name": EPD_FILE, "mimeType": "text/plain", "buffer": EPD_TEXT.encode(),
        }])
        await page.wait_for_function(
            f"() => document.querySelector('{STATUS}')?.textContent"
            f" === 'Loaded {EPD_FILE}'"
        )

        assert await page.evaluate(ACTIVE_JS, TABS) == {"tabs": [FEN], "panels": [FEN]}
        assert await page.locator(_textarea(FEN)).is_visible()
        assert not errors, errors
