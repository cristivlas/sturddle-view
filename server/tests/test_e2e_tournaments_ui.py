"""E2E: Tournaments perspective renders correctly (Playwright/Chromium).

Skipped if Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import os
import shutil
import sys

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.engines import EngineRegistry  # noqa: E402
from sturddle_view.tournament.store import STATUS_FAILED, TournamentStore  # noqa: E402

from .conftest import (  # noqa: E402
    REGISTRY_FILE,
    e2e_env,
    make_fake_uci,
    open_arena_workspace,
    pin_arena_tournament_ux,
    run_uvicorn_subprocess,
    wait_for_async_predicate,
    wait_perspective_ready,
)


_STICKY_TOAST_X = "#toast-stack .toast-danger .xgame-toast-x"


def _danger_toast(page):
    return page.locator("#toast-stack .toast-danger")


def _server_env(tmp_path, *, fastchess_path=None):
    """Seed the engine registry on disk and return SV_* env for an
    out-of-process server. When fastchess_path is sys.executable it's a
    real file detect_binary echoes back; fastchess is never spawned."""
    registry = EngineRegistry(path=tmp_path / REGISTRY_FILE)
    for name in ("engine-A", "engine-B"):
        registry.add(name=name, path=make_fake_uci(tmp_path, name))
    env = e2e_env(tmp_path)
    if fastchess_path:
        env["SV_TOURNAMENT_FASTCHESS_PATH"] = fastchess_path
    return env


@pytest.fixture
def server(tmp_path):
    # No fastchess configured -> empty state.
    with run_uvicorn_subprocess(env_overrides=_server_env(tmp_path)) as base:
        yield base


@pytest.mark.asyncio
async def test_tournaments_perspective_smoke(server, make_page):
    base = server

    _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    page.on("console", lambda msg: page_errors.append(f"console.{msg.type}: {msg.text}")
            if msg.type == "error" else None)
    await pin_arena_tournament_ux(page)
    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)

    await page.click('button[data-perspective="engines"]')
    await page.wait_for_selector("#engines-perspective")
    await page.wait_for_selector(".tournaments-panel")

    await page.wait_for_function(
        """() => {
            const e = document.querySelector('.tournaments-empty .empty-message');
            return e && /fastchess not configured/i.test(e.textContent);
        }""",
    )

    disabled = await page.evaluate(
        "() => document.querySelector('.t-new').disabled"
    )
    assert disabled is True

    assert page_errors == [], "JS errors during test:\n" + "\n".join(page_errors)


@pytest.mark.asyncio
async def test_tournaments_perspective_with_existing_tournament(tmp_path, make_page):
    """Boot the server with fastchess configured AND a tournament already
    on disk. The Tournaments tab should render the row with the four
    expected verbs and an 'idle' status badge."""
    env = _server_env(tmp_path, fastchess_path=sys.executable)

    # Pre-seed a tournament on disk.
    TournamentStore(tmp_path / "tournaments").create(
        name="smoke",
        template={"tc": "10+0.1"},
        engines=[{"name": "A", "cmd": "/bin/A"}, {"name": "B", "cmd": "/bin/B"}],
    )

    with run_uvicorn_subprocess(env_overrides=env) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on("console", lambda msg: page_errors.append(
            f"console.{msg.type}: {msg.text}"
        ) if msg.type == "error" else None)
        await pin_arena_tournament_ux(page)
        await page.goto(base + "/")
        await page.wait_for_selector("#play-perspective")
        await wait_perspective_ready(page)
        await page.click('button[data-perspective="engines"]')
        await page.wait_for_selector(".tournament-row")

        # Select the row so ribbon verbs reflect that tournament.
        await page.click('.tournament-row')
        await page.wait_for_selector('.tournament-row.selected')
        row_info = await page.evaluate(
            """() => {
                const row = document.querySelector('.tournament-row');
                const ribbon = document.querySelector('.tournaments-ribbon');
                const labelOf = (sel) => ribbon.querySelector(sel)?.getAttribute('aria-label');
                return {
                    name: row.querySelector('.tournament-name').textContent,
                    status: row.querySelector('.tournament-status').textContent,
                    ribbon_labels: {
                        start: labelOf('.t-start'),
                        stop: labelOf('.t-stop'),
                        workspace: labelOf('.t-workspace'),
                        info: labelOf('.t-info'),
                        remove: labelOf('.t-remove'),
                    },
                    new_button_disabled: document.querySelector('.t-new').disabled,
                };
            }"""
        )
        assert row_info["name"] == "smoke"
        assert row_info["status"].strip() == "idle"
        assert row_info["ribbon_labels"] == {
            "start": "Start tournament",
            "stop": "Stop tournament",
            "workspace": "Open workspace",
            "info": "Tournament details",
            "remove": "Delete tournament",
        }
        assert row_info["new_button_disabled"] is False

        await page.click("#settings-btn")
        await page.wait_for_function(
            """() => document.querySelector('wa-dialog wa-tab[panel="tournament"]')""",
        )
        await page.click('wa-dialog wa-tab[panel="tournament"]')
        await page.wait_for_function(
            """() => document.querySelector('wa-dialog wa-tab-panel[name="tournament"] wa-input[data-key="tc"]')""",
        )
        await page.evaluate(
            """() => {
                const setVal = (k, v) => {
                    const el = document.querySelector(
                        `wa-dialog wa-tab-panel[name="tournament"] wa-input[data-key="${k}"]`
                    );
                    el.value = v;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                };
                setVal('tc', '60+0.6');
                setVal('rounds', '42');
                setVal('games_in_parallel', '4');
            }"""
        )
        await wait_for_async_predicate(
            page,
            """async () => {
                const r = await fetch('/api/tournament-settings');
                const tpl = (await r.json()).default_template || {};
                return tpl.tc === '60+0.6' && tpl.rounds === 42 && tpl.games_in_parallel === 4;
            }""",
        )

        assert page_errors == [], "JS errors during test:\n" + "\n".join(page_errors)


def _vanishing_fastchess(tmp_path):
    """A configured fastchess to delete once the UI has seen it, on a PATH
    with no real one to fall back to. Returns (path, env)."""
    fastchess = tmp_path / "fastchess"
    fastchess.touch(mode=0o755)
    path = os.pathsep.join(
        d for d in os.environ["PATH"].split(os.pathsep)
        if not shutil.which("fastchess", path=d)
    )
    return fastchess, {**_server_env(tmp_path, fastchess_path=str(fastchess)), "PATH": path}


def _seed_tournament(tmp_path):
    return TournamentStore(tmp_path / "tournaments").create(
        name="smoke",
        template={"tc": "10+0.1"},
        engines=[{"name": "A", "cmd": "/bin/A"}, {"name": "B", "cmd": "/bin/B"}],
    )


async def _select_row(page, base):
    await pin_arena_tournament_ux(page)
    await page.goto(base + "/")
    await wait_perspective_ready(page)
    await page.click('button[data-perspective="engines"]')
    await page.click(".tournament-row")
    await page.wait_for_selector(".tournament-row.selected")


async def _assert_gear_opens_tournament_settings(page):
    await _danger_toast(page).locator(".toast-action-btn").click()
    await page.wait_for_function(
        """() => document.querySelector('wa-dialog .settings-tabs')?.active === 'tournament'""",
    )
    await _danger_toast(page).wait_for(state="detached")


@pytest.mark.asyncio
async def test_start_without_fastchess_gear_opens_tournament_settings(tmp_path, make_page):
    """fastchess disappearing after the list loaded fails the start with a
    toast that stays up, doesn't stack, and whose gear lands on Settings >
    Tournament."""
    fastchess, env = _vanishing_fastchess(tmp_path)
    _seed_tournament(tmp_path)

    with run_uvicorn_subprocess(env_overrides=env) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        await _select_row(page, base)

        fastchess.unlink()
        await page.click(".tournaments-ribbon .t-start")
        await _danger_toast(page).wait_for()
        assert "fastchess binary not found" in await _danger_toast(page).text_content()
        await page.locator(_STICKY_TOAST_X).wait_for()

        first = await _danger_toast(page).element_handle()
        await page.click(".tournaments-ribbon .t-start")
        await page.wait_for_function("(el) => !el.isConnected", arg=first)
        assert await _danger_toast(page).count() == 1
        await _assert_gear_opens_tournament_settings(page)


@pytest.mark.asyncio
async def test_launchable_start_retires_fastchess_toast(tmp_path, make_page):
    """Fixing the path without the gear: the next Start clears the toast."""
    fastchess, env = _vanishing_fastchess(tmp_path)
    _seed_tournament(tmp_path)

    with run_uvicorn_subprocess(env_overrides=env) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        await _select_row(page, base)

        fastchess.unlink()
        await page.click(".tournaments-ribbon .t-start")
        await _danger_toast(page).wait_for()

        fastchess.touch(mode=0o755)
        await page.click(".tournaments-ribbon .t-start")
        await page.locator(_STICKY_TOAST_X).wait_for(state="detached")


@pytest.mark.asyncio
async def test_banner_restart_without_fastchess_shows_gear(tmp_path, make_page):
    """The workspace banner's Restart reports the same gear, and stops short
    of the wipe confirm."""
    fastchess, env = _vanishing_fastchess(tmp_path)
    t = _seed_tournament(tmp_path)
    TournamentStore(tmp_path / "tournaments").update_status(
        t.id, STATUS_FAILED,
        last_error={"rc": 1, "stderr_tail": [
            "boom; press Start to restart from scratch (prior games will be discarded).",
        ]},
    )

    with run_uvicorn_subprocess(env_overrides=env) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        await _select_row(page, base)
        await open_arena_workspace(page)

        fastchess.unlink()
        await page.click('.wb-error-banner button[aria-label="Restart"]')
        await _danger_toast(page).wait_for()
        assert "Restart failed" in await _danger_toast(page).text_content()
        assert await page.locator("wa-dialog[no-header]").count() == 0
        await _assert_gear_opens_tournament_settings(page)


@pytest.mark.asyncio
async def test_tournament_workspace_opens_four_windows(tmp_path, make_page):
    """Clicking 'Open workspace' on an idle tournament opens the default
    Clean layout: all four system windows."""
    env = _server_env(tmp_path, fastchess_path=sys.executable)
    TournamentStore(tmp_path / "tournaments").create(
        name="ws-smoke",
        template={"tc": "10+0.1"},
        engines=[{"name": "A", "cmd": "/bin/A"}, {"name": "B", "cmd": "/bin/B"}],
    )

    with run_uvicorn_subprocess(env_overrides=env) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on("console", lambda msg: page_errors.append(
            f"console.{msg.type}: {msg.text}"
        ) if msg.type == "error" else None)
        await pin_arena_tournament_ux(page)
        await page.goto(base + "/")
        await page.wait_for_selector("#play-perspective")
        await wait_perspective_ready(page)
        await page.click('button[data-perspective="engines"]')
        await page.wait_for_selector(".tournament-row")

        await page.click('.tournament-row')
        # Fresh workspace: Clean opens all four system windows.
        await open_arena_workspace(page)
        titles = await page.evaluate(
            """() => [...document.querySelectorAll('.winbox.sturddle-wb .wb-title')]
                        .map(t => t.textContent)"""
        )
        assert any("Standings" in t for t in titles)

        # Assert against the production NO_GAMES_MSG const rather than a
        # hardcoded copy, so a wording change can't silently drift the test.
        await wait_for_async_predicate(
            page,
            """async () => {
                const { NO_GAMES_MSG } = await import('/ui/app/tournament-row.js');
                const el = document.querySelector('.wb-standings .wb-empty');
                return el?.textContent === NO_GAMES_MSG;
            }""",
        )

        await page.evaluate(
            """() => document.querySelectorAll('.winbox.sturddle-wb .wb-close')
                        .forEach(b => b.click())"""
        )
        await page.wait_for_function(
            "() => document.querySelectorAll('.winbox.sturddle-wb').length === 0",
        )
        await page.click('.tournament-row')
        await open_arena_workspace(page)

        assert page_errors == [], "JS errors during test:\n" + "\n".join(page_errors)
