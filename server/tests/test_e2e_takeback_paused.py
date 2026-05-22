"""End-to-end: takeback button must be enabled while the game is paused.

Server allows takeback while paused; the play ribbon must not disable the
button. Drives a real browser via Playwright.
"""
from __future__ import annotations

import httpx
import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import run_uvicorn_subprocess, wait_perspective_ready  # noqa: E402


FAKE_ENGINE_PATH = "/nonexistent/engine"


@pytest.fixture
def server(tmp_path):
    registry_path = tmp_path / "engines.json"
    seed = EngineRegistry(path=registry_path)
    e = seed.add(name="MyEngine", path=FAKE_ENGINE_PATH)
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


@pytest.mark.asyncio
async def test_takeback_button_enabled_while_paused(server, page):
    base = server

    # Install a 2-ply game: human=white, moves e2e4 e7e5 -> it's white's
    # (the human's) turn, which is what pause() requires. Engine path
    # matches the registry entry so _get_hve doesn't trigger a swap.
    install = httpx.post(
        f"{base}/_test/hve/install",
        json={
            "engine_path": FAKE_ENGINE_PATH,
            "human_white": True,
            "moves_uci": ["e2e4", "e7e5"],
            "tc": {"initial_seconds": 60.0, "increment_seconds": 0.0},
        },
    )
    install.raise_for_status()

    # Drive into PAUSED via the public HTTP endpoint.
    pause_resp = httpx.post(f"{base}/game/pause")
    pause_resp.raise_for_status()

    state = httpx.get(f"{base}/_test/hve/state").json()
    assert state["paused"] is True, f"server not paused after /game/pause: {state}"

    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)
    # First wait for the Resume affordance (icon=forward-step) so we know
    # the client has applied paused=true. Then assert takeback is enabled.
    await page.wait_for_function(
        "() => document.querySelector('#pause wa-icon')?.getAttribute('name') === 'forward-step'",
    )
    disabled = await page.evaluate(
        "() => document.querySelector('#takeback').disabled"
    )
    assert disabled is False, "takeback button must be enabled while paused"
