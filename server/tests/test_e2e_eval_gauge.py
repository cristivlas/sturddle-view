"""E2E: the board-side eval gauge in Play/View.

The gauge's White share is a logistic of the shown score in White's POV
(engine_info carries the score's `pov`). Desktop: vertical on the dock side
of the board, White's end toward White's pieces, the dock yielding room for
it. Mobile: horizontal under the board, White's end left unless flipped.
Mate 0 means the side to move is mated. A search start keeps the last value.
A view on a finished game's final position takes the gauge from the result:
full to the winner, a draw at 50%, whatever the last ply's eval.

Bus-driven like test_e2e_pv_line_play: no engine. Skipped if Playwright is
missing.
"""
from __future__ import annotations

import re

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
WHITE_WINS_SHARE = 100.0
BLACK_WINS_SHARE = 0.0

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


async def _wait_fen(page, fen: str) -> None:
    await page.wait_for_function(
        f"(fen) => document.querySelector('{PLAY_PERSP} .fen-text')?.textContent === fen", arg=fen,
    )


def _import_at(base, pgn: str, ply: int) -> None:
    httpx.post(f"{base}/game/import", json={
        "text": pgn, "format": "pgn", "land_at_ply": ply,
    }).raise_for_status()


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


# Saved HvE games: the engine's plies carry "<eval>/<depth> <time>", the
# human's (the mating move here) only a time. Bare: no comments at all.
_MATE_PGNS = {
    WHITE_MATED_FEN: "1. f3 { -0.50/20 1.0s } 1... e5 { 1.0s } 2. g4 { -9.00/20 1.0s } 2... Qh4# { 1.0s } 0-1",
    BLACK_MATED_FEN: ("1. e4 { 1.0s } 1... e5 { -0.20/20 1.0s } 2. Bc4 { 1.0s } 2... Nc6 { -0.30/20 1.0s }"
                      " 3. Qh5 { 1.0s } 3... Nf6 { -0.40/20 1.0s } 4. Qxf7# { 1.0s } 1-0"),
}
_STRIP_COMMENTS_RE = re.compile(r"\s*\{[^}]*\}")
MATE_PLIES = {WHITE_MATED_FEN: 4, BLACK_MATED_FEN: 7}


def _mate_pgn(fen: str, has_eval: bool) -> str:
    movetext = _MATE_PGNS[fen] if has_eval else _STRIP_COMMENTS_RE.sub("", _MATE_PGNS[fen])
    result = movetext.rsplit(" ", 1)[1]
    return f'[Event "M"]\n[White "W"]\n[Black "B"]\n[Result "{result}"]\n\n{movetext}\n'


# A view's mate position has no eval (the mating ply has none); the game's
# decisive result still fills the gauge for the winner.
@pytest.mark.asyncio
@pytest.mark.parametrize("has_eval", [True, False])
@pytest.mark.parametrize(("fen", "share"), [
    (WHITE_MATED_FEN, BLACK_WINS_SHARE),
    (BLACK_MATED_FEN, WHITE_WINS_SHARE),
])
async def test_view_checkmate_without_eval_fills_for_winner(server, make_page, fen, share, has_eval):
    base = server
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await page.goto(base + "/")
    await page.wait_for_selector(PLAY_PERSP)
    await wait_perspective_ready(page)
    _import_at(base, _mate_pgn(fen, has_eval), MATE_PLIES[fen])
    httpx.post(f"{base}/game/sync", json={}).raise_for_status()
    # Barrier: the FEN and the gauge land in the same board_update handler.
    await _wait_fen(page, fen)
    s = await page.evaluate(_STATE)
    assert not s["empty"] and s["share"] == share, s
    assert_no_page_errors(errors)


# Live game over: the human mates, the client re-imports the saved game and
# lands on the mate in view; the gauge shows the winner there too. An
# installed game has no recents row: the route serves the saved PGN.
@pytest.mark.asyncio
async def test_game_over_by_mate_fills_gauge_in_auto_view(server, make_page):
    base = server
    game_id = _install(base, human_white=True,
                       moves_uci=["e2e4", "e7e5", "f1c4", "b8c6", "d1h5", "g8f6"])["game_id"]
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    saved = {"format": "pgn", "text": _mate_pgn(BLACK_MATED_FEN, True)}
    await page.route(f"**/game/recent-imports/by-id/{game_id}",
                     lambda route: route.fulfill(json=saved))
    await _goto_play(page, base)
    _publish_score(base, game_id, {"cp": ADVANTAGE_CP})
    await _wait_state(page, "!s.empty")
    httpx.post(f"{base}/game/move", json={"uci": "h5f7"}).raise_for_status()
    await page.wait_for_function(
        "() => getComputedStyle(document.querySelector('#view-controls')).display !== 'none'",
    )
    await _wait_fen(page, BLACK_MATED_FEN)
    s = await page.evaluate(_STATE)
    assert not s["empty"] and s["share"] == WHITE_WINS_SHARE, s
    assert_no_page_errors(errors)


# A reload on a view already at the mate: the mount's replayed board_update
# fills the gauge for the winner.
@pytest.mark.asyncio
async def test_reload_on_view_checkmate_fills_gauge(server, make_page):
    base = server
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    _import_at(base, _mate_pgn(BLACK_MATED_FEN, True), MATE_PLIES[BLACK_MATED_FEN])
    await page.goto(base + "/")
    await wait_perspective_ready(page)
    await _wait_fen(page, BLACK_MATED_FEN)
    s = await page.evaluate(_STATE)
    assert not s["empty"] and s["share"] == WHITE_WINS_SHARE, s
    assert_no_page_errors(errors)


# fastchess appends the ending to the last ply's comment; its eval then goes
# unread. The result still sets the gauge: full to the winner, a draw at
# 50% (though its last eval, read here, leans White).
def _adjudicated_pgn(result: str, last_comment: str) -> str:
    return (
        f'[Event "T"]\n[White "W"]\n[Black "B"]\n[Result "{result}"]\n'
        f'[Termination "adjudication"]\n\n'
        f"1. e4 {{ +0.30/20 1.000s }} 1... e5 {{ {last_comment} }} {result}\n"
    )


ADJUDICATED_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
ADJUDICATED_PLIES = 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("result", "last_comment", "share"), [
    ("1-0", "-7.42/22 2.000s, White wins by adjudication", WHITE_WINS_SHARE),
    ("1/2-1/2", "-1.50/22 2.000s", EVEN_SHARE),
])
async def test_view_adjudicated_result_sets_gauge(server, make_page, result, last_comment, share):
    base = server
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    _import_at(base, _adjudicated_pgn(result, last_comment), ADJUDICATED_PLIES)
    await page.goto(base + "/")
    await wait_perspective_ready(page)
    await _wait_fen(page, ADJUDICATED_FEN)
    s = await page.evaluate(_STATE)
    assert not s["empty"] and s["share"] == share, s
    assert_no_page_errors(errors)
