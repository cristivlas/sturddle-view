"""End-to-end test: edit-mode ribbon UI must reflect side-to-move and
castling rights from the FEN the user inherits from view mode.

Regression: cm-chessboard's getPosition() returns ONLY the piece-placement
field, so the client's _seedFromFen() saw an undefined STM/castling field
and silently defaulted to "w" with all castling rights cleared, regardless
of the actual position.

Drives a real browser via Playwright.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.async_api")

from sturddle_view.app import create_app  # noqa: E402
from sturddle_view.config import Settings  # noqa: E402
from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import run_uvicorn  # noqa: E402

# Black to move, only White-kingside + Black-queenside castling rights.
# Asymmetric on every axis so a default-init wouldn't accidentally pass.
SEED_FEN = "r3kbnr/ppp1pppp/2n5/3p4/3P4/2N5/PPP1PPPP/R3KBNR b Kq - 0 1"


@pytest.fixture
def server(tmp_path):
    settings = Settings(token="test-token", auth_disabled=True)
    settings.pgn_dir = tmp_path / "pgn"
    registry = EngineRegistry(path=tmp_path / "engines.json")
    # Register a fake engine so _get_hve doesn't reject API calls with
    # "no_engine_configured". The path doesn't have to launch -- edit mode
    # never spawns the engine.
    e = registry.add(name="MyEngine", path="/nonexistent/engine")
    registry.select(e.id)
    app = create_app(settings=settings, engine_registry=registry)
    with run_uvicorn(app) as (base, _s):
        yield base, app


@pytest.mark.asyncio
async def test_edit_mode_seeds_stm_and_castling_from_inherited_fen(server, page):
    """View mode at SEED_FEN -> click edit pencil -> ribbon must show
    'Black to move' and only the wK/bQ castling pills active.
    """
    base, app = server

    from sturddle_view.play.human_vs_engine import HumanVsEngine, ViewModeParams

    hve = HumanVsEngine(
        engine_path="/nonexistent/engine",
        bus=app.state.event_bus,
        openings=getattr(app.state, "openings", None),
        settings=app.state.settings,
    )
    await hve.enter_view_mode(ViewModeParams(
        start_fen=SEED_FEN,
        moves_uci=[],
        clock_history=None,
    ))
    app.state.hve = hve

    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")
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
