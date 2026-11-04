"""E2E: an engine_info searched for another position never paints the board.

AI tools (play_out lines, test_aim) search positions off the live board.
Their infos carry the searched FEN; the board arrow and eval readout take
only infos for the position the board shows. Search Lines takes them all:
a row replays from its own searched position. Infos without a FEN
(tournament feed shape) stay trusted. The AI panel's rows name the moves
their searches put on the board.

Bus-driven like test_e2e_multipv_lines: no engine. Skipped if Playwright is
missing.
"""
from __future__ import annotations

import chess
import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from .conftest import assert_no_page_errors, watch_page_errors  # noqa: E402
from .test_e2e_ai_verdict_badge import _seed_replay, _seed_view_mode  # noqa: E402
from .test_e2e_pv_line_play import (  # noqa: E402,F401
    PLAY_PERSP,
    PV_OPEN_KEY,
    PV_ROW,
    VIEWPORT,
    _install,
    _open_search_lines,
    _publish,
    _wait_row,
    server,
)

ARROW_SEL = f"{PLAY_PERSP} .cm-chessboard .arrows .arrow"
_LABEL = f"{PLAY_PERSP} .play-ai-tool-label"
LIVE_FEN = chess.STARTING_FEN
_AFTER_E4 = chess.Board()
_AFTER_E4.push_uci("e2e4")
OFF_BOARD_FEN = _AFTER_E4.fen()

_STATE = f"""() => ({{
  depths: Array.from(document.querySelectorAll('{PV_ROW}')).map((tr) => tr.cells[0].textContent),
  arrows: document.querySelectorAll('{ARROW_SEL}').length,
}})"""


def _publish_info(base, game_id, *, depth, move, fen, pv_uci=None):
    _publish(base, "engine_info", game_id=game_id, payload={
        "depth": depth, "score": {"cp": 20}, "nodes": 1000, "nps": 1000,
        "pv": [move], "pv_uci": pv_uci or [move], "fen": fen,
    })


@pytest.mark.asyncio
async def test_off_board_info_lists_but_never_paints(server, make_page):
    base = server
    game_id = _install(base, human_white=True)["game_id"]
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_search_lines(page, base)

    # Live, off-board, live: once the last landed, the off-board one
    # (published before it) was handled too. Depths only rise, so the table
    # keeps every row it took; only the live ones paint an arrow.
    _publish_info(base, game_id, depth=1, move="e2e4", fen=LIVE_FEN)
    _publish_info(base, game_id, depth=7, move="d7d5", fen=OFF_BOARD_FEN)
    _publish_info(base, game_id, depth=8, move="d2d4", fen=LIVE_FEN)
    await page.wait_for_function(f"() => ({_STATE})().depths.includes('8')")
    s = await page.evaluate(_STATE)
    assert s == {"depths": ["8", "7", "1"], "arrows": 1}, s
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_off_board_row_replays_from_its_own_position(server, make_page):
    # ...d5 exd5 after 1.e4: the capture needs the e4 pawn, so a replay
    # walked from the live (start) board would stop before it.
    base = server
    game_id = _install(base, human_white=True)["game_id"]
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_search_lines(page, base)

    _publish_info(base, game_id, depth=5, move="d5 exd5", fen=OFF_BOARD_FEN,
                  pv_uci=["d7d5", "e4d5"])
    await _wait_row(page, showable=True)
    await page.dblclick(PV_ROW)
    await page.wait_for_selector(
        f"{PLAY_PERSP} .cm-chessboard g[data-square='d5'][data-piece='wp']"
    )
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_finished_step_keeps_its_arrow_until_the_tool_finishes(server, make_page):
    # play_out's "engine best" step searches the live position; its arrow
    # stays under the finished step row while the tool searches on.
    base = server
    game_id = _install(base, human_white=True)["game_id"]
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_search_lines(page, base)

    _publish(base, "ai_tool_call", game_id=game_id, payload={
        "round": 0, "name": "play_out", "input": {"idea": "i", "move": "e4"},
        "tool_use_id": "p1",
    })
    _publish(base, "ai_tool_call", game_id=game_id, payload={
        "round": 0, "name": "engine_best", "input": {"depth": 3},
        "tool_use_id": "p1-engine_best-1", "parent_tool_use_id": "p1",
    })
    _publish_info(base, game_id, depth=3, move="d2d4", fen=LIVE_FEN)
    await page.wait_for_function(f"() => ({_STATE})().arrows === 1")
    _publish(base, "ai_tool_call_complete", game_id=game_id, payload={
        "round": 0, "name": "engine_best", "tool_use_id": "p1-engine_best-1",
        "output": {"san": "d4"},
    })
    # An off-board info published after the step's complete: once it is
    # listed, the complete was handled too.
    _publish_info(base, game_id, depth=4, move="d7d5", fen=OFF_BOARD_FEN)
    await page.wait_for_function(f"() => ({_STATE})().depths.includes('4')")
    assert (await page.evaluate(_STATE))["arrows"] == 1
    _publish(base, "ai_tool_call_complete", game_id=game_id, payload={
        "round": 0, "name": "play_out", "tool_use_id": "p1", "output": {},
    })
    await page.wait_for_function(f"() => ({_STATE})().arrows === 0")
    assert_no_page_errors(errors)


async def _running_rows(base, make_page, rows):
    """Seed a turn whose `rows` (name, tool_use_id, input, parent) are
    dispatched and unfinished; returns (game_id, page, errors)."""
    game_id = _seed_view_mode(base)
    _seed_replay(base, [
        {"kind": "ai_tool_call", "game_id": game_id,
         "payload": {"name": name, "tool_use_id": tu, "input": input_,
                     "parent_tool_use_id": parent, "round": 0, "seq": seq}}
        for seq, (name, tu, input_, parent) in enumerate(rows, start=1)
    ])
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await page.add_init_script(f"localStorage.setItem('{PV_OPEN_KEY}', '1')")
    await page.goto(base + "/")
    await page.locator(_LABEL).nth(len(rows) - 1).wait_for()
    return game_id, page, errors


def _publish_san_line(base, game_id, depth, pv):
    # A view-mode import opens at the start position.
    board = chess.Board()
    pv_uci = [board.push_san(tok).uci() for tok in pv.split() if not tok[0].isdigit()]
    _publish_info(base, game_id, depth=depth, move=pv, fen=LIVE_FEN, pv_uci=pv_uci)


@pytest.mark.asyncio
async def test_top_moves_row_names_its_candidates(server, make_page):
    # top_moves' arrows show its candidates: its row must name them.
    _game_id, page, errors = await _running_rows(server, make_page, [
        ("top_moves", "t1", {"moves": ["Bb5", " d4 "]}, None),
    ])
    assert (await page.locator(_LABEL).text_content()).endswith(" Bb5, d4")
    assert_no_page_errors(errors)


async def _label_ends(page, index, suffix):
    await page.wait_for_function(
        "([sel, i, s]) => document.querySelectorAll(sel)[i]?.textContent.endsWith(s)",
        arg=[_LABEL, index, suffix],
    )


@pytest.mark.asyncio
async def test_running_row_names_the_move_its_search_shows(server, make_page):
    # The engine's own best has no name until found: the running row shows
    # it as the search goes, and keeps the last one once finished.
    base = server
    game_id, page, errors = await _running_rows(base, make_page, [
        ("play_out", "p1", {"idea": "i", "move": "e4"}, None),
        ("engine_best", "p1-engine_best-1", {"depth": 3}, "p1"),
    ])
    _publish_san_line(base, game_id, 1, "1. d4 d5")
    await _label_ends(page, 1, ": d4")
    _publish_san_line(base, game_id, 2, "1. Nf3 d5")
    await _label_ends(page, 1, ": Nf3")
    _publish(base, "ai_tool_call_complete", game_id=game_id, payload={
        "round": 0, "name": "engine_best", "tool_use_id": "p1-engine_best-1",
        "output": {"san": "Nf3"},
    })
    # The parent's own move: its label already names it, so it stays as is.
    _publish_san_line(base, game_id, 3, "1. e4 e5")
    await page.wait_for_function(
        f"() => document.querySelector('{PV_ROW}')?.cells[0].textContent === '3'"
    )
    labels = await page.locator(_LABEL).all_text_contents()
    assert labels[1].endswith(": Nf3"), labels
    assert labels[0].endswith(" e4"), labels
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
