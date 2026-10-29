"""E2E: an engine_info searched for another position never paints the board.

AI tools (play_out lines, test_aim) search positions off the live board.
Their infos carry the searched FEN; the board arrow, eval readout and the
Search Lines table take only infos for the position the board shows. Infos
without a FEN (tournament feed shape) stay trusted.

Bus-driven like test_e2e_multipv_lines: no engine. Skipped if Playwright is
missing.
"""
from __future__ import annotations

import chess
import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from .conftest import assert_no_page_errors, watch_page_errors  # noqa: E402
from .test_e2e_pv_line_play import (  # noqa: E402,F401
    PLAY_PERSP,
    PV_ROW,
    VIEWPORT,
    _install,
    _open_search_lines,
    _publish,
    server,
)

ARROW_SEL = f"{PLAY_PERSP} .cm-chessboard .arrows .arrow"
LIVE_FEN = chess.STARTING_FEN
_AFTER_E4 = chess.Board()
_AFTER_E4.push_uci("e2e4")
OFF_BOARD_FEN = _AFTER_E4.fen()

_STATE = f"""() => ({{
  depths: Array.from(document.querySelectorAll('{PV_ROW}')).map((tr) => tr.cells[0].textContent),
  arrows: document.querySelectorAll('{ARROW_SEL}').length,
}})"""


def _publish_info(base, game_id, *, depth, move, fen):
    _publish(base, "engine_info", game_id=game_id, payload={
        "depth": depth, "score": {"cp": 20}, "nodes": 1000, "nps": 1000,
        "pv": [move], "pv_uci": [move], "fen": fen,
    })


@pytest.mark.asyncio
async def test_off_board_info_is_ignored_live_one_paints(server, make_page):
    base = server
    game_id = _install(base, human_white=True)["game_id"]
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_search_lines(page, base)

    # Live, off-board, live: once the last landed, the off-board one
    # (published before it) was handled too. Depths only rise, so the table
    # keeps every row it took.
    _publish_info(base, game_id, depth=1, move="e2e4", fen=LIVE_FEN)
    _publish_info(base, game_id, depth=7, move="d7d5", fen=OFF_BOARD_FEN)
    _publish_info(base, game_id, depth=8, move="d2d4", fen=LIVE_FEN)
    await page.wait_for_function(f"() => ({_STATE})().depths.includes('8')")
    s = await page.evaluate(_STATE)
    assert s == {"depths": ["8", "1"], "arrows": 1}, s
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_analyze_preview_paints_its_own_search(server, make_page):
    # The model's FEN may carry rights python-chess normalizes away (an en
    # passant "e3" nothing can capture): the preview's search still paints.
    base = server
    game_id = _install(base, human_white=True)["game_id"]
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_search_lines(page, base)

    model_fen = OFF_BOARD_FEN.replace(" - ", " e3 ")
    assert model_fen != OFF_BOARD_FEN
    _publish(base, "ai_tool_call", game_id=game_id, payload={
        "round": 0, "name": "analyze", "input": {"fen": model_fen}, "tool_use_id": "a1",
    })
    _publish_info(base, game_id, depth=3, move="d7d5", fen=OFF_BOARD_FEN)
    await page.wait_for_function(f"() => ({_STATE})().depths.length === 1")
    s = await page.evaluate(_STATE)
    assert s == {"depths": ["3"], "arrows": 1}, s
    assert_no_page_errors(errors)
