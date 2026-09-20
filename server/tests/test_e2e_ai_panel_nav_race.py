"""E2E regression: AI panel must not float over another perspective.

Play's mount fires GET /game/analysis/replay and opens the AI panel when
the buffer is non-empty (the buffer outlives a finished turn by design, so
a reload rehydrates it). If the user leaves Play before that GET resolves,
unmount has already closed the panel and dropped its inline/dock hosts, so
the late open falls through to a floating WinBox over the next perspective.
On a phone this fires by itself: a dropped socket bounces Tournaments ->
Play -> Tournaments, and the wake-up GET lands after the bounce back.

Pins the race by holding the replay response until Play is detached.

Skipped if Playwright is missing.
"""
from __future__ import annotations

import httpx
import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import REGISTRY_FILE, e2e_env, run_uvicorn_subprocess  # noqa: E402


PLAY_PERSP = "#play-perspective"
OTHER_NAV_BTN = "#perspective-nav button[data-perspective='engines']"
REPLAY_ROUTE = "**/game/analysis/replay"
AI_WB = ".winbox.sturddle-wb-ai"
# Mirrors APP_EVT.AI_REHYDRATED in web/app/app-events.js.
AI_REHYDRATED_EVT = "sturddle:ai-rehydrated"

_PGN = (
    '[Event "?"]\n[Site "?"]\n[Date "????.??.??"]\n[Round "?"]\n'
    '[White "W"]\n[Black "B"]\n[Result "*"]\n\n'
    '1. e4 e5 2. Nf3 Nc6 *\n'
)


def _replay_events(game_id: str) -> list[dict]:
    return [
        {"kind": "ai_info", "game_id": game_id,
         "payload": {"delta": "The knight eyes d4.", "round": 0, "seq": 1}},
        {"kind": "ai_info", "game_id": game_id,
         "payload": {"done": True, "seq": 2}},
    ]


@pytest.fixture
def server(tmp_path):
    registry_path = tmp_path / REGISTRY_FILE
    seed = EngineRegistry(path=registry_path)
    e = seed.add(name="MyEngine", path="/nonexistent/engine")
    seed.select(e.id)
    env = e2e_env(tmp_path)
    with run_uvicorn_subprocess(env_overrides=env) as base:
        yield base


def _seed(base) -> None:
    resp = httpx.post(f"{base}/game/import", json={"text": _PGN, "format": "pgn"})
    resp.raise_for_status()
    game_id = resp.json()["game_id"]
    resp = httpx.post(f"{base}/_test/ai/seed_replay", json={"events": _replay_events(game_id)})
    resp.raise_for_status()


async def _hold_replay_until_play_detached(page) -> None:
    async def handle(route):
        await page.wait_for_selector(PLAY_PERSP, state="detached")
        await route.continue_()
    await page.route(REPLAY_ROUTE, handle)


async def _arm_rehydrated_signal(page) -> None:
    """Resolve once rehydrate's finally fires AI_REHYDRATED -- the page has
    finished handling the replay response by then, whatever it did with it."""
    await page.evaluate(f"""() => {{
        window.__aiRehydrated = new Promise((resolve) =>
            window.addEventListener('{AI_REHYDRATED_EVT}', resolve, {{ once: true }}));
    }}""")


@pytest.mark.asyncio
async def test_late_replay_does_not_float_ai_panel_over_next_perspective(server, make_page):
    base = server
    _seed(base)

    _ctx, page = await make_page(viewport={"width": 1600, "height": 1000})
    await _hold_replay_until_play_detached(page)

    await page.goto(base + "/")
    await page.wait_for_selector(PLAY_PERSP)
    # Armed while the replay response is still held, so the signal cannot
    # fire before the listener exists.
    await _arm_rehydrated_signal(page)
    await page.click(OTHER_NAV_BTN)
    await page.wait_for_selector(PLAY_PERSP, state="detached")
    await page.evaluate("() => window.__aiRehydrated")

    assert await page.locator(AI_WB).count() == 0, "AI panel floated over the next perspective"
