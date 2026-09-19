"""E2E: Search Lines row showability and double-click play (docs/pv-play-spec.md).

Pure bus-driven: the HVE is installed via /_test/hve/install, board state
is replayed with /game/sync, and engine_info rows are injected through
/_test/ai/publish_event. No engine, no LLM.

Spec, Trigger: a row with >= 2 frames is showable (hover class + tooltip)
whenever canPlayLine holds -- not analyzing, not editing, not the engine's
turn -- and a double-click on it starts a show (row lit as playing). While
analysis is in flight or the engine is to move the row is not showable and
double-click is inert.

Skipped if Playwright is missing.
"""
from __future__ import annotations

import httpx
import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import (  # noqa: E402
    REGISTRY_FILE,
    STARTPOS_FEN,
    assert_no_page_errors,
    e2e_env,
    run_uvicorn_subprocess,
    wait_perspective_ready,
    watch_page_errors,
)

PLAY_PERSP = "#play-perspective"
PV_OPEN_KEY = "sturddle:pvtable:open"
PV_ROW = ".wb-pvtable-tbl tbody tr"
SHOWABLE_CLASS = "wb-pv-showable"
PLAYING_CLASS = "wb-pv-row-playing"
SHOWABLE_TOOLTIP = "Double-click to play line"
VIEWPORT = {"width": 1600, "height": 1000}
# Minimal view-mode board_update at the start position; analyzing/editing
# vary per test.
_VIEW_AT_START = {"cursor": 0, "total_plies": 0}

_ROW_STATE = f"""() => {{
  const tr = document.querySelector('{PV_ROW}');
  if (!tr) return null;
  return {{
    showable: tr.classList.contains('{SHOWABLE_CLASS}'),
    title: tr.getAttribute('title'),
    playing: tr.classList.contains('{PLAYING_CLASS}'),
  }};
}}"""


@pytest.fixture
def server(tmp_path):
    registry_path = tmp_path / REGISTRY_FILE
    seed = EngineRegistry(path=registry_path)
    e = seed.add(name="MyEngine", path="/nonexistent/engine")
    seed.select(e.id)
    env = e2e_env(tmp_path)
    with run_uvicorn_subprocess(env_overrides=env) as base:
        yield base


def _install(base, **payload) -> dict:
    resp = httpx.post(f"{base}/_test/hve/install", json=payload)
    resp.raise_for_status()
    return resp.json()


def _publish(base, kind, *, game_id, payload):
    resp = httpx.post(
        f"{base}/_test/ai/publish_event",
        json={"kind": kind, "game_id": game_id, "payload": payload},
    )
    resp.raise_for_status()


def _publish_pv(base, game_id, depth=5):
    _publish(base, "engine_info", game_id=game_id, payload={
        "depth": depth, "seldepth": depth, "score": {"cp": 20},
        "nodes": 1000, "nps": 1000,
        "pv": ["1. e4 e5 2. Nf3"], "pv_uci": ["e2e4", "e7e5", "g1f3"],
    })


def _publish_view_board(base, game_id, *, analyzing=False, editing=False):
    _publish(base, "board_update", game_id=game_id, payload={
        "fen": STARTPOS_FEN, "analyzing": analyzing, "editing": editing,
        "view": _VIEW_AT_START, "human_white": None,
    })


async def _open_search_lines(page, base):
    # The ribbon's Search Lines button is hidden in view mode; the open flag
    # makes restoreDebugWindows mount the window on either mode's mount.
    await page.add_init_script(f"localStorage.setItem('{PV_OPEN_KEY}', '1')")
    await page.goto(base + "/")
    await page.wait_for_selector(PLAY_PERSP)
    await wait_perspective_ready(page)
    await page.wait_for_selector(".wb-pvtable-tbl")
    # Replay board + clock so the view's gate reflects the installed HVE.
    httpx.post(f"{base}/game/sync", json={}).raise_for_status()
    await page.wait_for_function(
        "(fen) => document.querySelector('.fen-text')?.textContent?.startsWith(fen.split(' ')[0])",
        arg=STARTPOS_FEN,
    )


async def _wait_row(page, *, showable: bool):
    await page.wait_for_function(
        f"(want) => {{ const s = ({_ROW_STATE})(); return !!s && s.showable === want; }}",
        arg=showable,
    )
    return await page.evaluate(_ROW_STATE)


@pytest.mark.asyncio
async def test_play_mode_human_turn_row_showable_and_dblclick_plays(server, make_page):
    base = server
    game_id = _install(base, human_white=True)["game_id"]
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_search_lines(page, base)

    _publish_pv(base, game_id)
    st = await _wait_row(page, showable=True)
    assert st["title"] == SHOWABLE_TOOLTIP, st

    await page.dblclick(PV_ROW)
    await page.wait_for_function(f"() => ({_ROW_STATE})()?.playing === true")
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_view_mode_row_showable_and_dblclick_plays(server, make_page):
    base = server
    game_id = _install(base, view_mode=True, view_moves_uci=[])["game_id"]
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_search_lines(page, base)

    _publish_pv(base, game_id)
    st = await _wait_row(page, showable=True)
    assert st["title"] == SHOWABLE_TOOLTIP, st

    await page.dblclick(PV_ROW)
    await page.wait_for_function(f"() => ({_ROW_STATE})()?.playing === true")
    assert_no_page_errors(errors)


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["analyzing", "editing"])
async def test_board_gate_blocks_row_and_regates_on_clear(server, make_page, gate):
    """Analysis in flight or edit mode: row not showable, double-click inert;
    the gate clearing on a later board_update re-gates it showable."""
    base = server
    game_id = _install(base, view_mode=True, view_moves_uci=[])["game_id"]
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_search_lines(page, base)
    _publish_pv(base, game_id)
    await _wait_row(page, showable=True)

    _publish_view_board(base, game_id, **{gate: True})
    st = await _wait_row(page, showable=False)
    assert st["title"] is None, st
    await page.dblclick(PV_ROW)
    assert (await page.evaluate(_ROW_STATE))["playing"] is False

    _publish_view_board(base, game_id, **{gate: False})
    await _wait_row(page, showable=True)
    await page.dblclick(PV_ROW)
    await page.wait_for_function(f"() => ({_ROW_STATE})()?.playing === true")
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_paused_row_stays_showable_across_perspective_nav(server, make_page):
    """A showable row must still be showable and playable after leaving Play
    and coming back: the Search Lines body survives the nav with its rows.
    Paused so the gate never flips afterwards (the announcement is flip-only),
    which makes a remount that gates the rows wrong stick."""
    base = server
    game_id = _install(base, human_white=True)["game_id"]
    httpx.post(f"{base}/game/pause", json={}).raise_for_status()
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_search_lines(page, base)
    _publish_pv(base, game_id)
    await _wait_row(page, showable=True)

    await page.click('button[data-perspective="engines"]')
    await page.wait_for_function("() => !document.querySelector('#play-perspective')")
    await page.click('button[data-perspective="play"]')
    await page.wait_for_selector(PLAY_PERSP)
    await wait_perspective_ready(page)
    await page.wait_for_selector(PV_ROW)

    st = await _wait_row(page, showable=True)
    assert st["title"] == SHOWABLE_TOOLTIP, st
    await page.dblclick(PV_ROW)
    await page.wait_for_function(f"() => ({_ROW_STATE})()?.playing === true")
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_engine_turn_gates_row(server, make_page):
    base = server
    game_id = _install(base, human_white=True)["game_id"]
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_search_lines(page, base)
    _publish_pv(base, game_id)
    await _wait_row(page, showable=True)

    clock = {"white_time": 300, "black_time": 300, "running": True,
             "paused": False, "analyzing": False}
    _publish(base, "clock_tick", game_id=game_id, payload={**clock, "turn": "black"})
    st = await _wait_row(page, showable=False)
    assert st["title"] is None, st
    await page.dblclick(PV_ROW)
    assert (await page.evaluate(_ROW_STATE))["playing"] is False

    _publish(base, "clock_tick", game_id=game_id, payload={**clock, "turn": "white"})
    await _wait_row(page, showable=True)
    assert_no_page_errors(errors)
