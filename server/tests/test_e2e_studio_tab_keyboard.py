"""E2E: Studio's tab groups are reachable by sequential Tab navigation.

markSelectable() stamps tabindex=-1 on a region so a click gives it focus and
Ctrl+A can scope to it. On a shadow host that also drops the host's entire
slotted subtree out of sequential focus navigation -- which silently made
every wa-tab in Studio's bottom panes unreachable by keyboard (arrow keys
never got a chance, since focus could not enter the group in the first place).

Guards both halves: the tabs are Tab-reachable, and Ctrl+A in the Event Log
selects the log's contents even when focus did not come from the tab group.

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
    REGISTRY_FILE,
    TOURNAMENT_UX_KEY,
    TOURNAMENT_UX_STUDIO,
    e2e_env,
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
LOG_LIST = ".studio-pane-log .wb-eventlog-list"
STUDIO_TAB_RIGHT_KEY = "sturddle:studio:tabRight"

# The seeded tourney's in-memory event history is empty, so stub the backfill.
LOG_LINES = ("fastchess line one", "fastchess line two")
EVENTS_ROUTE = "**/events"
STUB_EVENTS = {"events": [
    {"kind": "tournament_update",
     "payload": {"kind": "runner_log", "stream": "out", "line": line, "_seq": i + 1}}
    for i, line in enumerate(LOG_LINES)
]}

# Active-tab panel name per bottom group -- the one tab stop each group owns.
ACTIVE_TABS = (LEFT_TAB, RIGHT_TAB)

# Panel name of the wa-tab holding focus, or null when focus is elsewhere.
FOCUSED_TAB_PANEL = """() => {
  const el = document.activeElement;
  return el && el.localName === 'wa-tab' ? el.getAttribute('panel') : null;
}"""

GROUP_TABINDEX = """() => Array.from(document.querySelectorAll('wa-tab-group'))
  .map(g => g.getAttribute('tabindex'))"""

# Selected text, and whether the selection stays inside the log list.
LOG_SELECTION = f"""() => {{
  const sel = getSelection();
  if (!sel.rangeCount) return null;
  const list = document.querySelector('{LOG_LIST}');
  return {{ text: sel.toString(), inList: list.contains(sel.getRangeAt(0).commonAncestorContainer) }};
}}"""


def _server_env(tmp_path):
    """Engine registry + SV_* env for an out-of-process server (fastchess is
    never spawned -- we seed the PGN directly)."""
    registry = EngineRegistry(path=tmp_path / REGISTRY_FILE)
    for name in ("engine-A", "engine-B"):
        registry.add(name=name, path=sys.executable)
    return {
        **e2e_env(tmp_path),
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
async def test_studio_event_log_ctrl_a_selects_log(tmp_path, make_page):
    """Studio reopened on a persisted Event Log tab: no wa-tab ever took
    focus, so clicking a log line then Ctrl+A must still select the log."""
    _seed(tmp_path)
    with run_uvicorn_subprocess(env_overrides=_server_env(tmp_path)) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        errors = watch_page_errors(page)
        await page.route(EVENTS_ROUTE, lambda route: route.fulfill(json=STUB_EVENTS))
        await page.add_init_script(
            f"localStorage.setItem('{STUDIO_TAB_RIGHT_KEY}', '{LOG_TAB}')"
        )
        await _open_studio(page, base)

        line = page.locator(f"{LOG_LIST} li", has_text=LOG_LINES[0])
        await line.click()
        await page.keyboard.press("Control+a")

        sel = await page.evaluate(LOG_SELECTION)
        assert sel and sel["inList"], sel
        for text in LOG_LINES:
            assert text in sel["text"], sel
        assert not errors, errors
