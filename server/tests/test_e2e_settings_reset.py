"""E2E: the Settings dialog's Reset all settings button.

Covers: the footer button shows only on the Common tab; Cancel leaves the
settings alone; Reset restores server defaults and reloads the page. Also
the tab layout per width: side rail when wide, top tabs below the side-rail
breakpoint, and the dialog body never scrolls sideways in either.

Skipped if Playwright is missing.
"""
from __future__ import annotations

import httpx
import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from .conftest import assert_no_page_errors, e2e_env, run_uvicorn_subprocess, wait_perspective_ready  # noqa: E402

SETTINGS_PATH = "/settings"
PLAYER_NAME_KEY = "player_name"
PLAYER_NAME = "Alyssa P. Hacker"
# Wire value of an unset (default) player name.
PLAYER_NAME_DEFAULT = ""
SETTINGS_BTN = "#settings-btn"
SETTINGS_TABS = "wa-dialog .settings-tabs"
RESET_BTN = "wa-dialog > .settings-reset-row > wa-button.settings-reset-btn"
CONFIRM_DIALOG = "wa-dialog[no-header]"
CONFIRM_BTN = f"{CONFIRM_DIALOG} > wa-button"
RESET_LABEL = "Reset"
CANCEL_LABEL = "Cancel"
COMMON_TAB = "wa-dialog wa-tab[panel='general']"
DISPLAY_TAB = "wa-dialog wa-tab[panel='display']"
PLACEMENT_SIDE = "start"
PLACEMENT_TOP = "top"
WIDE_VIEWPORT = {"width": 1280, "height": 900}
# Below --bp-settings-top-tabs (736px) but above the 480px phone gate.
MID_VIEWPORT = {"width": 600, "height": 900}
# The Settings dialog's WA body part: its scrollable and visible widths.
BODY_WIDTHS_JS = """
  () => {
    const body = document.querySelector('wa-dialog:has(.settings-tabs)')
      .shadowRoot.querySelector('[part~="body"]');
    return { scroll: body.scrollWidth, client: body.clientWidth };
  }
"""


@pytest.fixture
def server(tmp_path):
    with run_uvicorn_subprocess(env_overrides=e2e_env(tmp_path)) as base:
        yield base


async def _new_page(make_page, viewport):
    _ctx, page = await make_page(viewport=viewport)
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    return page, errors


async def _open_settings(page, base):
    """Load the app, open Settings (Common is the start tab) and wait for
    the dialog to show, which the footer Reset button proves."""
    await page.goto(base + "/")
    await wait_perspective_ready(page)
    await page.click(SETTINGS_BTN)
    await page.locator(RESET_BTN).wait_for(state="visible")


def _player_name(base):
    return httpx.get(f"{base}{SETTINGS_PATH}").json()[PLAYER_NAME_KEY]


def _put_player_name(base):
    httpx.put(f"{base}{SETTINGS_PATH}", json={PLAYER_NAME_KEY: PLAYER_NAME}).raise_for_status()


@pytest.mark.asyncio
async def test_reset_button_only_on_common_tab(server, make_page):
    """Leaving Common drops the footer; coming back must re-render it."""
    page, errors = await _new_page(make_page, WIDE_VIEWPORT)
    await _open_settings(page, server)
    reset = page.locator(RESET_BTN)
    await page.click(DISPLAY_TAB)
    await reset.wait_for(state="detached")
    await page.click(COMMON_TAB)
    await reset.wait_for(state="visible")
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_cancel_keeps_settings(server, make_page):
    _put_player_name(server)
    page, errors = await _new_page(make_page, WIDE_VIEWPORT)
    await _open_settings(page, server)
    await page.click(RESET_BTN)
    await page.locator(CONFIRM_BTN, has_text=CANCEL_LABEL).click()
    await page.locator(CONFIRM_DIALOG).wait_for(state="detached")
    assert _player_name(server) == PLAYER_NAME
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_reset_restores_defaults_and_reloads(server, make_page):
    _put_player_name(server)
    page, errors = await _new_page(make_page, WIDE_VIEWPORT)
    await _open_settings(page, server)
    await page.click(RESET_BTN)
    async with page.expect_navigation():
        await page.locator(CONFIRM_BTN, has_text=RESET_LABEL).click()
    await wait_perspective_ready(page)
    assert _player_name(server) == PLAYER_NAME_DEFAULT
    assert_no_page_errors(errors)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("viewport", "placement"),
    [(WIDE_VIEWPORT, PLACEMENT_SIDE), (MID_VIEWPORT, PLACEMENT_TOP)],
)
async def test_tab_layout_never_scrolls_sideways(server, make_page, viewport, placement):
    """The side rail only fits a full-width dialog; narrower windows get top
    tabs so the body never needs a horizontal scrollbar."""
    page, errors = await _new_page(make_page, viewport)
    await _open_settings(page, server)
    assert await page.locator(SETTINGS_TABS).evaluate("(el) => el.placement") == placement
    widths = await page.evaluate(BODY_WIDTHS_JS)
    assert widths["client"] > 0, widths
    assert widths["scroll"] <= widths["client"], widths
    assert_no_page_errors(errors)
