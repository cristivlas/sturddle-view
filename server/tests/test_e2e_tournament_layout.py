"""E2E: Tournament layout is per-tournament, not global (Playwright/Chromium).

Skipped if Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import sys

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.engines import EngineRegistry  # noqa: E402
from sturddle_view.tournament.store import TournamentStore  # noqa: E402

from .conftest import (  # noqa: E402
    pin_arena_tournament_ux,
    run_uvicorn_subprocess,
    wait_perspective_ready,
)


@pytest.fixture
def server(tmp_path):
    # Seed engine registry and two tournaments on disk, then launch an
    # out-of-process server pointed at those paths (SV_* -> Settings).
    # tournament_fastchess_path is sys.executable (a real file detect_binary
    # echoes back); fastchess is never spawned -- the test only drives the UI.
    registry = EngineRegistry(path=tmp_path / "engines.json")
    registry.add(name="engine-A", path=sys.executable)
    registry.add(name="engine-B", path=sys.executable)
    store = TournamentStore(tmp_path / "tournaments")
    for name in ("alpha", "bravo"):
        store.create(
            name=name,
            template={"tc": "10+0.1"},
            engines=[{"name": "A", "cmd": "/bin/A"}, {"name": "B", "cmd": "/bin/B"}],
        )
    env = {
        "SV_PGN_DIR": str(tmp_path / "pgn"),
        "SV_TOURNAMENT_ROOT": str(tmp_path / "tournaments"),
        "SV_ENGINE_REGISTRY_PATH": str(tmp_path / "engines.json"),
        "SV_SETTINGS_FILE": str(tmp_path / "settings.json"),
        "SV_GAME_STATE_PATH": str(tmp_path / "current_game.json"),
        "SV_IMPORTS_DIR": str(tmp_path / "imports"),
        "SV_TOURNAMENT_FASTCHESS_PATH": sys.executable,
    }
    with run_uvicorn_subprocess(env_overrides=env) as base:
        yield base


async def _goto_tournaments(page, base):
    await pin_arena_tournament_ux(page)
    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)
    await page.click('button[data-perspective="engines"]')
    await page.wait_for_selector(".tournament-row")


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


async def _open_workspace(page):
    await page.click('.tournaments-ribbon .t-workspace')
    await page.wait_for_function(
        "() => document.querySelectorAll('.winbox.sturddle-wb').length >= 1",
    )


async def _click_window_menu_tidy(page):
    """Click the Tidy (Clean) layout button directly (no need to open dropdown)."""
    await page.evaluate("() => document.querySelector('.tmb-tidy').click()")


async def _layout_state(page):
    """Return which layout buttons currently have tmb-active class."""
    return await page.evaluate(
        """() => ({
            snap: document.querySelector('.tmb-snap')?.classList.contains('tmb-active'),
            tile: document.querySelector('.tmb-tile')?.classList.contains('tmb-active'),
            tidy: document.querySelector('.tmb-tidy')?.classList.contains('tmb-active'),
        })"""
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

    await _goto_tournaments(page, server)

    # Open workspace for alpha and set Tidy layout.
    await _select_tournament(page, "alpha")
    await _open_workspace(page)
    await _click_window_menu_tidy(page)

    state_alpha = await _layout_state(page)
    assert state_alpha["tidy"] is True, "tidy button should be active after clicking Clean"
    assert state_alpha["snap"] is False
    assert state_alpha["tile"] is False

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
    assert state_bravo["tidy"] is False, "tidy button must not carry over to bravo"
    assert state_bravo["snap"] is False
    assert state_bravo["tile"] is False

    # Switch back to alpha -- workspace auto-reopens; layout should restore.
    await _select_tournament(page, "alpha")
    await page.wait_for_function(
        "() => document.querySelectorAll('.winbox.sturddle-wb').length >= 1",
    )
    state_alpha_back = await _layout_state(page)
    assert state_alpha_back["tidy"] is True, "tidy layout should be restored for alpha"
    assert state_alpha_back["snap"] is False
    assert state_alpha_back["tile"] is False

    assert page_errors == [], "JS errors during test:\n" + "\n".join(page_errors)


@pytest.mark.asyncio
async def test_layout_cleared_after_xclose_reopen(server, make_page):
    """X-closing all windows then reopening via ribbon must not restore the layout."""
    _ctx, page = await make_page(viewport={"width": 1600, "height": 1000})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    page.on("console", lambda msg: page_errors.append(
        f"console.{msg.type}: {msg.text}"
    ) if msg.type == "error" else None)

    await _goto_tournaments(page, server)

    await _select_tournament(page, "alpha")
    await _open_workspace(page)
    await _click_window_menu_tidy(page)

    state = await _layout_state(page)
    assert state["tidy"] is True, "tidy should be active before X-close"

    # X-close all windows (simulates user closing via the WinBox X button).
    await page.evaluate(
        """() => document.querySelectorAll('.winbox.sturddle-wb .wb-close')
                    .forEach(b => b.click())"""
    )
    await page.wait_for_function(
        "() => document.querySelectorAll('.winbox.sturddle-wb').length === 0",
    )

    # Reopen via ribbon -- restoreFromSaved is false (no open windows in snapshot).
    await page.click('.tournaments-ribbon .t-workspace')
    await page.wait_for_function(
        "() => document.querySelectorAll('.winbox.sturddle-wb').length >= 1",
    )

    state_after = await _layout_state(page)
    assert state_after["tidy"] is False, "tidy must not reapply after X-close reopen"
    assert state_after["snap"] is False
    assert state_after["tile"] is False

    assert page_errors == [], "JS errors during test:\n" + "\n".join(page_errors)
