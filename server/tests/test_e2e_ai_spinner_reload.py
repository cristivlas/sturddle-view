"""E2E regression: a reload early in an AI turn keeps the spinner.

A turn emits nothing until the model's first output -- on a local model
that can take minutes. The page rebuilds the AI panel from the replay
buffer, so a reload in that window found it empty and showed no panel and
no spinner until the first event arrived.

Driven by a test hook that starts a turn whose model never answers, then
loading the page. Skipped if Playwright is missing.
"""
from __future__ import annotations

import httpx
import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import REGISTRY_FILE, e2e_env, run_uvicorn_subprocess  # noqa: E402


PLAY_PERSP = "#play-perspective"
_STATUS_TEXT = ".play-ai-status-text"
_ANALYZING = "Analyzing..."

_PGN = (
    '[Event "?"]\n[Site "?"]\n[Date "????.??.??"]\n[Round "?"]\n'
    '[White "W"]\n[Black "B"]\n[Result "*"]\n\n'
    '1. e4 e5 2. Nf3 Nc6 *\n'
)


@pytest.fixture
def server(tmp_path):
    registry_path = tmp_path / REGISTRY_FILE
    seed = EngineRegistry(path=registry_path)
    e = seed.add(name="MyEngine", path="/nonexistent/engine")
    seed.select(e.id)
    env = e2e_env(tmp_path)
    with run_uvicorn_subprocess(env_overrides=env) as base:
        yield base


@pytest.mark.asyncio
async def test_reload_before_the_first_event_shows_the_spinner(server, make_page):
    base = server
    resp = httpx.post(f"{base}/game/import", json={"text": _PGN, "format": "pgn"})
    resp.raise_for_status()
    game_id = resp.json()["game_id"]
    httpx.post(f"{base}/_test/ai/start_silent_turn", json={"game_id": game_id}).raise_for_status()
    # The turn is live but has emitted nothing: the replay is empty.
    assert httpx.get(f"{base}/game/analysis/replay").json()["events"] == []

    _ctx, page = await make_page(viewport={"width": 1600, "height": 1000})
    await page.goto(base + "/")
    await page.wait_for_selector(PLAY_PERSP)

    await page.wait_for_function(
        """([sel, text]) => {
          const el = document.querySelector(sel);
          return !!el && el.offsetParent !== null && el.textContent.trim() === text;
        }""",
        arg=[_STATUS_TEXT, _ANALYZING],
    )
