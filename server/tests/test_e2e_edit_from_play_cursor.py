"""Regression test: entering edit mode from play must land the view
cursor at the last ply (the live position the user wants to edit), not
at ply 0.

Bug: with view_show_pgn_comments=true, syncCommentsVisibility's
seeding POST /view/goto fired on the first board_update (cursor=0
from enter_view_mode), overwriting the cursor=n of the play -> view
flip. The pencil now makes one /game/edit/start call, which opens the
editor on a live clone at the last ply.

Adjacent paths whose existing behavior must be preserved:
  * /game/import (PGN/FEN import) lands at cursor=0.
  * Tournament Replay (import + activate-perspective) lands at cursor=0.

Synchronization: tests are timeout-free. We observe HTTP requests via
Playwright's CDP-level request/requestfinished events (no JS injection
needed -- so no leaked listeners) and await the request lifecycles that
deterministically bound when the cursor would have been mutated."""
from __future__ import annotations

import httpx
import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from .conftest import (  # noqa: E402
    FAKE_ENGINE,
    PageObserver,
    make_replying_fake_uci,
    serve_with_engine,
    wait_perspective_ready,
)


@pytest.fixture
def server(tmp_path):
    with serve_with_engine(tmp_path, FAKE_ENGINE, make_replying_fake_uci(tmp_path, FAKE_ENGINE)) as base:
        yield base


def _view_cursor(base: str) -> int:
    return httpx.get(f"{base}/_test/hve/state").json()["view_cursor"]


def _n_plies(base: str) -> int:
    return httpx.get(f"{base}/_test/hve/state").json()["n_plies"]


_PGN_SIMPLE = """\
[Event "T"]
[Site "?"]
[Date "????.??.??"]
[Round "?"]
[White "W"]
[Black "B"]
[Result "*"]

1. e4 e5 2. Nf3 Nc6 *
"""


# Board-update predicates. The board_update WS frame carries `viewing`
# implicitly via the `view` field's presence, `editing` as a boolean,
# `moves_san` as the SAN list, and `view.cursor` as the current ply.

def _two_plies_visible(p):
    return len(p.get("moves_san") or []) >= 2


def _editing_started(p):
    return p.get("editing") is True


def _viewing_at_cursor_zero(p):
    v = p.get("view") or {}
    return bool(v) and v.get("cursor") == 0


async def _enable_comments(page):
    await page.evaluate(
        "() => fetch('/settings', {method:'PUT',"
        " headers:{'Content-Type':'application/json'},"
        " body: JSON.stringify({view_show_pgn_comments: true})}).then(r => r.text())"
    )


async def _setup_play_with_one_move(page, obs):
    """Open the app, start a play game, push 1.e4 -- await the engine's
    reply so we have a 2-ply position before the bug-triggering click."""
    await page.goto("/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)
    await _enable_comments(page)
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
    # Engine bestmove arrives via WS board_update; await it.
    await obs.wait_board_update(_two_plies_visible)


@pytest.mark.asyncio
async def test_edit_from_play_lands_at_last_ply_with_comments_on(server, make_page):
    """Click 'Edit position' from play with comments on: cursor must be
    at the last ply (the live position), not back at 0."""
    base = server

    _ctx, page = await make_page(base_url=base)
    obs = PageObserver(page)
    # Pre-register /view/goto so any (buggy) request gets counted before
    # we wait for the in-flight queue to drain.
    obs.track("/game/view/goto")
    await _setup_play_with_one_move(page, obs)
    n_plies = _n_plies(base)
    assert n_plies == 2, f"precondition: 2 plies in play; got {n_plies}"

    # Click Edit position: no confirm, nothing is discarded.
    await page.click("#edit-pos")
    # Deterministic settle: editing=true has arrived from server AND any
    # spurious /view/goto the JS may have fired has resolved.
    await obs.wait_board_update(_editing_started)
    await obs.wait_quiet("/game/view/goto")

    cursor = _view_cursor(base)
    assert cursor == n_plies, (
        f"play->edit landed at cursor={cursor}, "
        f"expected {n_plies} (last ply)"
    )


@pytest.mark.asyncio
async def test_import_lands_at_first_ply_with_comments_on(server, make_page):
    """Adjacent path: explicit PGN import keeps existing behavior --
    cursor starts at 0."""
    base = server

    _ctx, page = await make_page(base_url=base)
    obs = PageObserver(page)
    obs.track("/game/view/goto")
    await page.goto("/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)
    await _enable_comments(page)

    await page.evaluate(
        "async (pgn) => { const r = await fetch('/game/import', {method:'POST',"
        " headers:{'Content-Type':'application/json'},"
        " body: JSON.stringify({text: pgn, format: 'pgn'})});"
        " if (r.status !== 200) throw new Error('import failed: ' + r.status);"
        " await fetch('/game/sync', {method:'POST'}); }",
        _PGN_SIMPLE,
    )
    await obs.wait_board_update(_viewing_at_cursor_zero)
    await obs.wait_quiet("/game/view/goto")

    cursor = _view_cursor(base)
    assert cursor == 0, (
        f"import should land at cursor=0; got {cursor}"
    )


@pytest.mark.asyncio
async def test_replay_activation_lands_at_first_ply_with_comments_on(server, make_page):
    """Adjacent path: tournament Replay (import + activate play perspective)
    keeps existing behavior -- cursor starts at 0."""
    base = server

    _ctx, page = await make_page(base_url=base)
    obs = PageObserver(page)
    obs.track("/game/view/goto")
    await page.goto("/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)
    await _enable_comments(page)

    # Switch off play so the Replay activation is a real transition.
    await page.click('button[data-perspective="engines"]')

    await page.evaluate(
        "async (pgn) => {"
        " await fetch('/game/import', {method:'POST',"
        "  headers:{'Content-Type':'application/json'},"
        "  body: JSON.stringify({text: pgn, format: 'pgn'})});"
        " window.dispatchEvent(new CustomEvent("
        "  'sturddle:activate-perspective', {detail:{id:'play'}}));"
        "}",
        _PGN_SIMPLE,
    )
    await obs.wait_board_update(_viewing_at_cursor_zero)
    await obs.wait_quiet("/game/view/goto")

    cursor = _view_cursor(base)
    assert cursor == 0, (
        f"replay should land at cursor=0; got {cursor}"
    )
