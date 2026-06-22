"""End-to-end test: view-mode board orientation survives a perspective
remount (View -> Engines/Tournaments -> back).

Regression for: in View mode the server sends ``human_white: null`` (the
user isn't "playing" at the cursor), so the client's ``state.humanWhite``
is never refreshed and falls back to its mount default (True = White at
bottom). On perspective remount of a *resumable* view session (a live game
scrubbed back via /view/start), play.js re-derived orientation from that
stale ``state.humanWhite`` -- so a Black player's board snapped to
White-bottom. Play mode is unaffected because its board_update carries a
real ``human_white`` boolean.

Drives a real browser via Playwright. Skipped if Playwright or its Chromium
isn't available so unit-only test runs aren't blocked.
"""
from __future__ import annotations

import httpx
import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import run_uvicorn_subprocess, wait_perspective_ready  # noqa: E402

ENGINE_NAME = "MyEngine 1.0"


@pytest.fixture
def server(tmp_path):
    # /view/start requires a configured engine; seed one (never launched --
    # the test keeps it the human's turn so the engine is never invoked).
    registry_path = tmp_path / "engines.json"
    seed = EngineRegistry(path=registry_path)
    e = seed.add(name=ENGINE_NAME, path="/nonexistent/engine")
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


# cm-chessboard renders the bottom-left file coordinate as "a" when White
# is at the bottom and "h" when Black is at the bottom. That first file
# label is the orientation signal.
_FIRST_FILE_LABEL = (
    "() => document.querySelector("
    "'.game-view-board svg.cm-chessboard .coordinate.file')?.textContent ?? null"
)


@pytest.mark.asyncio
async def test_resumable_view_orientation_survives_perspective_remount(server, page):
    """A Black player who scrubs a live game into view mode must keep
    Black-bottom after navigating away and back."""
    base = server

    # Live play game, human plays Black. Odd ply count leaves Black (the
    # human) to move so /game/sync never kicks the (nonexistent) engine.
    install = httpx.post(
        f"{base}/_test/hve/install",
        json={
            "human_white": False,
            "moves_uci": ["e2e4", "c7c5", "g1f3"],
            "tc": {"initial_seconds": 300.0, "increment_seconds": 0.0},
            "game_id": "test-game",
        },
    )
    install.raise_for_status()

    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)
    # Play mode, Black at bottom.
    await page.wait_for_function(f"{_FIRST_FILE_LABEL} === 'h'")

    # Scrub the live game into a resumable view session (the same path the
    # scrub-back UI uses). Land one ply back so we stay in view (the
    # last-ply auto-resume can't fire on the landing event).
    httpx.post(
        f"{base}/game/view/start",
        json={"suspend": True, "land_at_ply": 1},
    ).raise_for_status()
    await page.wait_for_function(
        "() => getComputedStyle(document.querySelector('#view-controls'))"
        ".display !== 'none'",
    )
    # Still Black-bottom in view mode (resumable keeps the player's POV).
    await page.wait_for_function(f"{_FIRST_FILE_LABEL} === 'h'")

    # Navigate to the Engines/Tournaments perspective (drops Play's DOM),
    # then back -- the Play perspective remounts and resyncs.
    await page.click('[data-perspective="engines"]')
    await page.wait_for_function(
        "() => !!document.querySelector('#engines-perspective')"
        " && !document.querySelector('#perspective-root')?.classList.contains('is-pending')",
    )
    await page.click('[data-perspective="play"]')
    await wait_perspective_ready(page)
    await page.wait_for_function(
        "() => getComputedStyle(document.querySelector('#view-controls'))"
        ".display !== 'none'",
    )

    # The board must still be Black-bottom. The bug snapped it to "a".
    label = await page.evaluate(_FIRST_FILE_LABEL)
    assert label == "h", (
        f"resumable view orientation lost on remount: first file label is "
        f"{label!r} (expected 'h' = Black at bottom)"
    )
