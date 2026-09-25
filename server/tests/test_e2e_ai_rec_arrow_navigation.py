"""E2E regression: AI recommendation arrow survives perspective navigation.

The remount rehydrate (GET /game/analysis/replay) redraws the recommendation
arrow, and the BOARD_UPDATE bus handler restores it whenever a board_update
wipes arrows. In view mode, fetchXgameInfo re-applies the cached
board_update DIRECTLY into GameView (to refresh move-list fork glyphs) --
that apply bypasses the bus handler, so it cleared the arrow with nothing
to restore it. Play mode never fetches x-game info, hence the asymmetry.

The x-game fetch races the replay rehydrate; the bug only manifests when
the fetch resolves AFTER the arrow is drawn. The tests pin that ordering by
holding the /recent-imports/by-id response until the arrow is on the board.

Driven by seeding the coordinator's replay buffer while the server holds
ANALYZING (engine-only analysis on a fake engine) -- no real LLM.

Skipped if Playwright is missing.
"""
from __future__ import annotations

import httpx
import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import REGISTRY_FILE, e2e_env, make_searching_fake_uci, run_uvicorn_subprocess  # noqa: E402


PLAY_PERSP = "#play-perspective"
ARROW_SEL = ".cm-chessboard .arrow-secondary"
ALT_ARROW_SEL = ".cm-chessboard .arrow-info"
PLAY_NAV_BTN = "#perspective-nav button[data-perspective='play']"
OTHER_NAV_BTN = "#perspective-nav button[data-perspective='engines']"
XGAME_BY_ID_ROUTE = "**/game/recent-imports/by-id/**"
# Mirrors APP_EVT.XGAME_INFO_APPLIED in web/app/app-events.js.
XGAME_APPLIED_EVT = "sturddle:xgame-info-applied"

_PGN = (
    '[Event "?"]\n[Site "?"]\n[Date "????.??.??"]\n[Round "?"]\n'
    '[White "W"]\n[Black "B"]\n[Result "*"]\n\n'
    '1. e4 e5 2. Nf3 Nc6 *\n'
)


@pytest.fixture
def server(tmp_path):
    registry_path = tmp_path / REGISTRY_FILE
    seed = EngineRegistry(path=registry_path)
    engine_path = make_searching_fake_uci(tmp_path, "FakeEngine")
    e = seed.add(name="FakeEngine", path=engine_path)
    seed.select(e.id)
    env = e2e_env(tmp_path)
    with run_uvicorn_subprocess(env_overrides=env) as base:
        yield base


def _seed_recommendation(base, gid, alternatives=None) -> None:
    # The server stamps the pick's position FEN on the payload; the client's
    # arrow re-apply guard keys on it, so the seed must carry it too.
    fen = httpx.get(f"{base}/_test/hve/state").json()["board_fen"]
    rec = {"uci": "d2d4", "seq": 2, "fen": fen}
    if alternatives:
        rec["alternatives"] = alternatives
    resp = httpx.post(f"{base}/_test/ai/seed_replay", json={"events": [
        {"kind": "ai_info", "game_id": gid,
         "payload": {"delta": "I recommend d4.", "round": 0, "seq": 1}},
        {"kind": "ai_recommendation", "game_id": gid,
         "payload": rec},
        {"kind": "ai_info", "game_id": gid,
         "payload": {"done": True, "seq": 3}},
    ]})
    resp.raise_for_status()


async def _hold_xgame_fetch_until_arrow(page) -> None:
    """Pin the race the bug depends on: release the x-game by-id response
    only once the recommendation arrow is on the board, so its cached
    board_update re-apply always lands after the replay draw."""
    async def handle(route):
        await page.wait_for_selector(ARROW_SEL, state="attached")
        await route.continue_()
    await page.route(XGAME_BY_ID_ROUTE, handle)


async def _count_xgame_applies(page) -> None:
    """Count XGAME_INFO_APPLIED on a window slot from document start, so the
    listener exists before any page script runs (a listener added after goto
    would miss the initial mount's fetch)."""
    await page.add_init_script(f"""
        window.__xgameApplied = 0;
        window.addEventListener('{XGAME_APPLIED_EVT}', () => {{ window.__xgameApplied += 1; }});
    """)


async def _await_xgame_apply(page) -> None:
    """Block until the x-game fetch continuation (json -> cached board_update
    apply -> arrow restore) has run once since the last reset."""
    await page.wait_for_function("() => window.__xgameApplied >= 1")


async def _reset_xgame_apply_count(page) -> None:
    await page.evaluate("() => { window.__xgameApplied = 0; }")


async def _navigate_away_and_back(page) -> None:
    await page.click(OTHER_NAV_BTN)
    await page.wait_for_selector(PLAY_PERSP, state="detached")
    await page.click(PLAY_NAV_BTN)
    await page.wait_for_selector(PLAY_PERSP)


@pytest.mark.asyncio
async def test_view_mode_arrow_survives_navigation(server, make_page):
    base = server
    r = httpx.post(f"{base}/game/import", json={"text": _PGN, "format": "pgn"})
    r.raise_for_status()
    gid = r.json()["game_id"]
    httpx.post(f"{base}/game/analysis/start").raise_for_status()
    _seed_recommendation(base, gid)

    _ctx, page = await make_page(viewport={"width": 1600, "height": 1000})
    await _hold_xgame_fetch_until_arrow(page)
    await _count_xgame_applies(page)

    await page.goto(base + "/")
    await page.wait_for_selector(PLAY_PERSP)
    await _await_xgame_apply(page)
    assert await page.locator(ARROW_SEL).count() > 0, "arrow lost on initial load"

    await _reset_xgame_apply_count(page)
    await _navigate_away_and_back(page)
    await _await_xgame_apply(page)
    assert await page.locator(ARROW_SEL).count() > 0, "arrow lost after navigation"


@pytest.mark.asyncio
async def test_play_mode_arrow_survives_navigation(server, make_page):
    base = server
    r = httpx.post(f"{base}/game/new", json={})
    r.raise_for_status()
    gid = r.json().get("game_id")
    httpx.post(f"{base}/game/pause").raise_for_status()
    httpx.post(f"{base}/game/analysis/start").raise_for_status()
    _seed_recommendation(base, gid)

    _ctx, page = await make_page(viewport={"width": 1600, "height": 1000})
    await page.goto(base + "/")
    await page.wait_for_selector(PLAY_PERSP)
    await page.wait_for_selector(ARROW_SEL, state="attached")

    await _navigate_away_and_back(page)
    await page.wait_for_selector(ARROW_SEL, state="attached")


@pytest.mark.asyncio
async def test_play_mode_alternative_arrows_drawn_and_survive_navigation(server, make_page):
    # Book siblings ride the recommendation: one muted arrow each beside
    # the pick, and the same replay re-apply keeps them across a remount.
    base = server
    r = httpx.post(f"{base}/game/new", json={})
    r.raise_for_status()
    gid = r.json().get("game_id")
    httpx.post(f"{base}/game/pause").raise_for_status()
    httpx.post(f"{base}/game/analysis/start").raise_for_status()
    _seed_recommendation(base, gid, alternatives=["e2e4", "c2c4"])

    _ctx, page = await make_page(viewport={"width": 1600, "height": 1000})
    await page.goto(base + "/")
    await page.wait_for_selector(PLAY_PERSP)
    await page.wait_for_selector(ARROW_SEL, state="attached")
    assert await page.locator(ALT_ARROW_SEL).count() == 2

    await _navigate_away_and_back(page)
    await page.wait_for_selector(ARROW_SEL, state="attached")
    assert await page.locator(ALT_ARROW_SEL).count() == 2
