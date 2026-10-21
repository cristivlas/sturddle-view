"""E2E: Tournament layout is per-tournament, not global (Playwright/Chromium).

Skipped if Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from .conftest import (  # noqa: E402
    MENU_CHECKED,
    goto_arena_tournaments,
    open_arena_workspace,
    run_uvicorn_subprocess,
    seed_tournament,
    tournament_server_env,
)

ENGINES = ("engine-A", "engine-B")


@pytest.fixture
def server(tmp_path):
    # Two unplayed tournaments on disk; fastchess is never spawned -- the
    # test only drives the UI.
    for name in ("alpha", "bravo"):
        seed_tournament(tmp_path, name, ENGINES, [])
    with run_uvicorn_subprocess(env_overrides=tournament_server_env(tmp_path, ENGINES)) as base:
        yield base


async def _select_tournament(page, name):
    """Click the tournament row with the given name."""
    await page.evaluate(
        """(name) => {
            const rows = [...document.querySelectorAll('.tournament-row')];
            const row = rows.find(r => r.querySelector('.tournament-name')?.textContent === name);
            if (!row) throw new Error('tournament not found: ' + name);
            row.click();
        }""",
        name,
    )


async def _click_window_menu_tile(page):
    """Click the Tile layout button directly (no need to open dropdown)."""
    await page.evaluate("() => document.querySelector('.tmb-tile').click()")


async def _layout_state(page):
    """Return which layout buttons are currently checked."""
    return await page.evaluate(
        """(checked) => ({
            snap: document.querySelector('.tmb-snap')?.classList.contains(checked),
            tile: document.querySelector('.tmb-tile')?.classList.contains(checked),
            tidy: document.querySelector('.tmb-tidy')?.classList.contains(checked),
        })""",
        MENU_CHECKED,
    )


@pytest.mark.asyncio
async def test_layout_persists_per_tournament(server, make_page):
    """Layout set for tournament A is saved and restored when switching back from B."""
    _ctx, page = await make_page(viewport={"width": 1600, "height": 1000})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    page.on("console", lambda msg: page_errors.append(
        f"console.{msg.type}: {msg.text}"
    ) if msg.type == "error" else None)

    await goto_arena_tournaments(page, server)

    # First open defaults to Clean; then set Tile.
    await _select_tournament(page, "alpha")
    await open_arena_workspace(page)
    state_fresh = await _layout_state(page)
    assert state_fresh["tidy"] is True, "first open should default to Clean"
    await _click_window_menu_tile(page)

    state_alpha = await _layout_state(page)
    assert state_alpha["tile"] is True, "tile button should be active after clicking Tile"
    assert state_alpha["snap"] is False
    assert state_alpha["tidy"] is False

    # Switch to bravo -- it has no workspace yet, so Window menu may be
    # disabled/absent. Layout buttons should not show alpha's state.
    await _select_tournament(page, "bravo")
    await page.wait_for_function(
        """() => {
            const rows = [...document.querySelectorAll('.tournament-row')];
            return rows.some(r =>
                r.querySelector('.tournament-name')?.textContent === 'bravo' &&
                r.classList.contains('selected')
            );
        }"""
    )
    state_bravo = await _layout_state(page)
    assert state_bravo["tile"] is False, "tile button must not carry over to bravo"
    assert state_bravo["snap"] is False
    assert state_bravo["tidy"] is False

    # Switch back to alpha -- workspace auto-reopens; layout should restore.
    await _select_tournament(page, "alpha")
    await page.wait_for_function(
        "() => document.querySelectorAll('.winbox.sturddle-wb').length >= 1",
    )
    state_alpha_back = await _layout_state(page)
    assert state_alpha_back["tile"] is True, "tile layout should be restored for alpha"
    assert state_alpha_back["snap"] is False
    assert state_alpha_back["tidy"] is False

    assert page_errors == [], "JS errors during test:\n" + "\n".join(page_errors)


@pytest.mark.asyncio
async def test_layout_cleared_after_xclose_reopen(server, make_page):
    """X-closing all windows then reopening via ribbon must not restore the
    layout: the reopen is fresh, so it defaults to Clean."""
    _ctx, page = await make_page(viewport={"width": 1600, "height": 1000})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    page.on("console", lambda msg: page_errors.append(
        f"console.{msg.type}: {msg.text}"
    ) if msg.type == "error" else None)

    await goto_arena_tournaments(page, server)

    await _select_tournament(page, "alpha")
    await open_arena_workspace(page)
    await _click_window_menu_tile(page)

    state = await _layout_state(page)
    assert state["tile"] is True, "tile should be active before X-close"

    # X-close all windows (simulates user closing via the WinBox X button).
    await page.evaluate(
        """() => document.querySelectorAll('.winbox.sturddle-wb .wb-close')
                    .forEach(b => b.click())"""
    )
    await page.wait_for_function(
        "() => document.querySelectorAll('.winbox.sturddle-wb').length === 0",
    )

    # Reopen via ribbon -- restoreFromSaved is false (no open windows in snapshot).
    await open_arena_workspace(page)

    state_after = await _layout_state(page)
    assert state_after["tile"] is False, "tile must not reapply after X-close reopen"
    assert state_after["snap"] is False
    assert state_after["tidy"] is True, "fresh reopen should default to Clean"

    assert page_errors == [], "JS errors during test:\n" + "\n".join(page_errors)
