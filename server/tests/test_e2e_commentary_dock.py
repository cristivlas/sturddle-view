"""E2E: PGN commentary dock/float lifecycle in Play view-mode.

Covers the commentary window (web/app/play-commentary-window.js) which
reuses the createDockableWindow factory with its own dock container
(.play-comments-host). The factory must NOT close commentary when the
play perspective tears down the debug-window stack (UCI Log / Search
Lines) -- that was the regression caught during initial integration.

Skipped if Playwright is missing.
"""
from __future__ import annotations

import httpx
import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import run_uvicorn_subprocess  # noqa: E402


PLAY_PERSP = "#play-perspective"
COMMENTS_HOST = ".play-comments-host"
COMMENTS_SLOT = f"{COMMENTS_HOST} .dock-slot"
COMMENTS_WB = ".winbox.sturddle-wb-commentary"

SEED_ROOT_COMMENT = "Root annotation."
SEED_FIRST_COMMENT = "First move comment."
SEED_THIRD_COMMENT = "Third move comment."

# PGN with a root comment and per-ply comments on plies 1 and 3 (no
# comment on ply 2). Imported into view mode via /game/import, which
# parses PGN comments into ViewModeParams.comments/root_comment.
_PGN = (
    '[Event "?"]\n'
    '[Site "?"]\n'
    '[Date "????.??.??"]\n'
    '[Round "?"]\n'
    '[White "W"]\n'
    '[Black "B"]\n'
    '[Result "*"]\n\n'
    f'{{{SEED_ROOT_COMMENT}}} '
    f'1. e4 {{{SEED_FIRST_COMMENT}}} e5 2. Nf3 {{{SEED_THIRD_COMMENT}}} *\n'
)


@pytest.fixture
def server(tmp_path):
    # Fake engine so _get_hve doesn't reject /game/import; view mode never
    # actually spawns it.
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


async def _new_page(make_page):
    ctx, page = await make_page(viewport={"width": 1600, "height": 1000})
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(f"console.{msg.type}: {msg.text}")
            if msg.type == "error" else None)
    return ctx, page, errors


def _assert_no_errors(errors):
    benign = ("Failed to load resource",)
    real = [e for e in errors if not any(b in e for b in benign)]
    assert real == [], "JS errors:\n" + "\n".join(real)


def _seed_view_mode(base):
    """Drive the server into view mode by importing the seed PGN."""
    resp = httpx.post(
        f"{base}/game/import",
        json={"text": _PGN, "format": "pgn"},
    )
    resp.raise_for_status()


def _set_view_show_pgn_comments(base, value):
    resp = httpx.put(
        f"{base}/settings",
        json={"view_show_pgn_comments": value},
    )
    resp.raise_for_status()


async def _goto_play_in_view_mode(page, base):
    await page.goto(base + "/")
    await page.wait_for_selector(PLAY_PERSP)
    # Wait for the view-mode ribbon to confirm board_update arrived.
    await page.wait_for_function(
        "() => getComputedStyle(document.querySelector('#view-controls')).display !== 'none'",
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
async def test_commentary_opens_docked_on_view_mode_entry(server, make_page):
    """Entering view-mode with setting on -> commentary auto-docks and shows root comment."""
    base = server
    _seed_view_mode(base)
    _ctx, page, errors = await _new_page(make_page)
    await _goto_play_in_view_mode(page, base)
    await page.wait_for_selector(COMMENTS_SLOT)
    s = await _snapshot(page)
    assert s["slotPresent"]
    assert s["hostHasDockEmpty"] is False
    assert s["docked"] == "1"
    assert s["openFlag"] == "1"
    assert SEED_ROOT_COMMENT in (s["slotText"] or "")
    _assert_no_errors(errors)


@pytest.mark.asyncio
async def test_commentary_survives_debug_window_lifecycle(server, make_page):
    """Regression: closeDebugWindowsPersist (triggered on view-mode entry)
    must only close UCI-dock instances, not commentary."""
    base = server
    _seed_view_mode(base)
    _ctx, page, errors = await _new_page(make_page)
    await _goto_play_in_view_mode(page, base)
    await page.wait_for_selector(COMMENTS_SLOT)
    # Stays open across navigation (which triggers fresh board_update +
    # the analysis-off code path that previously tore commentary down).
    await page.evaluate("document.querySelector('#view-forward')?.click()")
    await page.wait_for_function(
        f"() => document.querySelector('{COMMENTS_SLOT} .pgn-comments-body')"
        f"?.textContent?.includes('{SEED_FIRST_COMMENT}')",
    )
    s = await _snapshot(page)
    assert s["slotPresent"]
    # And after another navigation step.
    await page.evaluate("document.querySelector('#view-forward')?.click()")
    await page.wait_for_function(
        f"() => document.querySelector('{COMMENTS_SLOT} .pgn-comments-body')"
        "?.textContent?.includes('No commentary at this ply')",
    )
    s = await _snapshot(page)
    assert s["slotPresent"]
    _assert_no_errors(errors)


@pytest.mark.asyncio
async def test_commentary_text_updates_per_ply(server, make_page):
    """Navigating to a no-comment ply shows placeholder; window stays open."""
    base = server
    _seed_view_mode(base)
    _ctx, page, errors = await _new_page(make_page)
    await _goto_play_in_view_mode(page, base)
    await page.wait_for_selector(COMMENTS_SLOT)

    # Cursor 0 = root comment.
    s = await _snapshot(page)
    assert SEED_ROOT_COMMENT in (s["slotText"] or "")

    # Cursor 1 = first move comment.
    await page.evaluate("document.querySelector('#view-forward')?.click()")
    await page.wait_for_function(
        f"() => document.querySelector('{COMMENTS_SLOT} .pgn-comments-body')"
        f"?.textContent?.includes('{SEED_FIRST_COMMENT}')",
    )

    # Cursor 2 = no comment -> placeholder.
    await page.evaluate("document.querySelector('#view-forward')?.click()")
    await page.wait_for_function(
        f"() => document.querySelector('{COMMENTS_SLOT} .pgn-comments-body')"
        "?.textContent?.includes('No commentary at this ply')",
    )
    s = await _snapshot(page)
    assert s["slotPresent"], "must stay open at no-comment ply"

    # Cursor 3 = third move comment.
    await page.evaluate("document.querySelector('#view-forward')?.click()")
    await page.wait_for_function(
        f"() => document.querySelector('{COMMENTS_SLOT} .pgn-comments-body')"
        f"?.textContent?.includes('{SEED_THIRD_COMMENT}')",
    )
    _assert_no_errors(errors)


@pytest.mark.asyncio
async def test_undock_floats_as_winbox(server, make_page):
    """Slot undock button moves commentary into a floating WinBox."""
    base = server
    _seed_view_mode(base)
    _ctx, page, errors = await _new_page(make_page)
    await _goto_play_in_view_mode(page, base)
    await page.wait_for_selector(COMMENTS_SLOT)
    await page.evaluate(
        f"document.querySelector('{COMMENTS_SLOT} .dock-slot-undock')?.click()"
    )
    await page.wait_for_selector(COMMENTS_WB)
    s = await _snapshot(page)
    assert s["wbPresent"]
    assert not s["slotPresent"]
    assert s["docked"] == "0"
    _assert_no_errors(errors)


@pytest.mark.asyncio
async def test_redock_via_winbox_control(server, make_page):
    """The WinBox dock control returns commentary to a dock slot."""
    base = server
    _seed_view_mode(base)
    _ctx, page, errors = await _new_page(make_page)
    await _goto_play_in_view_mode(page, base)
    await page.wait_for_selector(COMMENTS_SLOT)
    await page.evaluate(
        f"document.querySelector('{COMMENTS_SLOT} .dock-slot-undock')?.click()"
    )
    await page.wait_for_selector(COMMENTS_WB)
    await page.evaluate(
        f"document.querySelector('{COMMENTS_WB} .wb-dock-ctrl')?.click()"
    )
    await page.wait_for_function(
        f"() => !document.querySelector('{COMMENTS_WB}')",
    )
    s = await _snapshot(page)
    assert s["slotPresent"]
    assert not s["wbPresent"]
    assert s["docked"] == "1"
    _assert_no_errors(errors)


@pytest.mark.asyncio
async def test_slot_close_clears_setting(server, make_page):
    """Clicking the slot X closes commentary AND clears the server setting."""
    base = server
    _seed_view_mode(base)
    _ctx, page, errors = await _new_page(make_page)
    await _goto_play_in_view_mode(page, base)
    await page.wait_for_selector(COMMENTS_SLOT)
    await page.evaluate(
        f"document.querySelector('{COMMENTS_SLOT} .dock-slot-close')?.click()"
    )
    await page.wait_for_function(
        f"() => !document.querySelector('{COMMENTS_SLOT}')",
    )
    v = await page.evaluate(
        "async () => (await (await fetch('/settings')).json()).view_show_pgn_comments"
    )
    assert v is False, f"expected setting cleared after X, got {v}"
    _assert_no_errors(errors)


# -- Regression: stale comment-nav state after game switch ----------------
# Reproduces both reported symptoms when importing a new game while the
# commentary window is already open:
#  (a) "next comment" jumps to an old-game ply that has no comment in the
#      new game, skipping many plies; and
#  (b) the old-game ply is past the end of the new game, so view/goto
#      400s with "ply out of range" and a "Navigation failed" toast.

# Second PGN: 2 plies, single comment at ply 1 (no root, no later comments).
# Old game's commented ply 3 is out of range here (only 0..2 valid).
_PGN_TWO_PLY = (
    '[Event "?"]\n[Site "?"]\n[Date "????.??.??"]\n[Round "?"]\n'
    '[White "X"]\n[Black "Y"]\n[Result "*"]\n\n'
    '1. d4 {Second-game first comment.} d5 *\n'
)
SECOND_FIRST_COMMENT = "Second-game first comment."

# Third PGN: 2 plies, NO comments at all -- both nav buttons must end up
# disabled after the switch.
_PGN_NO_COMMENTS = (
    '[Event "?"]\n[Site "?"]\n[Date "????.??.??"]\n[Round "?"]\n'
    '[White "X"]\n[Black "Y"]\n[Result "*"]\n\n'
    '1. d4 d5 *\n'
)

NAV_PREV = f"{COMMENTS_SLOT} .pgn-comments-nav-btn:nth-child(1)"
NAV_NEXT = f"{COMMENTS_SLOT} .pgn-comments-nav-btn:nth-child(2)"


def _import_pgn(base, pgn):
    resp = httpx.post(f"{base}/game/import", json={"text": pgn, "format": "pgn"})
    resp.raise_for_status()


@pytest.mark.asyncio
async def test_comment_nav_disabled_after_switch_to_no_comments(server, make_page):
    """Import a commented game, then switch to one with no comments while
    the commentary window stays open. Both nav buttons must become
    disabled -- currently they carry stale prev/next from the old game."""
    base = server
    _seed_view_mode(base)  # game A: comments at plies 1, 3
    _ctx, page, errors = await _new_page(make_page)
    await _goto_play_in_view_mode(page, base)
    await page.wait_for_selector(COMMENTS_SLOT)
    # Move forward so prev_comment is populated from game A.
    await page.evaluate("document.querySelector('#view-forward')?.click()")
    await page.wait_for_function(
        f"() => !document.querySelector('{NAV_PREV}').disabled"
        f" || !document.querySelector('{NAV_NEXT}').disabled"
    )
    # Switch games via direct HTTP import (bypasses the JS confirm dialog).
    _import_pgn(base, _PGN_NO_COMMENTS)
    # Wait for the new game's board_update to land (root comment placeholder
    # replaces the old game's text).
    await page.wait_for_function(
        f"() => document.querySelector('{COMMENTS_SLOT} .pgn-comments-body')"
        "?.textContent?.includes('No commentary at this ply')"
    )
    prev_disabled = await page.evaluate(
        f"() => document.querySelector('{NAV_PREV}').disabled"
    )
    next_disabled = await page.evaluate(
        f"() => document.querySelector('{NAV_NEXT}').disabled"
    )
    assert prev_disabled, "prev-comment must be disabled in a commentless game"
    assert next_disabled, "next-comment must be disabled in a commentless game"
    _assert_no_errors(errors)


@pytest.mark.asyncio
async def test_comment_nav_targets_new_game_after_switch(server, make_page):
    """Import a longer commented game, then switch to a shorter one with
    a comment at ply 1. Clicking next-comment must navigate within the
    NEW game (no 400 'ply out of range', no 'Navigation failed' toast,
    new comment text shows)."""
    base = server
    _seed_view_mode(base)  # game A: 4 plies, comment at ply 3
    _ctx, page, errors = await _new_page(make_page)
    await _goto_play_in_view_mode(page, base)
    await page.wait_for_selector(COMMENTS_SLOT)
    # Capture failed view/goto requests -- the bug 400s when the stale
    # ply is past the end of the new game.
    failed_goto = []
    page.on(
        "response",
        lambda r: failed_goto.append(r.url) if (
            r.status >= 400 and "view/goto" in r.url
        ) else None,
    )
    # Switch to the short game (2 plies, comment at ply 1).
    _import_pgn(base, _PGN_TWO_PLY)
    # Wait for new game state to land (cursor 0, no root comment).
    await page.wait_for_function(
        f"() => document.querySelector('{COMMENTS_SLOT} .pgn-comments-body')"
        "?.textContent?.includes('No commentary at this ply')"
    )
    # Click next-comment. The new game has its first comment at ply 1.
    await page.evaluate(f"document.querySelector('{NAV_NEXT}')?.click()")
    await page.wait_for_function(
        f"() => document.querySelector('{COMMENTS_SLOT} .pgn-comments-body')"
        f"?.textContent?.includes('{SECOND_FIRST_COMMENT}')"
    )
    assert not failed_goto, f"view/goto failed (stale ply): {failed_goto}"
    _assert_no_errors(errors)


@pytest.mark.asyncio
async def test_comment_nav_recovers_after_resize_hide_show(server, make_page):
    """Resize past the hide-commentary breakpoint then back: the rebuilt
    body's nav buttons start disabled and used to stay dead until the next
    board_update (a move click). Recovery must happen with no navigation."""
    base = server
    _seed_view_mode(base)  # game A: comments at plies 1, 3
    _ctx, page, errors = await _new_page(make_page)
    await _goto_play_in_view_mode(page, base)
    await page.wait_for_selector(COMMENTS_SLOT)
    # Move to a ply with a populated prev target so the buttons are live.
    await page.evaluate("document.querySelector('#view-forward')?.click()")
    await page.wait_for_function(
        f"() => !document.querySelector('{NAV_PREV}').disabled"
    )
    # Any view/goto after this point would mean recovery came from nav,
    # not from the hide/show push -- which is what we are testing against.
    gotos = []
    page.on(
        "request",
        lambda r: gotos.append(r.url) if "view/goto" in r.url else None,
    )
    # Hide: drop below --bp-mobile-h-play so isMobileLayout() hides the dock.
    # Read the gate live (rem * 16 = its media-query px) so the test isn't
    # pinned to a literal that drifts when the breakpoint changes.
    gate_px = await page.evaluate(
        "() => parseFloat(getComputedStyle(document.documentElement)"
        ".getPropertyValue('--bp-mobile-h-play')) * 16"
    )
    await page.set_viewport_size({"width": 1600, "height": int(gate_px) - 40})
    await page.wait_for_function(f"() => !document.querySelector('{COMMENTS_SLOT}')")
    # Show again: back well above the gate.
    await page.set_viewport_size({"width": 1600, "height": int(gate_px) + 320})
    await page.wait_for_selector(COMMENTS_SLOT)
    # prev-comment must be live again without a move click.
    await page.wait_for_function(
        f"() => !document.querySelector('{NAV_PREV}').disabled"
    )
    assert not gotos, f"recovery must not require navigation, saw: {gotos}"
    _assert_no_errors(errors)


@pytest.mark.asyncio
async def test_setting_off_keeps_commentary_closed(server, make_page):
    """Entering view mode with setting=false -> no dock slot, no WinBox."""
    base = server
    # Pre-set the setting off before the perspective mounts.
    _set_view_show_pgn_comments(base, False)
    _seed_view_mode(base)
    _ctx, page, errors = await _new_page(make_page)
    await _goto_play_in_view_mode(page, base)
    # _goto_play_in_view_mode awaits the view-controls becoming visible,
    # which only fires after the perspective's awaited refreshSettings()
    # GET resolves and the subsequent board_update runs
    # syncCommentsVisibility -- so by here the no-open decision is final.
    s = await _snapshot(page)
    assert not s["slotPresent"]
    assert not s["wbPresent"]
    _assert_no_errors(errors)
