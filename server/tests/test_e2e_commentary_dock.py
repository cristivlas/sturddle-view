"""E2E: PGN commentary dock/float lifecycle in Play view-mode.

Covers the commentary window (web/app/play-commentary-window.js) which
reuses the createDockableWindow factory with its own dock container
(.play-comments-host). The factory must NOT close commentary when the
play perspective tears down the debug-window stack (UCI Log / Search
Lines) -- that was the regression caught during initial integration.

Skipped if Playwright is missing.
"""
from __future__ import annotations

import socket
import sys
import threading
import time

import pytest

pytest.importorskip("playwright.async_api")

from sturddle_view.app import create_app  # noqa: E402
from sturddle_view.config import Settings  # noqa: E402
from sturddle_view.engines import EngineRegistry  # noqa: E402


PLAY_PERSP = "#play-perspective"
COMMENTS_HOST = ".play-comments-host"
COMMENTS_SLOT = f"{COMMENTS_HOST} .dock-slot"
COMMENTS_WB = ".winbox.sturddle-wb-commentary"

SEED_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
# Three plies, comments only on plies 1 and 3 (cursor 1 and 3 respectively).
SEED_MOVES = ["e2e4", "e7e5", "g1f3"]
SEED_COMMENTS = ["First move comment.", None, "Third move comment."]
SEED_ROOT_COMMENT = "Root annotation."


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path):
    import uvicorn

    settings = Settings(token="test-token", auth_disabled=True)
    settings.pgn_dir = tmp_path / "pgn"
    registry = EngineRegistry(path=tmp_path / "engines.json")
    # Fake engine so _get_hve doesn't reject API calls; view mode never
    # actually spawns it.
    e = registry.add(name="MyEngine", path="/nonexistent/engine")
    registry.select(e.id)
    app = create_app(settings=settings, engine_registry=registry)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws="wsproto")
    s = uvicorn.Server(config)
    thread = threading.Thread(target=s.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline and not s.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", app
    s.should_exit = True
    s.force_exit = True
    thread.join(timeout=2)


async def _new_page(browser):
    ctx = await browser.new_context(viewport={"width": 1600, "height": 1000})
    page = await ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(f"console.{msg.type}: {msg.text}")
            if msg.type == "error" else None)
    return ctx, page, errors


def _assert_no_errors(errors):
    benign = ("Failed to load resource",)
    real = [e for e in errors if not any(b in e for b in benign)]
    assert real == [], "JS errors:\n" + "\n".join(real)


async def _seed_view_mode(app):
    from sturddle_view.play.human_vs_engine import HumanVsEngine
    hve = HumanVsEngine(
        engine_path="/nonexistent/engine",
        bus=app.state.event_bus,
        openings=getattr(app.state, "openings", None),
        settings=app.state.settings,
    )
    await hve.enter_view_mode(
        start_fen=SEED_FEN,
        moves_uci=SEED_MOVES,
        clock_history=None,
        comments=SEED_COMMENTS,
        root_comment=SEED_ROOT_COMMENT,
    )
    app.state.hve = hve


async def _goto_play_in_view_mode(page, base):
    await page.goto(base + "/")
    await page.wait_for_selector(PLAY_PERSP, timeout=5000)
    # Wait for the view-mode ribbon to confirm board_update arrived.
    await page.wait_for_function(
        "() => getComputedStyle(document.querySelector('#view-controls')).display !== 'none'",
        timeout=5000,
    )


async def _snapshot(page):
    return await page.evaluate("""
      () => {
        const host = document.querySelector('.play-comments-host');
        const slot = host?.querySelector('.dock-slot');
        const body = slot?.querySelector('.pgn-comments-body');
        const wb = document.querySelector('.winbox.sturddle-wb-commentary');
        return {
          hostHasDockEmpty: host?.classList.contains('dock-empty') ?? null,
          slotPresent: !!slot,
          slotText: body?.textContent?.trim() || null,
          wbPresent: !!wb,
          docked: localStorage.getItem('sturddle:commentary:docked'),
          openFlag: localStorage.getItem('sturddle:commentary:open'),
        };
      }
    """)


@pytest.mark.asyncio
async def test_commentary_opens_docked_on_view_mode_entry(server, browser):
    """Entering view-mode with setting on -> commentary auto-docks and shows root comment."""
    if browser is None:
        pytest.skip("chromium not installed")
    base, app = server
    await _seed_view_mode(app)
    ctx, page, errors = await _new_page(browser)
    try:
        await _goto_play_in_view_mode(page, base)
        await page.wait_for_selector(COMMENTS_SLOT, timeout=3000)
        s = await _snapshot(page)
        assert s["slotPresent"]
        assert s["hostHasDockEmpty"] is False
        assert s["docked"] == "1"
        assert s["openFlag"] == "1"
        assert SEED_ROOT_COMMENT in (s["slotText"] or "")
        _assert_no_errors(errors)
    finally:
        await ctx.close()


@pytest.mark.asyncio
async def test_commentary_survives_debug_window_lifecycle(server, browser):
    """Regression: closeDebugWindowsPersist (triggered on view-mode entry)
    must only close UCI-dock instances, not commentary."""
    if browser is None:
        pytest.skip("chromium not installed")
    base, app = server
    await _seed_view_mode(app)
    ctx, page, errors = await _new_page(browser)
    try:
        await _goto_play_in_view_mode(page, base)
        await page.wait_for_selector(COMMENTS_SLOT, timeout=3000)
        # Stays open across navigation (which triggers fresh board_update +
        # the analysis-off code path that previously tore commentary down).
        await page.evaluate("document.querySelector('#view-forward')?.click()")
        await page.wait_for_timeout(300)
        s = await _snapshot(page)
        assert s["slotPresent"]
        # And after another navigation step.
        await page.evaluate("document.querySelector('#view-forward')?.click()")
        await page.wait_for_timeout(300)
        s = await _snapshot(page)
        assert s["slotPresent"]
        _assert_no_errors(errors)
    finally:
        await ctx.close()


@pytest.mark.asyncio
async def test_commentary_text_updates_per_ply(server, browser):
    """Navigating to a no-comment ply shows placeholder; window stays open."""
    if browser is None:
        pytest.skip("chromium not installed")
    base, app = server
    await _seed_view_mode(app)
    ctx, page, errors = await _new_page(browser)
    try:
        await _goto_play_in_view_mode(page, base)
        await page.wait_for_selector(COMMENTS_SLOT, timeout=3000)

        # Cursor 0 = root comment.
        s = await _snapshot(page)
        assert SEED_ROOT_COMMENT in (s["slotText"] or "")

        # Cursor 1 = first move comment.
        await page.evaluate("document.querySelector('#view-forward')?.click()")
        await page.wait_for_function(
            f"() => document.querySelector('{COMMENTS_SLOT} .pgn-comments-body')"
            "?.textContent?.includes('First move comment.')",
            timeout=2000,
        )

        # Cursor 2 = no comment -> placeholder.
        await page.evaluate("document.querySelector('#view-forward')?.click()")
        await page.wait_for_function(
            f"() => document.querySelector('{COMMENTS_SLOT} .pgn-comments-body')"
            "?.textContent?.includes('No commentary at this ply')",
            timeout=2000,
        )
        s = await _snapshot(page)
        assert s["slotPresent"], "must stay open at no-comment ply"

        # Cursor 3 = third move comment.
        await page.evaluate("document.querySelector('#view-forward')?.click()")
        await page.wait_for_function(
            f"() => document.querySelector('{COMMENTS_SLOT} .pgn-comments-body')"
            "?.textContent?.includes('Third move comment.')",
            timeout=2000,
        )
        _assert_no_errors(errors)
    finally:
        await ctx.close()


@pytest.mark.asyncio
async def test_undock_floats_as_winbox(server, browser):
    """Slot undock button moves commentary into a floating WinBox."""
    if browser is None:
        pytest.skip("chromium not installed")
    base, app = server
    await _seed_view_mode(app)
    ctx, page, errors = await _new_page(browser)
    try:
        await _goto_play_in_view_mode(page, base)
        await page.wait_for_selector(COMMENTS_SLOT, timeout=3000)
        await page.evaluate(
            f"document.querySelector('{COMMENTS_SLOT} .dock-slot-undock')?.click()"
        )
        await page.wait_for_selector(COMMENTS_WB, timeout=2000)
        s = await _snapshot(page)
        assert s["wbPresent"]
        assert not s["slotPresent"]
        assert s["docked"] == "0"
        _assert_no_errors(errors)
    finally:
        await ctx.close()


@pytest.mark.asyncio
async def test_redock_via_winbox_control(server, browser):
    """The WinBox dock control returns commentary to a dock slot."""
    if browser is None:
        pytest.skip("chromium not installed")
    base, app = server
    await _seed_view_mode(app)
    ctx, page, errors = await _new_page(browser)
    try:
        await _goto_play_in_view_mode(page, base)
        await page.wait_for_selector(COMMENTS_SLOT, timeout=3000)
        await page.evaluate(
            f"document.querySelector('{COMMENTS_SLOT} .dock-slot-undock')?.click()"
        )
        await page.wait_for_selector(COMMENTS_WB, timeout=2000)
        await page.evaluate(
            f"document.querySelector('{COMMENTS_WB} .wb-dock-ctrl')?.click()"
        )
        await page.wait_for_function(
            f"() => !document.querySelector('{COMMENTS_WB}')",
            timeout=2000,
        )
        s = await _snapshot(page)
        assert s["slotPresent"]
        assert not s["wbPresent"]
        assert s["docked"] == "1"
        _assert_no_errors(errors)
    finally:
        await ctx.close()


@pytest.mark.asyncio
async def test_slot_close_clears_setting(server, browser):
    """Clicking the slot X closes commentary AND clears the server setting."""
    if browser is None:
        pytest.skip("chromium not installed")
    base, app = server
    await _seed_view_mode(app)
    ctx, page, errors = await _new_page(browser)
    try:
        await _goto_play_in_view_mode(page, base)
        await page.wait_for_selector(COMMENTS_SLOT, timeout=3000)
        await page.evaluate(
            f"document.querySelector('{COMMENTS_SLOT} .dock-slot-close')?.click()"
        )
        await page.wait_for_function(
            f"() => !document.querySelector('{COMMENTS_SLOT}')",
            timeout=2000,
        )
        v = await page.evaluate(
            "async () => (await (await fetch('/settings')).json()).view_show_pgn_comments"
        )
        assert v is False, f"expected setting cleared after X, got {v}"
        _assert_no_errors(errors)
    finally:
        await ctx.close()


@pytest.mark.asyncio
async def test_setting_off_keeps_commentary_closed(server, browser):
    """Entering view mode with setting=false -> no dock slot, no WinBox."""
    if browser is None:
        pytest.skip("chromium not installed")
    base, app = server
    # Pre-set the setting off before mounting the perspective.
    app.state.settings.view_show_pgn_comments = False
    await _seed_view_mode(app)
    ctx, page, errors = await _new_page(browser)
    try:
        await _goto_play_in_view_mode(page, base)
        # Settle then assert.
        await page.wait_for_timeout(500)
        s = await _snapshot(page)
        assert not s["slotPresent"]
        assert not s["wbPresent"]
        _assert_no_errors(errors)
    finally:
        await ctx.close()
