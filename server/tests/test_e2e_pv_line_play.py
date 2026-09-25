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
# 1. e4 e5 2. Nf3 Nc6 3. Bc4 Nf6, white (human) to move.
_ITALIAN_MOVES = ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6"]
_ITALIAN_FEN = "r1bqkb1r/pppp1ppp/2n2n2/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4"

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


def _publish_pv(base, game_id, depth=5, *, pv="1. e4 e5 2. Nf3",
                pv_uci=("e2e4", "e7e5", "g1f3")):
    _publish(base, "engine_info", game_id=game_id, payload={
        "depth": depth, "seldepth": depth, "score": {"cp": 20},
        "nodes": 1000, "nps": 1000,
        "pv": [pv], "pv_uci": list(pv_uci),
    })


def _publish_view_board(base, game_id, *, analyzing=False, editing=False):
    _publish(base, "board_update", game_id=game_id, payload={
        "fen": STARTPOS_FEN, "analyzing": analyzing, "editing": editing,
        "view": _VIEW_AT_START, "human_white": None,
    })


async def _open_search_lines(page, base, fen=STARTPOS_FEN):
    # The ribbon's Search Lines button is hidden in view mode; the open flag
    # makes restoreDebugWindows mount the window on either mode's mount.
    await page.add_init_script(f"localStorage.setItem('{PV_OPEN_KEY}', '1')")
    await page.goto(base + "/")
    await page.wait_for_selector(PLAY_PERSP)
    await wait_perspective_ready(page)
    await page.wait_for_selector(".wb-pvtable-scroll")
    # Replay board + clock so the view's gate reflects the installed HVE.
    httpx.post(f"{base}/game/sync", json={}).raise_for_status()
    await page.wait_for_function(
        "(fen) => document.querySelector('.fen-text')?.textContent?.startsWith(fen.split(' ')[0])",
        arg=fen,
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
async def test_ai_turn_finished_regates_row_showable(server, make_page):
    """A finished AI turn leaves the server in ANALYZING with nothing
    searching: the row must become showable again on the ai_info done
    alone (no board_update follows), and a double-click must play it."""
    base = server
    game_id = _install(base, view_mode=True, view_moves_uci=[])["game_id"]
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_search_lines(page, base)
    _publish_pv(base, game_id)
    await _wait_row(page, showable=True)

    _publish_view_board(base, game_id, analyzing=True)
    await _wait_row(page, showable=False)

    _publish(base, "ai_info", game_id=game_id, payload={"done": True})
    st = await _wait_row(page, showable=True)
    assert st["title"] == SHOWABLE_TOOLTIP, st
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
async def test_row_written_while_unmounted_is_showable_on_return(server, make_page):
    """A PV that streams in while another perspective is up lands in the
    detached Search Lines body with no board to sample; on return the row
    must be resampled against the live board and become playable. A played
    position whose PV starts from a square empty at startpos, so a resample
    against a default (startpos) board would truncate and fail."""
    base = server
    game_id = _install(base, human_white=True, moves_uci=_ITALIAN_MOVES)["game_id"]
    httpx.post(f"{base}/game/pause", json={}).raise_for_status()
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_search_lines(page, base, fen=_ITALIAN_FEN)

    await page.click('button[data-perspective="engines"]')
    await page.wait_for_function("() => !document.querySelector('#play-perspective')")
    _publish_pv(base, game_id, pv="4. Ng5 d5 5. exd5", pv_uci=("f3g5", "d7d5", "e4d5"))
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
