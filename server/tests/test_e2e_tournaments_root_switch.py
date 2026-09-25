"""E2E: switching the tournaments folder in Settings.

Covers: Studio's list follows a switch without a page reload (repro: it
kept the old folder's tournaments until a manual reload); Cancel on the
switch confirmation keeps the old folder in the field and on the server.

Skipped if Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import httpx
import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.tournament.store import TOURNAMENTS_DIRNAME, TournamentStore  # noqa: E402

from .conftest import (  # noqa: E402
    TOURNAMENT_UX_KEY,
    TOURNAMENT_UX_STUDIO,
    e2e_env,
    run_uvicorn_subprocess,
    wait_perspective_ready,
    watch_page_errors,
)

VIEWPORT = {"width": 1400, "height": 900}
PERSPECTIVE_LS_KEY = "sturddle:active-perspective"
ENGINES_PERSPECTIVE = "engines"
TOURNAMENT_SETTINGS_PATH = "/api/tournament-settings"
TOURNAMENTS_ROOT_KEY = "tournaments_root"
# Empty sibling of the e2e_env tournaments root.
OTHER_DIR = "other-tournaments"
TOURNEY_ROW = ".studio-tourney-row"
ROOT_BROWSE = 'wa-button[aria-label="Pick tournaments root"]'
ROOT_FIELD = f".settings-tournament-path-row:has({ROOT_BROWSE}) wa-input.path-field"
TOURNAMENT_TAB = 'wa-dialog wa-tab[panel="tournament"]'
PICKER_SELECTED = ".fs-entry.selected"
SELECT_DIR_LABEL = "Select directory"
CONFIRM_DIALOG = "wa-dialog[no-header]"
CONFIRM_BTN = f"{CONFIRM_DIALOG} > wa-button"
SWITCH_LABEL = "Switch"
CANCEL_LABEL = "Cancel"
VALUE_IS_JS = "([sel, want]) => document.querySelector(sel)?.value === want"


def _seed_tournament(tmp_path):
    TournamentStore(tmp_path / TOURNAMENTS_DIRNAME).create(
        name="old-folder",
        template={"tc": "5+0.05"},
        engines=[
            {"name": "engine-A", "cmd": "/bin/a"},
            {"name": "engine-B", "cmd": "/bin/b"},
        ],
    )


def _root(base):
    return httpx.get(f"{base}{TOURNAMENT_SETTINGS_PATH}").json()[TOURNAMENTS_ROOT_KEY]


async def _pick_other_folder(page):
    """Settings -> Tournament -> Browse -> OTHER_DIR; leaves the switch
    confirmation open (the current folder has a tournament)."""
    await page.click("#settings-btn")
    await page.click(TOURNAMENT_TAB)
    await page.click(ROOT_BROWSE)
    await page.wait_for_selector(PICKER_SELECTED)
    await page.locator(".fs-entry", has=page.locator(".fs-name", has_text=OTHER_DIR)).click()
    await page.locator("wa-button", has_text=SELECT_DIR_LABEL).click()


@pytest.mark.asyncio
async def test_studio_list_follows_root_switch(tmp_path, make_page):
    _seed_tournament(tmp_path)
    (tmp_path / OTHER_DIR).mkdir()
    with run_uvicorn_subprocess(env_overrides=e2e_env(tmp_path)) as base:
        _ctx, page = await make_page(viewport=VIEWPORT)
        errors = watch_page_errors(page)
        await page.add_init_script(
            f"localStorage.setItem('{TOURNAMENT_UX_KEY}', '{TOURNAMENT_UX_STUDIO}');"
            f"localStorage.setItem('{PERSPECTIVE_LS_KEY}', '{ENGINES_PERSPECTIVE}')"
        )
        await page.goto(base + "/")
        await page.wait_for_selector(".studio-panel")
        await wait_perspective_ready(page)
        await page.wait_for_selector(TOURNEY_ROW)

        await _pick_other_folder(page)
        await page.locator(CONFIRM_BTN, has_text=SWITCH_LABEL).click()

        # The bug: the old folder's row stayed until a manual reload.
        await page.locator(TOURNEY_ROW).wait_for(state="detached")
        assert errors == [], "JS errors:\n" + "\n".join(errors)


@pytest.mark.asyncio
async def test_cancel_keeps_folder(tmp_path, make_page):
    _seed_tournament(tmp_path)
    (tmp_path / OTHER_DIR).mkdir()
    with run_uvicorn_subprocess(env_overrides=e2e_env(tmp_path)) as base:
        root = _root(base)
        _ctx, page = await make_page(viewport=VIEWPORT)
        errors = watch_page_errors(page)
        await page.goto(base + "/")
        await wait_perspective_ready(page)

        await _pick_other_folder(page)
        await page.locator(CONFIRM_BTN, has_text=CANCEL_LABEL).click()
        await page.locator(CONFIRM_DIALOG).wait_for(state="detached")

        await page.wait_for_function(VALUE_IS_JS, arg=[ROOT_FIELD, root])
        assert _root(base) == root
        assert errors == [], "JS errors:\n" + "\n".join(errors)
