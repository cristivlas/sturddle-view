"""End-to-end test: view mode shows PGN player names in the clock area.

Regression for a listener-order race where play.js's board_update handler
called ``view.setHumanWhite(...)`` after GameView's handler had already
written the PGN names, clobbering them with ``Human`` / ``<engine name>``.

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

WHITE_NAME = "Celeris 2.0 64-bit"
BLACK_NAME = "Panda 1.1 64-bit"
ENGINE_NAME = "MyEngine 1.0"

# Import a PGN whose headers carry the names we assert on. The
# /game/import endpoint plumbs White/Black headers into view-mode params,
# which is the same field path the production import flow exercises.
_PGN = (
    f'[Event "?"]\n'
    f'[Site "?"]\n'
    f'[Date "????.??.??"]\n'
    f'[Round "?"]\n'
    f'[White "{WHITE_NAME}"]\n'
    f'[Black "{BLACK_NAME}"]\n'
    f'[Result "*"]\n\n'
    f'1. e4 c5 2. Nf3 d6 *\n'
)


@pytest.fixture
def server(tmp_path):
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


@pytest.mark.asyncio
async def test_view_mode_clock_names_after_hard_reload(server, page):
    """In view mode after a hard reload, the clock-area name labels must
    reflect the PGN's White/Black headers, not the play-mode placeholders.
    """
    base = server

    resp = httpx.post(
        f"{base}/game/import",
        json={"text": _PGN, "format": "pgn"},
    )
    resp.raise_for_status()

    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)
    # view-controls become visible only after play.js's board_update
    # handler runs (it toggles view-mode UI based on the viewing flag) --
    # which is the same handler that used to clobber the PGN names.
    # Once it's visible BOTH listeners have run on the first board_update.
    await page.wait_for_function(
        "() => getComputedStyle(document.querySelector('#view-controls'))"
        ".display !== 'none'",
    )

    names = await page.evaluate(
        """() => ({
            top: document.querySelector('.clock-name[data-side="top"]')?.textContent ?? null,
            bottom: document.querySelector('.clock-name[data-side="bottom"]')?.textContent ?? null,
        })"""
    )
    # Production placeholder const, so a rename can't silently weaken the
    # negative assertion below.
    player_default = await page.evaluate(
        "async () => (await import('/ui/app/settings-dialog.js')).PLAYER_NAME_DEFAULT"
    )
    # Bottom defaults to white when not flipped; top is black.
    assert names["bottom"] == WHITE_NAME, (
        f"bottom clock name should be PGN white ({WHITE_NAME!r}), got {names['bottom']!r}"
    )
    assert names["top"] == BLACK_NAME, (
        f"top clock name should be PGN black ({BLACK_NAME!r}), got {names['top']!r}"
    )
    # And explicitly NOT the play-mode placeholders.
    assert names["bottom"] != player_default
    assert names["top"] != ENGINE_NAME
