"""E2E: play-mode scrub-and-resume.

Clicking a past move in play mode posts /game/view/start with
suspend:true (entering server view mode at that ply). Navigating
forward to the last ply auto-posts /game/view/resume-play, returning
to the same play game with the same game_id.
"""
from __future__ import annotations

import httpx
import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import (  # noqa: E402
    PageObserver,
    make_searching_fake_uci,
    run_uvicorn_subprocess,
    wait_perspective_ready,
)

_VIEW_FORWARD = "#view-forward"


@pytest.fixture
def server(tmp_path):
    registry_path = tmp_path / "engines.json"
    seed = EngineRegistry(path=registry_path)
    e = seed.add(
        name="FakeEngine",
        path=make_searching_fake_uci(tmp_path, "FakeEngine", bestmove="e7e5", pv="e7e5"),
    )
    seed.select(e.id)
    env = {
        "SV_PGN_DIR": str(tmp_path / "pgn"),
        "SV_TOURNAMENT_ROOT": str(tmp_path / "tournaments"),
        "SV_ENGINE_REGISTRY_PATH": str(registry_path),
        "SV_IMPORTS_DIR": str(tmp_path / "imports"),
        "SV_SETTINGS_FILE": str(tmp_path / "settings.json"),
        "SV_GAME_STATE_PATH": str(tmp_path / "current_game.json"),
    }
    with run_uvicorn_subprocess(env_overrides=env) as base:
        yield base


def _hve_state(base: str) -> dict:
    return httpx.get(f"{base}/_test/hve/state").json()


def _two_plies_visible(p):
    return len(p.get("moves_san") or []) >= 2


def _in_play_mode(p):
    return not p.get("view")


def _viewing_at(cursor: int):
    def pred(p):
        v = p.get("view") or {}
        return bool(v) and v.get("cursor") == cursor
    pred.__name__ = f"viewing_at_{cursor}"
    return pred


async def _setup_play(page, obs):
    """Navigate to /, start a play game (human white), push e2e4, await engine reply."""
    await page.goto("/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)
    await page.evaluate(
        "() => fetch('/game/new', {method:'POST',"
        " headers:{'Content-Type':'application/json'},"
        " body: JSON.stringify({human_white:true, tc:{base:60, inc:0}})}).then(r=>r.text())"
    )
    await page.evaluate("() => fetch('/game/sync', {method:'POST'}).then(r=>r.text())")
    await page.evaluate(
        "() => fetch('/game/move', {method:'POST',"
        " headers:{'Content-Type':'application/json'},"
        " body: JSON.stringify({uci:'e2e4'})}).then(r=>r.text())"
    )
    await obs.wait_board_update(_two_plies_visible)


@pytest.mark.asyncio
async def test_move_click_enters_view_at_ply(server, make_page):
    """Clicking the first (non-last) move cell posts view/start and
    puts the server into view mode at cursor=1."""
    base = server
    _ctx, page = await make_page(base_url=base, viewport={"width": 1600, "height": 1000})
    obs = PageObserver(page)
    obs.track("/game/view/start")

    await _setup_play(page, obs)

    # In play mode, non-last move cells have class "clickable". Click first.
    await page.locator(".move-cell.clickable").first.click()
    await obs.wait_quiet("/game/view/start")

    state = _hve_state(base)
    assert state["viewing"] is True
    assert state["view_cursor"] == 1


@pytest.mark.asyncio
async def test_view_forward_to_last_auto_resumes(server, make_page):
    """Scrubbing forward to the last ply triggers auto-resume: the
    client posts resume-play and the server exits view mode, restoring
    the same game."""
    base = server
    _ctx, page = await make_page(base_url=base, viewport={"width": 1600, "height": 1000})
    obs = PageObserver(page)
    obs.track("/game/view/start")
    obs.track("/game/view/resume-play")

    await _setup_play(page, obs)
    game_id_before = _hve_state(base)["game_id"]

    # Enter view at ply 1 (first clickable move cell = white's e4).
    await page.locator(".move-cell.clickable").first.click()
    # _last_board must be in view mode before we navigate, so wait_board_update
    # for _in_play_mode correctly waits for the resume rather than returning
    # the cached pre-view play board.
    await obs.wait_board_update(_viewing_at(1))

    # Advance one step: cursor 1 -> 2 == total_plies (2); auto-resume fires.
    await page.click(_VIEW_FORWARD)
    # Resume publishes a play-mode board_update (no "view" field).
    await obs.wait_board_update(_in_play_mode)

    state = _hve_state(base)
    assert state["viewing"] is False
    assert state["game_id"] == game_id_before  # same game, not forked
