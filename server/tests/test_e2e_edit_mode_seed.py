"""End-to-end test: edit-mode ribbon UI must reflect side-to-move and
castling rights from the FEN the user inherits from view mode.

Regression: cm-chessboard's getPosition() returns ONLY the piece-placement
field, so the client's _seedFromFen() saw an undefined STM/castling field
and silently defaulted to "w" with all castling rights cleared, regardless
of the actual position.

Drives a real browser via Playwright.
"""
from __future__ import annotations

import httpx
import pytest

pytest.importorskip("playwright.async_api")

from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import run_uvicorn_subprocess, wait_perspective_ready  # noqa: E402

# Black to move, only White-kingside + Black-queenside castling rights.
# Asymmetric on every axis so a default-init wouldn't accidentally pass.
SEED_FEN = "r3kbnr/ppp1pppp/2n5/3p4/3P4/2N5/PPP1PPPP/R3KBNR b Kq - 0 1"


@pytest.fixture
def server(tmp_path):
    # Register a fake engine so _get_hve doesn't reject API calls with
    # "no_engine_configured". The path doesn't have to launch -- edit mode
    # never spawns the engine.
    registry_path = tmp_path / "engines.json"
    seed = EngineRegistry(path=registry_path)
    e = seed.add(name="MyEngine", path="/nonexistent/engine")
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
async def test_edit_mode_seeds_stm_and_castling_from_inherited_fen(server, page):
    """View mode at SEED_FEN -> click edit pencil -> ribbon must show
    'Black to move' and only the wK/bQ castling pills active.
    """
    base = server

    install = httpx.post(
        f"{base}/_test/hve/install",
        json={
            "engine_path": "/nonexistent/engine",
            "view_mode": True,
            "view_start_fen": SEED_FEN,
            "view_moves_uci": [],
        },
    )
    install.raise_for_status()

    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)
    # View-mode ribbon visible means the board_update with view payload arrived.
    await page.wait_for_function(
        "() => getComputedStyle(document.querySelector('#view-controls')).display !== 'none'",
    )
    # Click the view-mode edit pencil.
    await page.click("#view-edit")
    # Edit ribbon should appear; give the board_update -> editing flip
    # a moment to propagate.
    await page.wait_for_function(
        "() => getComputedStyle(document.querySelector('#edit-controls')).display !== 'none'",
    )
    # Open the side popover to read the toggle pill text.
    await page.click("#edit-side")
    side_text = await page.text_content("#edit-side-toggle")
    # Open the castling popover.
    await page.click("#edit-castle-btn")
    castle_state = await page.evaluate("""() => ({
        wK: document.querySelector('#edit-castle-cb-wk').classList.contains('is-active'),
        wQ: document.querySelector('#edit-castle-cb-wq').classList.contains('is-active'),
        bK: document.querySelector('#edit-castle-cb-bk').classList.contains('is-active'),
        bQ: document.querySelector('#edit-castle-cb-bq').classList.contains('is-active'),
    })""")
    # Clock-row active class must match STM. With FEN STM=b and the user
    # not flipped (bottom = white), the TOP row should be active.
    clock_active = await page.evaluate("""() => ({
        top: document.querySelector('.clock-top')?.classList.contains('active') ?? false,
        bottom: document.querySelector('.clock-bottom')?.classList.contains('active') ?? false,
    })""")

    assert side_text.strip() == "Black to move", (
        f"side pill should read 'Black to move', got {side_text!r}"
    )
    assert castle_state == {"wK": True, "wQ": False, "bK": False, "bQ": True}, (
        f"castling pills mismatch: got {castle_state!r}"
    )
    assert clock_active["top"] is True and clock_active["bottom"] is False, (
        f"top clock should be active (black to move); got {clock_active!r}"
    )
