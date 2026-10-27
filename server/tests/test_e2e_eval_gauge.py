"""E2E: the board-side eval gauge in Play/View.

The gauge's White share is a logistic of the shown score in White's POV
(engine_info carries the score's `pov`). Desktop: vertical on the dock side
of the board, White's end toward White's pieces, the dock yielding room for
it. Mobile: horizontal under the board, White's end left unless flipped.
Mate 0 means the side to move is mated. A search start keeps the last value.

Bus-driven like test_e2e_pv_line_play: no engine. Skipped if Playwright is
missing.
"""
from __future__ import annotations

import httpx
import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.chess.results import SIDE_BLACK, SIDE_WHITE  # noqa: E402

from .conftest import (  # noqa: E402
    assert_no_page_errors,
    wait_perspective_ready,
    watch_page_errors,
)
from .test_e2e_pv_line_play import (  # noqa: E402,F401
    PLAY_PERSP,
    VIEWPORT,
    _install,
    _open_search_lines,
    _publish,
    server,
)

MOBILE_VIEWPORT = {"width": 600, "height": 1000}
EVEN_SHARE = 50.0
ADVANTAGE_CP = 300
# Fool's mate (White mated, White to move) and Scholar's mate (Black mated).
WHITE_MATED_FEN = "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3"
BLACK_MATED_FEN = "r1bqkb1r/pppp1Qpp/2n2n2/4p3/2B1P3/8/PPPP1PPP/RNB1K1NR b KQkq - 0 4"
PX_TOLERANCE = 1
BARRIER_DEPTH = 7

_STATE = f"""() => {{
  const g = document.querySelector('{PLAY_PERSP} .eval-gauge');
  const box = (el) => {{
    const b = el.getBoundingClientRect();
    return {{ left: b.left, right: b.right, top: b.top, bottom: b.bottom, width: b.width }};
  }};
  const share = g.style.getPropertyValue('--white-share');
  return {{
    share: share ? parseFloat(share) : null,
    empty: g.classList.contains('is-empty'),
    gauge: box(g),
    fill: box(g.querySelector('.eval-gauge-fill')),
    board: box(document.querySelector('{PLAY_PERSP} .board')),
    dock: box(document.querySelector('.play-dock-left')),
  }};
}}"""


def _publish_score(base, game_id, score, pov=SIDE_WHITE):
    _publish(base, "engine_info", game_id=game_id, payload={
        "depth": 1, "score": score, "pov": pov, "pv": ["e4"], "pv_uci": ["e2e4"],
    })


def _publish_view_fen(base, game_id, fen):
    _publish(base, "board_update", game_id=game_id, payload={
        "fen": fen, "analyzing": False, "editing": False,
        "view": {"cursor": 0, "total_plies": 0}, "human_white": None,
    })


# Mobile hides Search Lines, so its opener can't serve as the load wait.
async def _goto_play(page, base):
    await page.goto(base + "/")
    await wait_perspective_ready(page)
    httpx.post(f"{base}/game/sync", json={}).raise_for_status()
    # The replayed board_update (not mount) clears board-idle: stream is live.
    await page.wait_for_selector(".play-board-host:not(.board-idle)")


async def _wait_state(page, predicate: str) -> dict:
    await page.wait_for_function(f"() => {{ const s = ({_STATE})(); return {predicate}; }}")
    return await page.evaluate(_STATE)


def _near(a: float, b: float) -> bool:
    return abs(a - b) <= PX_TOLERANCE


@pytest.mark.asyncio
async def test_gauge_share_follows_score_in_whites_pov(server, make_page):
    base = server
    game_id = _install(base, human_white=True)["game_id"]
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_search_lines(page, base)
    s = await page.evaluate(_STATE)
    assert s["empty"] and s["share"] is None, s

    _publish_score(base, game_id, {"cp": 0})
    s = await _wait_state(page, "!s.empty")
    assert s["share"] == EVEN_SHARE, s
    # The same cp from Black's POV is a White deficit.
    _publish_score(base, game_id, {"cp": ADVANTAGE_CP}, pov=SIDE_BLACK)
    await _wait_state(page, f"s.share < {EVEN_SHARE}")
    _publish_score(base, game_id, {"cp": ADVANTAGE_CP})
    await _wait_state(page, f"s.share > {EVEN_SHARE}")
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_search_start_keeps_gauge(server, make_page):
    base = server
    game_id = _install(base, human_white=True)["game_id"]
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_search_lines(page, base)
    _publish_score(base, game_id, {"cp": ADVANTAGE_CP})
    before = await _wait_state(page, "!s.empty")

    _publish(base, "engine_search_start", game_id=game_id, payload={})
    # Barrier: a scoreless info on the same stream lands after the start.
    _publish(base, "engine_info", game_id=game_id, payload={"depth": BARRIER_DEPTH})
    await page.wait_for_function(
        "(d) => document.querySelector('.engine-depth')?.textContent === d",
        arg=str(BARRIER_DEPTH),
    )
    s = await page.evaluate(_STATE)
    assert not s["empty"] and s["share"] == before["share"], (before, s)
    assert_no_page_errors(errors)


@pytest.mark.asyncio
@pytest.mark.parametrize("human_white", [True, False])
async def test_desktop_gauge_beside_board_white_end_toward_white(server, make_page, human_white):
    base = server
    game_id = _install(base, human_white=human_white)["game_id"]
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_search_lines(page, base)
    _publish_score(base, game_id, {"cp": ADVANTAGE_CP})
    # Dock bounds settle on layout events; wait for the yielded room.
    s = await _wait_state(page, "!s.empty && s.dock.right < s.gauge.left")

    assert _near(s["gauge"]["top"], s["board"]["top"]), s
    assert _near(s["gauge"]["bottom"], s["board"]["bottom"]), s
    assert s["gauge"]["right"] < s["board"]["left"], s
    white_end = "bottom" if human_white else "top"
    assert _near(s["fill"][white_end], s["gauge"][white_end]), s
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_desktop_gauge_follows_ribbon_to_the_right(server, make_page):
    base = server
    _install(base, human_white=True)
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_search_lines(page, base)
    await page.evaluate("() => { document.body.dataset.ribbonSide = 'right'; }")
    s = await _wait_state(page, "s.gauge.left > s.board.right")
    assert _near(s["gauge"]["top"], s["board"]["top"]), s
    assert_no_page_errors(errors)


@pytest.mark.asyncio
@pytest.mark.parametrize("human_white", [True, False])
async def test_mobile_gauge_horizontal_under_board(server, make_page, human_white):
    base = server
    game_id = _install(base, human_white=human_white)["game_id"]
    _ctx, page = await make_page(viewport=MOBILE_VIEWPORT)
    errors = watch_page_errors(page)
    await _goto_play(page, base)
    _publish_score(base, game_id, {"cp": ADVANTAGE_CP})
    s = await _wait_state(page, "!s.empty")

    assert s["gauge"]["top"] >= s["board"]["bottom"], s
    assert _near(s["gauge"]["width"], s["board"]["width"]), s
    white_end = "left" if human_white else "right"
    assert _near(s["fill"][white_end], s["gauge"][white_end]), s
    assert_no_page_errors(errors)


@pytest.mark.asyncio
@pytest.mark.parametrize(("fen", "pov", "share"), [
    (WHITE_MATED_FEN, SIDE_WHITE, 0.0),
    (BLACK_MATED_FEN, SIDE_WHITE, 100.0),
    (BLACK_MATED_FEN, SIDE_BLACK, 100.0),
])
async def test_mate_zero_side_to_move_loses(server, make_page, fen, pov, share):
    base = server
    game_id = _install(base, human_white=True)["game_id"]
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_search_lines(page, base)
    _publish_view_fen(base, game_id, fen)
    _publish_score(base, game_id, {"mate": 0}, pov=pov)
    s = await _wait_state(page, "!s.empty")
    assert s["share"] == share, s
    assert_no_page_errors(errors)
