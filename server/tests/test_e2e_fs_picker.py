"""E2E: the fs picker opens where its path field points.

A field with a value opens the picker at the value's parent with the value
preselected, for files and folders alike; directory pickers list folders
only.

Skipped if Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import sys
from pathlib import Path

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
PGN_BROWSE = 'wa-button[aria-label="Pick PGN directory"]'
PGN_FIELD = f".settings-tournament-path-row:has({PGN_BROWSE}) wa-input.path-field"
FASTCHESS_BROWSE = 'wa-button[aria-label="Pick fastchess binary"]'
PLAY_TAB = 'wa-dialog wa-tab[panel="play"]'
TOURNAMENT_TAB = 'wa-dialog wa-tab[panel="tournament"]'
UP_BTN = 'wa-button[aria-label="Up to parent directory"]'
SELECTED_NAME = ".fs-entry.selected .fs-name"
PICKER_PATH = ".fs-picker-path"
SELECT_DIR_LABEL = "Select directory"
# e2e_env points SV_PGN_DIR at tmp_path / PGN_DIR_NAME.
PGN_DIR_NAME = "pgn"
STRAY_FILE = "stray.txt"

LISTED_NAMES_JS = (
    "() => Array.from(document.querySelectorAll('.fs-entry .fs-name'), (e) => e.textContent)"
)
VALUE_IS_JS = "([sel, want]) => document.querySelector(sel)?.value === want"


async def _open_settings(make_page, base):
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)
    await page.click("#settings-btn")
    return page, errors


async def _open_picker(page, browse_sel):
    await page.click(browse_sel)
    await page.wait_for_selector(SELECTED_NAME)


async def _picker_path(page):
    return await page.eval_on_selector(PICKER_PATH, "(el) => el.value")


@pytest.mark.asyncio
async def test_dir_picker_opens_at_parent_with_value_preselected(tmp_path, make_page):
    (tmp_path / PGN_DIR_NAME).mkdir()
    (tmp_path / STRAY_FILE).write_text("")
    listed = tmp_path.resolve()
    with run_uvicorn_subprocess(env_overrides=e2e_env(tmp_path)) as base:
        page, errors = await _open_settings(make_page, base)
        await page.click(PLAY_TAB)
        await _open_picker(page, PGN_BROWSE)

        assert await page.text_content(SELECTED_NAME) == PGN_DIR_NAME
        assert await _picker_path(page) == str(listed)
        assert STRAY_FILE not in await page.evaluate(LISTED_NAMES_JS), (
            "a directory picker must list folders only"
        )

        # The preselected folder arms Select, which returns it.
        await page.locator("wa-button", has_text=SELECT_DIR_LABEL).click()
        await page.wait_for_function(VALUE_IS_JS, arg=[PGN_FIELD, str(listed / PGN_DIR_NAME)])

        # Up climbs from the listed folder.
        await _open_picker(page, PGN_BROWSE)
        await page.click(UP_BTN)
        await page.wait_for_function(VALUE_IS_JS, arg=[PICKER_PATH, str(listed.parent)])
        assert not errors, errors


@pytest.mark.asyncio
async def test_file_picker_opens_at_parent_with_value_preselected(tmp_path, make_page):
    exe = Path(sys.executable)
    env = {**e2e_env(tmp_path), "SV_TOURNAMENT_FASTCHESS_PATH": str(exe)}
    with run_uvicorn_subprocess(env_overrides=env) as base:
        page, errors = await _open_settings(make_page, base)
        await page.click(TOURNAMENT_TAB)
        await _open_picker(page, FASTCHESS_BROWSE)

        assert await page.text_content(SELECTED_NAME) == exe.name
        assert await _picker_path(page) == str(exe.parent.resolve())
        assert not errors, errors
