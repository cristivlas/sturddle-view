"""E2E: Studio's tab groups are reachable by sequential Tab navigation.

markSelectable() stamps tabindex=-1 on a region so a click gives it focus and
Ctrl+A can scope to it. On a shadow host that also drops the host's entire
slotted subtree out of sequential focus navigation -- which silently made
every wa-tab in Studio's bottom panes unreachable by keyboard (arrow keys
never got a chance, since focus could not enter the group in the first place).

Guards both halves: the tabs are Tab-reachable, and Ctrl+A still scopes to the
region rather than the whole page.

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

# Tab presses before we give up: the walk stops early once focus cycles, so
# this only bounds a pathological run.
MAX_TAB_STOPS = 60

LEFT_TAB = "livegames"
RIGHT_TAB = "tourneys"
LOG_TAB = "log"
LOG_PANE = ".studio-pane-log"

# Active-tab panel name per bottom group -- the one tab stop each group owns.
ACTIVE_TABS = (LEFT_TAB, RIGHT_TAB)

# Panel name of the wa-tab holding focus, or null when focus is elsewhere.
FOCUSED_TAB_PANEL = """() => {
  const el = document.activeElement;
  return el && el.localName === 'wa-tab' ? el.getAttribute('panel') : null;
}"""

GROUP_TABINDEX = """() => Array.from(document.querySelectorAll('wa-tab-group'))
  .map(g => g.getAttribute('tabindex'))"""

# Where Ctrl+A put the selection: the enclosing selectable region, or the bare
# tag name when the selection escaped every region.
SELECTION_REGION = """() => {
  const node = getSelection().anchorNode;
  if (!node) return null;
  const el = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
  const region = el && el.closest('[data-selectable]');
  return region ? region.localName : 'ESCAPED:' + (el ? el.localName : '?');
}"""


def _server_env(tmp_path):
    """Engine registry + SV_* env for an out-of-process server (fastchess is
    never spawned -- we seed the PGN directly)."""
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


def _seed(tmp_path):
    """One finished tournament so the bottom panes have content to render."""
    store = TournamentStore(tmp_path / "tournaments")
    t = store.create(
        name="kbd",
        template={"tc": "5+0.05"},
        engines=[{"name": e, "cmd": f"/bin/{e}"} for e in ("engine-A", "engine-B")],
    )
    store.pgn_path(t.id).write_text(
        '[Event "kbd"]\n[Round "1"]\n[White "engine-A"]\n[Black "engine-B"]\n'
        '[Result "1-0"]\n\n1. e4 e5 1-0\n\n',
        encoding="utf-8",
    )
    return t


async def _open_studio(page, base):
    await page.add_init_script(
        f"localStorage.setItem('{TOURNAMENT_UX_KEY}', '{TOURNAMENT_UX_STUDIO}')"
    )
    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)
    await page.click('button[data-perspective="engines"]')
    await page.wait_for_selector(".studio-panel")
    await page.wait_for_selector(f'.studio-bottom-right wa-tab[panel="{RIGHT_TAB}"]')


async def _tab_reachable_panels(page):
    """Tab from the top of the document, collecting the panel name of every
    wa-tab focus lands on. Stops as soon as focus cycles back to the start."""
    await page.evaluate("() => document.activeElement.blur()")
    reached, first = [], None
    for _ in range(MAX_TAB_STOPS):
        await page.keyboard.press("Tab")
        here = await page.evaluate(
            "() => { const e = document.activeElement;"
            " return e ? e.localName + '#' + (e.id || '') + (e.className || '') : ''; }"
        )
        if first is None:
            first = here
        elif here == first:
            break
        panel = await page.evaluate(FOCUSED_TAB_PANEL)
        if panel:
            reached.append(panel)
    return reached


@pytest.mark.asyncio
async def test_studio_tabs_are_tab_reachable(tmp_path, make_page):
    """Sequential Tab navigation reaches each bottom group's active tab. A
    tabindex on the wa-tab-group host is what breaks this, so pin that too."""
    _seed(tmp_path)
    with run_uvicorn_subprocess(env_overrides=_server_env(tmp_path)) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        errors = watch_page_errors(page)
        await _open_studio(page, base)

        assert await page.evaluate(GROUP_TABINDEX) == [None, None], (
            "a tabindex on the wa-tab-group shadow host removes its whole "
            "slotted subtree from sequential focus navigation"
        )

        reached = await _tab_reachable_panels(page)
        for panel in ACTIVE_TABS:
            assert panel in reached, f"Tab never reached wa-tab[panel={panel}]: {reached}"

        assert not errors, errors


@pytest.mark.asyncio
async def test_studio_ctrl_a_still_scoped_to_region(tmp_path, make_page):
    """The marker tabindex existed so Ctrl+A could scope to a region; dropping
    it on the tab group must not push the selection out to the page."""
    _seed(tmp_path)
    with run_uvicorn_subprocess(env_overrides=_server_env(tmp_path)) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        errors = watch_page_errors(page)
        await _open_studio(page, base)

        await page.click(f'.studio-bottom-right wa-tab[panel="{LOG_TAB}"]')
        await page.wait_for_selector(LOG_PANE)
        await page.click(LOG_PANE)
        await page.keyboard.press("Control+a")

        assert await page.evaluate(SELECTION_REGION) == "wa-tab-group"
        assert not errors, errors
