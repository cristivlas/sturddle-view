"""E2E: keyboard navigation in the app's row-selection lists.

Every such list is a single Tab stop whose rows are walked with Arrow
Up/Down + Home/End (wireArrowKeyNav). Focusing one by keyboard selects the
first row, because the container's own focus ring is suppressed and the
highlighted row is the only affordance -- a list you can Tab into but that
shows nothing on arrival reads as unreachable.

Covers the fs picker (opened the real way, from Settings), the Studio
tourney list, the Studio Games history (roving tabindex -- focus is the
selection there), and the opening browser.

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
    TOURNAMENT_UX_KEY,
    TOURNAMENT_UX_STUDIO,
    run_uvicorn_subprocess,
    wait_perspective_ready,
    watch_page_errors,
)

# Tab presses allowed while hunting for a list; well above any real chain.
MAX_TAB_HOPS = 25

PICKER_TABLE = ".fs-picker-table"
PICKER_ROW = ".fs-entry"
TOURNEY_TABLE = ".studio-tourney-tbl"
TOURNEY_ROW = ".studio-tourney-row"
HISTORY_ROW = ".studio-history-row"
OPENINGS_LIST = ".openings-list"
OPENINGS_ROW = "tr.openings-list-item"

SEEDED_TOURNEYS = 3


def _server_env(tmp_path):
    """Engine registry + SV_* env for an out-of-process server (fastchess is
    never spawned -- tournaments are seeded on disk)."""
    registry = EngineRegistry(path=tmp_path / "engines.json")
    for name in ("engine-A", "engine-B"):
        registry.add(name=name, path=sys.executable)
    return {
        "SV_PGN_DIR": str(tmp_path / "pgn"),
        "SV_TOURNAMENT_ROOT": str(tmp_path / "tournaments"),
        "SV_ENGINE_REGISTRY_PATH": str(tmp_path / "engines.json"),
        "SV_SETTINGS_FILE": str(tmp_path / "settings.json"),
        "SV_GAME_STATE_PATH": str(tmp_path / "current_game.json"),
        "SV_IMPORTS_DIR": str(tmp_path / "imports"),
        "SV_TOURNAMENT_FASTCHESS_PATH": sys.executable,
    }


def _seed(tmp_path, count, games=1):
    """`count` finished tournaments, each with `games` played games."""
    store = TournamentStore(tmp_path / "tournaments")
    for i in range(count):
        t = store.create(
            name=f"tourney-{i}",
            template={"tc": "5+0.05"},
            engines=[{"name": e, "cmd": f"/bin/{e}"} for e in ("engine-A", "engine-B")],
        )
        pgn = "".join(
            f'[Event "t"]\n[Round "{g + 1}"]\n[White "engine-A"]\n'
            f'[Black "engine-B"]\n[Result "1-0"]\n\n1. e4 e5 1-0\n\n'
            for g in range(games)
        )
        store.pgn_path(t.id).write_text(pgn, encoding="utf-8")


async def _tab_until(page, predicate):
    """Tab until document.activeElement satisfies `predicate` (a JS boolean
    expression over `el`). Returns True once focused."""
    for _ in range(MAX_TAB_HOPS):
        await page.keyboard.press("Tab")
        found = await page.evaluate(
            f"() => {{ const el = document.activeElement; return !!({predicate}); }}"
        )
        if found:
            return True
    return False


def _row_text(row_sel, cell_sel):
    """JS reading the selected row's label, or null when nothing is selected."""
    return (
        f"() => {{ const r = document.querySelector('{row_sel}.selected');"
        f" return r ? r.querySelector('{cell_sel}').textContent : null; }}"
    )


async def _open_studio(page, base):
    await page.add_init_script(
        f"localStorage.setItem('{TOURNAMENT_UX_KEY}', '{TOURNAMENT_UX_STUDIO}')"
    )
    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)
    await page.click('button[data-perspective="engines"]')
    await page.wait_for_selector(".studio-panel")


@pytest.mark.asyncio
async def test_fs_picker_arrow_navigation(tmp_path, make_page):
    """Tab into the picker's file list, then walk it with the arrows. The
    picker is opened the way a user opens it: Settings -> a path row's Browse."""
    with run_uvicorn_subprocess(env_overrides=_server_env(tmp_path)) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        errors = watch_page_errors(page)
        await page.goto(base + "/")
        await page.wait_for_selector("#play-perspective")
        await wait_perspective_ready(page)

        await page.click("#settings-btn")
        await page.wait_for_selector(".settings-tournament-path-row")
        await page.locator(".settings-tournament-path-row wa-button").first.click()
        await page.wait_for_function(
            f"() => document.querySelectorAll('{PICKER_ROW}').length > 1"
        )

        assert await _tab_until(
            page, f"el.classList && el.classList.contains('{PICKER_TABLE[1:]}')"
        ), "Tab never reached the picker's file list"

        name = _row_text(PICKER_ROW, ".fs-name")
        first = await page.evaluate(name)
        assert first, "focusing the list did not select a row"

        await page.keyboard.press("ArrowDown")
        second = await page.evaluate(name)
        assert second and second != first

        await page.keyboard.press("End")
        last = await page.evaluate(name)
        assert last not in (first, second)

        await page.keyboard.press("Home")
        assert await page.evaluate(name) == first

        assert not errors, errors


@pytest.mark.asyncio
async def test_fs_picker_header_click_selects_nothing(tmp_path, make_page):
    """The focus-selects-first-row rule is keyboard-only: clicking a sort
    header focuses the table too, and must not arm the Select button."""
    with run_uvicorn_subprocess(env_overrides=_server_env(tmp_path)) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        errors = watch_page_errors(page)
        await page.goto(base + "/")
        await page.wait_for_selector("#play-perspective")
        await wait_perspective_ready(page)

        await page.click("#settings-btn")
        await page.wait_for_selector(".settings-tournament-path-row")
        await page.locator(".settings-tournament-path-row wa-button").first.click()
        await page.wait_for_function(
            f"() => document.querySelectorAll('{PICKER_ROW}').length > 1"
        )

        await page.click(f"{PICKER_TABLE} thead th:first-child")
        assert await page.evaluate(_row_text(PICKER_ROW, ".fs-name")) is None
        assert not errors, errors


@pytest.mark.asyncio
async def test_studio_tourney_list_arrow_navigation(tmp_path, make_page):
    """The Studio tourney list is one Tab stop; arrows move the selection and
    the container draws no focus ring of its own (the row highlight is it)."""
    _seed(tmp_path, SEEDED_TOURNEYS)
    with run_uvicorn_subprocess(env_overrides=_server_env(tmp_path)) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        errors = watch_page_errors(page)
        await _open_studio(page, base)
        await page.wait_for_function(
            f"() => document.querySelectorAll('{TOURNEY_ROW}').length"
            f" === {SEEDED_TOURNEYS}"
        )

        assert await _tab_until(
            page, f"el.classList && el.classList.contains('{TOURNEY_TABLE[1:]}')"
        ), "Tab never reached the tourney list"

        assert await page.evaluate(
            f"() => getComputedStyle(document.querySelector('{TOURNEY_TABLE}'))"
            ".outlineStyle"
        ) == "none", "the focused list should not box itself in a focus ring"

        name = _row_text(TOURNEY_ROW, ".studio-tourney-name")
        start = await page.evaluate(name)
        await page.keyboard.press("ArrowDown")
        assert await page.evaluate(name) != start

        await page.keyboard.press("End")
        assert await page.evaluate(name) == f"tourney-{SEEDED_TOURNEYS - 1}"

        await page.keyboard.press("Home")
        assert await page.evaluate(name) == "tourney-0"

        assert not errors, errors


@pytest.mark.asyncio
async def test_studio_history_is_one_tab_stop(tmp_path, make_page):
    """Games history roves a single tabindex=0 instead of making every row a
    Tab stop, so a long list costs one Tab to cross, not one per game."""
    games = 4
    _seed(tmp_path, 1, games=games)
    with run_uvicorn_subprocess(env_overrides=_server_env(tmp_path)) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        errors = watch_page_errors(page)
        await _open_studio(page, base)
        await page.click('.studio-bottom-right wa-tab[panel="history"]')
        await page.wait_for_function(
            f"() => document.querySelectorAll('{HISTORY_ROW}').length === {games}"
        )

        assert await page.evaluate(
            f"() => document.querySelectorAll('{HISTORY_ROW}[tabindex=\"0\"]').length"
        ) == 1, "history should expose exactly one Tab stop"

        # Arrowing moves both focus and the roving stop, so Tab still re-enters
        # the list where the user left it.
        await page.evaluate(f"() => document.querySelector('{HISTORY_ROW}').focus()")
        await page.keyboard.press("ArrowDown")
        assert await page.evaluate(
            f"() => {{ const rows = [...document.querySelectorAll('{HISTORY_ROW}')];"
            " return rows.indexOf(document.activeElement); }"
        ) == 1
        assert await page.evaluate(
            f"() => document.querySelectorAll('{HISTORY_ROW}[tabindex=\"0\"]').length"
        ) == 1

        assert not errors, errors


@pytest.mark.asyncio
async def test_openings_list_arrow_navigation(tmp_path, make_page):
    """The opening browser behaves like the other lists: Tab in selects the
    first opening, arrows walk from there."""
    with run_uvicorn_subprocess(env_overrides=_server_env(tmp_path)) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        errors = watch_page_errors(page)
        await page.goto(base + "/")
        await page.wait_for_selector("#play-perspective")
        await wait_perspective_ready(page)

        await page.click("#import-pos")
        await page.wait_for_selector('wa-tab[panel="openings"]')
        await page.click('wa-tab[panel="openings"]')
        await page.wait_for_function(
            f"() => document.querySelectorAll('{OPENINGS_ROW}').length > 1"
        )

        assert await _tab_until(
            page, f"el.classList && el.classList.contains('{OPENINGS_LIST[1:]}')"
        ), "Tab never reached the openings list"

        name = _row_text(OPENINGS_ROW, ".openings-list-name")
        first = await page.evaluate(name)
        assert first, "focusing the list did not select an opening"

        await page.keyboard.press("ArrowDown")
        assert await page.evaluate(name) != first

        assert not errors, errors
