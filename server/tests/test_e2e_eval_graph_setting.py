"""E2E: the Engine Eval graph's visibility is a server setting.

play_show_eval_graph replaced the localStorage open flag the eval-graph
window used to carry, so the choice follows the user across browser
profiles instead of living in one. Covers: default-on docks the window,
setting-off keeps it closed, the window's own X writes the setting, that
choice survives into a fresh profile, and the Display-tab switch is the
way back.

The rail slot is display:none until the strip has samples (empty eval),
so every assertion here is DOM-presence based, never visibility.

Skipped if Playwright is missing.
"""
from __future__ import annotations

import httpx
import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from .conftest import run_uvicorn_subprocess, wait_perspective_ready  # noqa: E402


PLAY_PERSP = "#play-perspective"
RAIL_SLOT = ".play-rail-dock .dock-slot"
EVAL_WB = ".winbox.sturddle-wb-evalbar"
EVAL_TITLE = "Engine Eval"
SETTING = "play_show_eval_graph"

# Any eval-graph placement: rail/main dock slot or floating WinBox.
EVAL_PLACED_JS = f"""
  () => !!document.querySelector({EVAL_WB!r})
    || [...document.querySelectorAll('.dock-slot')].some(
         s => s.querySelector('.dock-slot-title')?.textContent === {EVAL_TITLE!r})
"""


@pytest.fixture
def server(tmp_path):
    env = {
        "SV_PGN_DIR": str(tmp_path / "pgn"),
        "SV_TOURNAMENT_ROOT": str(tmp_path / "tournaments"),
        "SV_ENGINE_REGISTRY_PATH": str(tmp_path / "engines.json"),
        "SV_IMPORTS_DIR": str(tmp_path / "imports"),
        "SV_SETTINGS_FILE": str(tmp_path / "settings.json"),
        "SV_GAME_STATE_PATH": str(tmp_path / "current_game.json"),
    }
    with run_uvicorn_subprocess(env_overrides=env) as base:
        yield base


async def _new_page(make_page):
    ctx, page = await make_page(viewport={"width": 1600, "height": 1000})
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(f"console.{msg.type}: {msg.text}")
            if msg.type == "error" else None)
    return ctx, page, errors


def _assert_no_errors(errors):
    benign = ("Failed to load resource",)
    real = [e for e in errors if not any(b in e for b in benign)]
    assert real == [], "JS errors:\n" + "\n".join(real)


def _get_setting(base):
    return httpx.get(f"{base}/settings").json()[SETTING]


def _put_setting(base, value):
    resp = httpx.put(f"{base}/settings", json={SETTING: value})
    resp.raise_for_status()


async def _open_play(page, base):
    """Load Play and wait for mount to finish. Mount awaits its /settings
    GET (which applies play_show_eval_graph) before clearing is-pending, so
    the open/closed decision is final once this returns."""
    await page.goto(base + "/")
    await page.wait_for_selector(PLAY_PERSP)
    await wait_perspective_ready(page)


async def _eval_placed(page):
    return await page.evaluate(EVAL_PLACED_JS)


async def _click_slot_close(page):
    await page.evaluate(
        "(t) => { const s = [...document.querySelectorAll('.dock-slot')]"
        ".find(x => x.querySelector('.dock-slot-title')?.textContent === t);"
        " s?.querySelector('.dock-slot-close')?.click(); }",
        EVAL_TITLE,
    )


@pytest.mark.asyncio
async def test_eval_graph_docks_by_default(server, make_page):
    """Fresh profile, untouched setting -> the graph docks into the rail."""
    _ctx, page, errors = await _new_page(make_page)
    await _open_play(page, server)
    await page.wait_for_function(f"() => !!document.querySelector({RAIL_SLOT!r})")
    assert await _eval_placed(page)
    assert _get_setting(server) is True
    _assert_no_errors(errors)


@pytest.mark.asyncio
async def test_setting_off_keeps_eval_graph_closed(server, make_page):
    """Setting off before mount -> no slot, no WinBox."""
    _put_setting(server, False)
    _ctx, page, errors = await _new_page(make_page)
    await _open_play(page, server)
    assert not await _eval_placed(page)
    _assert_no_errors(errors)


@pytest.mark.asyncio
async def test_slot_close_clears_server_setting(server, make_page):
    """The window's own X closes it AND writes the setting off."""
    _ctx, page, errors = await _new_page(make_page)
    await _open_play(page, server)
    await page.wait_for_function(f"() => !!document.querySelector({RAIL_SLOT!r})")
    await _click_slot_close(page)
    # The PUT is fire-and-forget from the click handler; wait on the server
    # state the click is supposed to produce.
    await page.wait_for_function(
        f"async () => (await (await fetch('/settings')).json()).{SETTING} === false"
    )
    assert not await _eval_placed(page)
    assert _get_setting(server) is False
    _assert_no_errors(errors)


@pytest.mark.asyncio
async def test_closed_state_carries_to_a_fresh_profile(server, make_page):
    """The regression this feature exists for: closing the graph in one
    browser profile must keep it closed in another. A localStorage-backed
    open flag passes every test above and fails this one."""
    _ctx, page, errors = await _new_page(make_page)
    await _open_play(page, server)
    await page.wait_for_function(f"() => !!document.querySelector({RAIL_SLOT!r})")
    await _click_slot_close(page)
    await page.wait_for_function(
        f"async () => (await (await fetch('/settings')).json()).{SETTING} === false"
    )

    # Second context = empty localStorage, same server.
    _ctx2, page2, errors2 = await _new_page(make_page)
    await _open_play(page2, server)
    assert not await _eval_placed(page2), "closed state did not follow the user"
    _assert_no_errors(errors)
    _assert_no_errors(errors2)


@pytest.mark.asyncio
async def test_display_switch_reopens_eval_graph(server, make_page):
    """Display tab's Eval graph switch is the way back after the X."""
    _put_setting(server, False)
    _ctx, page, errors = await _new_page(make_page)
    await _open_play(page, server)
    assert not await _eval_placed(page)

    await page.click("#settings-btn")
    await page.wait_for_selector("wa-dialog wa-tab[panel='display']")
    await page.click("wa-dialog wa-tab[panel='display']")
    switch = page.locator("wa-tab-panel[name='display'] wa-switch", has_text="Eval graph")
    await switch.wait_for(state="visible")
    await switch.click()

    await page.wait_for_function(f"() => !!document.querySelector({RAIL_SLOT!r})")
    await page.wait_for_function(
        f"async () => (await (await fetch('/settings')).json()).{SETTING} === true"
    )
    assert _get_setting(server) is True
    _assert_no_errors(errors)
