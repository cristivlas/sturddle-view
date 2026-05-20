"""Regression test: replaying a NEW import while a PREVIOUS import has a
non-zero view cursor must NOT propagate the old cursor into the new game.

Bug: play.js cached _cachedBoardUpdate replay on remount re-emits the
previous game's settled board_update. play.js's handler in turn fires a
synthetic /view/goto POST (via syncCommentsVisibility opening the
commentary window), and that POST lands on the NEW server game --
corrupting its cursor to the old value.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.async_api")

from sturddle_view.app import create_app  # noqa: E402
from sturddle_view.config import Settings  # noqa: E402
from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import make_fake_uci, run_uvicorn  # noqa: E402


@pytest.fixture
def server(tmp_path):
    settings = Settings(token="test-token", auth_disabled=True)
    settings.pgn_dir = tmp_path / "pgn"
    registry = EngineRegistry(path=tmp_path / "engines.json")
    eng = registry.add(name="FakeEngine", path=make_fake_uci(tmp_path, "FakeEngine"))
    registry.select(eng.id)
    app = create_app(settings=settings, engine_registry=registry)
    with run_uvicorn(app) as (base, _s):
        yield base, app


_PGN_A = """\
[Event "A"]
[Site "?"]
[Date "????.??.??"]
[Round "?"]
[White "WA"]
[Black "BA"]
[Result "*"]

1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7 6. Re1 b5 7. Bb3 d6
8. c3 O-O 9. h3 Nb8 10. d4 *
"""

_PGN_B = """\
[Event "B"]
[Site "?"]
[Date "????.??.??"]
[Round "?"]
[White "WB"]
[Black "BB"]
[Result "*"]

1. d4 d5 2. c4 e6 3. Nc3 Nf6 4. Bg5 Be7 5. e3 O-O 6. Nf3 Nbd7
7. Rc1 c6 8. Bd3 dxc4 9. Bxc4 Nd5 10. Bxe7 *
"""


@pytest.mark.asyncio
async def test_replay_while_old_cursor_nonzero_does_not_corrupt_new_game(server, page):
    """Repro the production Replay bug: cursor=N on game A, import game B,
    remount play -- new game's cursor must stay at 0."""
    base, app = server

    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")

    # Ensure the failure mode's prerequisite: commentary window is on.
    # The bug only repros when syncCommentsVisibility() decides to open
    # the commentary window on board_update.
    await page.evaluate(
        "async () => { await fetch('/settings', {method:'PUT',"
        " headers:{'Content-Type':'application/json'},"
        " body: JSON.stringify({view_show_pgn_comments: true})}); }"
    )

    # Import game A and navigate cursor to a non-zero ply.
    import_status = await page.evaluate(
        "async (pgn) => { const r = await fetch('/game/import', {method:'POST',"
        " headers:{'Content-Type':'application/json'},"
        " body: JSON.stringify({text: pgn, format: 'pgn'})});"
        " return { status: r.status, body: await r.text() }; }",
        _PGN_A,
    )
    assert import_status["status"] == 200, f"import A failed: {import_status}"
    # Tell client to sync to the new game id (mirrors normal UI flow).
    await page.evaluate(
        "async () => { await fetch('/game/sync', {method:'POST'}); }"
    )
    goto_status = await page.evaluate(
        "async () => { const r = await fetch('/game/view/goto', {method:'POST',"
        " headers:{'Content-Type':'application/json'},"
        " body: JSON.stringify({ply: 10})});"
        " return { status: r.status, body: await r.text() }; }"
    )
    assert goto_status["status"] == 200, f"goto failed: {goto_status}"
    assert app.state.hve._view_cursor == 10, "precondition: game A cursor at 10"

    # Switch away from play (simulates user opening tournament window
    # while play perspective is unmounted -- as the Replay button does).
    await page.click('button[data-perspective="engines"]')
    await page.wait_for_function(
        "() => !!document.querySelector('#engines-perspective')"
        " && !document.querySelector('.perspective-root')?.classList.contains('is-pending')",
    )

    # Replay: import game B + activate play (mirror tournament-live-game).
    await page.evaluate(
        "async (pgn) => {"
        " await fetch('/game/import', {method:'POST',"
        "  headers:{'Content-Type':'application/json'},"
        "  body: JSON.stringify({text: pgn, format: 'pgn'})});"
        " window.dispatchEvent(new CustomEvent("
        "  'sturddle:activate-perspective', {detail:{id:'play'}}));"
        "}",
        _PGN_B,
    )
    # Wait for the play perspective to be fully re-mounted.
    await page.wait_for_function(
        "() => !!document.querySelector('#play-perspective')"
        " && !document.querySelector('.perspective-root')?.classList.contains('is-pending')",
    )
    # The mount issues POST /game/sync; its board_update is what fires
    # the buggy synthetic /view/goto. Issue ANOTHER /game/sync and wait
    # for its server response -- it's enqueued behind any HVE-locked
    # work the mount triggered, so by the time it resolves any spurious
    # /view/goto from the bug would already have executed.
    await page.evaluate(
        "async () => { await fetch('/game/sync', {method:'POST'}); }"
    )
    # One more flush: wait for the resulting board_update to reach the
    # client (view-controls visible reflects the latest server state).
    await page.wait_for_function(
        "() => getComputedStyle(document.querySelector('#view-controls'))"
        ".display !== 'none'",
    )

    # Assertion: server's view cursor on game B must be 0. Anything
    # else means a stale cached board_update from game A fired a
    # synthetic /view/goto against game B.
    assert app.state.hve._view_cursor == 0, (
        f"new game cursor corrupted to {app.state.hve._view_cursor}"
    )
