"""E2E: MultiPV analysis lines in the Search Lines table and on the board.

Single-PV infos show no line numbers. Once a line below the best arrives,
every row carries a numbered circle ahead of its PV (deeper rows first,
best line first within a depth), and the board's arrows switch to numbered
badges -- the plain best arrow renumbered as line 1.

Bus-driven like test_e2e_pv_line_play: no engine. Skipped if Playwright is
missing.
"""
from __future__ import annotations

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

BEST_MOVE = "e2e4"
SECOND_MOVE = "d2d4"
ARROW_SEL = f"{PLAY_PERSP} .cm-chessboard .arrows .arrow"
BADGE_SEL = f"{PLAY_PERSP} .cm-chessboard .arrow-badge text"

_STATE = f"""() => ({{
  rows: Array.from(document.querySelectorAll('{PV_ROW}')).map((tr) => ({{
    depth: tr.cells[0].textContent,
    rank: tr.querySelector('.wb-pv-rank')?.textContent ?? null,
  }})),
  arrows: document.querySelectorAll('{ARROW_SEL}').length,
  badges: Array.from(document.querySelectorAll('{BADGE_SEL}')).map((t) => t.textContent).sort(),
}})"""


def _publish_line(base, game_id, *, depth, move, rank=None):
    payload = {
        "depth": depth, "score": {"cp": 20}, "nodes": 1000, "nps": 1000,
        "pv": [move], "pv_uci": [move],
    }
    if rank is not None:
        payload["multipv"] = rank
    _publish(base, "engine_info", game_id=game_id, payload=payload)


async def _wait_state(page, predicate: str) -> dict:
    await page.wait_for_function(f"() => {{ const s = ({_STATE})(); return {predicate}; }}")
    return await page.evaluate(_STATE)


@pytest.mark.asyncio
async def test_multipv_lines_numbered_in_table_and_on_board(server, make_page):
    base = server
    game_id = _install(base, human_white=True)["game_id"]
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_search_lines(page, base)

    # Best line alone: no numbers anywhere.
    _publish_line(base, game_id, depth=1, move=BEST_MOVE, rank=1)
    s = await _wait_state(page, "s.rows.length === 1 && s.arrows === 1")
    assert s["rows"] == [{"depth": "1", "rank": None}] and s["badges"] == [], s

    # A second line: rows and arrows numbered, the best renumbered as 1.
    _publish_line(base, game_id, depth=1, move=SECOND_MOVE, rank=2)
    s = await _wait_state(page, "s.rows.length === 2 && s.badges.length === 2")
    assert [r["rank"] for r in s["rows"]] == ["1", "2"], s
    assert s["arrows"] == 2 and s["badges"] == ["1", "2"], s

    # Deeper iteration: deeper rows on top, best line first within a depth.
    _publish_line(base, game_id, depth=2, move=SECOND_MOVE, rank=2)
    _publish_line(base, game_id, depth=2, move=BEST_MOVE, rank=1)
    s = await _wait_state(page, "s.rows.length === 4")
    assert s["rows"] == [
        {"depth": "2", "rank": "1"}, {"depth": "2", "rank": "2"},
        {"depth": "1", "rank": "1"}, {"depth": "1", "rank": "2"},
    ], s
    assert s["arrows"] == 2 and s["badges"] == ["1", "2"], s
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_single_pv_infos_stay_unnumbered(server, make_page):
    base = server
    game_id = _install(base, human_white=True)["game_id"]
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_search_lines(page, base)

    _publish_line(base, game_id, depth=1, move=SECOND_MOVE)
    _publish_line(base, game_id, depth=2, move=BEST_MOVE)
    s = await _wait_state(page, "s.rows.length === 2")
    assert s["rows"] == [{"depth": "2", "rank": None}, {"depth": "1", "rank": None}], s
    assert s["arrows"] == 1 and s["badges"] == [], s
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_new_search_clears_previous_numbered_arrows(server, make_page):
    """AI tools search the same position repeatedly: a search start drops the
    last search's lines, so a single-line search leaves one plain arrow."""
    base = server
    game_id = _install(base, human_white=True)["game_id"]
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_search_lines(page, base)

    _publish_line(base, game_id, depth=1, move=BEST_MOVE, rank=1)
    _publish_line(base, game_id, depth=1, move=SECOND_MOVE, rank=2)
    await _wait_state(page, "s.badges.length === 2")

    _publish(base, "engine_search_start", game_id=game_id, payload={})
    _publish_line(base, game_id, depth=1, move=SECOND_MOVE)
    s = await _wait_state(page, "s.arrows === 1 && s.badges.length === 0")
    assert s["arrows"] == 1, s
    assert_no_page_errors(errors)
