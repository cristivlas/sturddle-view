"""E2E: the engine-only stand-in Analysis panel.

Engine-only analysis shows a stand-in body pointing at Search Lines, with
the spinner running until Stop. A reload must bring it back: the panel's
persisted open flag used to reopen it blank, and the stand-in only opens
on a closed panel. The session's kind is fixed at its start, so turning AI
on mid-session (the stand-in links to that setting) changes neither; and
the server's kind wins over a start that assumed AI from a stale setting.

Driven by starting engine-only analysis on a fake engine, then loading
the page. Skipped if Playwright is missing.
"""
from __future__ import annotations

import httpx
import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.config import AI_ENABLED_KEY  # noqa: E402

from .conftest import serve_fake_engine, wait_perspective_ready  # noqa: E402


PLAY_PERSP = "#play-perspective"
_VIEWPORT = {"width": 1600, "height": 1000}
_STAND_IN = ".play-ai-engine-only"
_STAND_IN_LEAD = "Engine-only analysis: see "
_STATUS_TEXT = ".play-ai-status-text"
_SPINNER = ".play-ai-status wa-spinner"
_ENGINE_SEARCH = "Running engine search..."
_PV_OPEN_KEY = "sturddle:pvtable:open"
_PV_ROW = ".wb-pvtable-tbl tbody tr"
_ENGINE_INFO = "engine_info"
_ANALYZE_BTN = "#analyze"
# A depth the fake engine never reports: its Search Lines row shows the
# published engine_info has been handled.
_MARKER_DEPTH = 17
_MARKER_SCORE = {"cp": 20}


@pytest.fixture
def server(tmp_path):
    with serve_fake_engine(tmp_path) as base:
        yield base


def _start_engine_only(base) -> str:
    r = httpx.post(f"{base}/game/new", json={})
    r.raise_for_status()
    httpx.post(f"{base}/game/pause").raise_for_status()
    httpx.post(f"{base}/game/analysis/start").raise_for_status()
    return r.json()["game_id"]


async def _wait_stand_in_with_spinner(page) -> None:
    await page.wait_for_function(
        """([standIn, lead, status, text, spinner]) => {
          const p = document.querySelector(standIn);
          const s = document.querySelector(status);
          const sp = document.querySelector(spinner);
          return !!p && p.textContent.startsWith(lead)
            && !!s && s.offsetParent !== null && s.textContent.trim() === text
            && !!sp && sp.offsetParent !== null;
        }""",
        arg=[_STAND_IN, _STAND_IN_LEAD, _STATUS_TEXT, _ENGINE_SEARCH, _SPINNER],
    )


@pytest.mark.asyncio
async def test_engine_only_panel_spins_and_survives_reload(server, make_page):
    base = server
    _start_engine_only(base)

    _ctx, page = await make_page(viewport=_VIEWPORT)
    await page.goto(base + "/")
    await page.wait_for_selector(PLAY_PERSP)
    await _wait_stand_in_with_spinner(page)

    await page.reload()
    await page.wait_for_selector(PLAY_PERSP)
    await _wait_stand_in_with_spinner(page)


@pytest.mark.asyncio
async def test_ai_setting_flip_mid_session_keeps_the_stand_in(server, make_page):
    base = server
    gid = _start_engine_only(base)
    httpx.put(f"{base}/settings", json={AI_ENABLED_KEY: True}).raise_for_status()

    _ctx, page = await make_page(viewport=_VIEWPORT)
    await page.add_init_script(f"localStorage.setItem('{_PV_OPEN_KEY}', '1')")
    await page.goto(base + "/")
    await page.wait_for_selector(PLAY_PERSP)
    await _wait_stand_in_with_spinner(page)

    # The engine's search must not read as an AI turn's.
    fen = httpx.get(f"{base}/_test/hve/state").json()["board_fen"]
    httpx.post(f"{base}/_test/ai/publish_event", json={
        "kind": _ENGINE_INFO, "game_id": gid, "payload": {
            "fen": fen, "depth": _MARKER_DEPTH, "seldepth": _MARKER_DEPTH, "score": _MARKER_SCORE,
        },
    }).raise_for_status()
    await page.wait_for_function(
        "([sel, marker]) => [...document.querySelectorAll(sel)]"
        ".some((tr) => tr.textContent.includes(marker))",
        arg=[_PV_ROW, f"{_MARKER_DEPTH}/{_MARKER_DEPTH}"],
    )
    assert (await page.text_content(_STATUS_TEXT)).strip() == _ENGINE_SEARCH


@pytest.mark.asyncio
async def test_start_on_a_stale_ai_setting_shows_the_stand_in(server, make_page):
    base = server
    httpx.post(f"{base}/game/new", json={}).raise_for_status()
    httpx.post(f"{base}/game/pause").raise_for_status()
    httpx.put(f"{base}/settings", json={AI_ENABLED_KEY: True}).raise_for_status()

    _ctx, page = await make_page(viewport=_VIEWPORT)
    await page.goto(base + "/")
    await wait_perspective_ready(page)
    # Turned off elsewhere: this page still holds AI on and opens the AI
    # panel, but the server starts an engine-only session.
    httpx.put(f"{base}/settings", json={AI_ENABLED_KEY: False}).raise_for_status()
    await page.click(_ANALYZE_BTN)
    await _wait_stand_in_with_spinner(page)
