"""e2e: clicking a row in the Engines window opens a live
game window that subscribes to a proxy and renders the board.

Uses a fake fastchess (a sleeping Python script) and injects proxy
lines directly via the internal HTTP endpoint, so no real engine
binaries are required.

The single-proxy attach goes through the Engines window (proxy_id
WS path), not Live Games (game_id WS path). Live Games requires two
proxies to confirm a pair and is covered by other tests.

Also covers the window surviving a dropped socket (mobile backgrounding).
"""
from __future__ import annotations

import asyncio
import json
from urllib.parse import urlparse

import pytest
from httpx import AsyncClient

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.chess.results import UNKNOWN_TERMINATION  # noqa: E402
from sturddle_view.tournament.pgn_reconcile import ReconciledMatch  # noqa: E402

from .conftest import (  # noqa: E402
    LIVE_ENGINE_A,
    LIVE_ENGINE_B,
    LIVE_PROXY_ID,
    TOURNAMENT_UX_KEY,
    TOURNAMENT_UX_STUDIO,
    open_arena_engines_window,
    pin_arena_tournament_ux,
    post_proxy,
    running_tournament,
    wait_perspective_ready,
)


_PROXY_ID_BLACK = "proxy-black"
_GAME_WINDOW = ".winbox.sturddle-wb-live-game"
_FROZEN_WINDOW = ".winbox.sturddle-wb-live-frozen"
_RECONCILED_RESULT = "1-0"
# Moves deliberately differ from the watched pair's, so the live PGN tailer
# can't race the test's own reconcile.
_RECONCILED_PGN = "\n".join([
    '[Event "live-e2e"]',
    '[Round "1"]',
    f'[White "{LIVE_ENGINE_A}"]',
    f'[Black "{LIVE_ENGINE_B}"]',
    f'[Result "{_RECONCILED_RESULT}"]',
    "",
    f"1. d4 d5 {_RECONCILED_RESULT}",
    "",
])
_PERSPECTIVE_LS_KEY = "sturddle:active-perspective"
_STUDIO_SELECTED_ID_KEY = "sturddle:studio:selectedId"
_STUDIO_BOARDS_PREFIX = "sturddle:studio:boards:"
_WORKSPACE_PREFIX = "sturddle:workspace:"
_PROXY_WS_PATTERN = "**/ws/tournament/proxy/**"
_SETTINGS_PATH = "/settings"
_GET = "GET"
# Any style but the client default, so a board mounted before the
# setting arrived is told apart from one that waited for it.
_BOARD_STYLE = "green"


async def _open_page(make_page):
    _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    page.on("console", lambda msg: page_errors.append(
        f"console.{msg.type}: {msg.text}"
    ) if msg.type == "error" else None)
    return page, page_errors


async def _watch_proxy(page, base, secret):
    """Open the workspace, watch the proxy from the Engines window, and
    stream a first position/go/info so the board and eval render."""
    await open_arena_engines_window(page, base)

    # Click the watch button -> live game window opens.
    await page.click(
        ".wb-engines .wb-sched-list .wb-sched-live .wb-sched-attach-btn"
    )
    # Window opens on watch click. The body is revealed only once the
    # board's first position lands (paint-before-reveal gating), so
    # assert the board is attached here and check visibility after the
    # position stream below.
    await page.wait_for_selector(".wb-livegame .lg-board", state="attached")

    # Drive the proxy stream:
    #   1. position -> engine learns it's playing Black (FEN
    #      side-to-move = "b").
    #   2. go -> triggers orientation flip + clock display.
    #   3. info -> eval score renders.
    await post_proxy(base, secret, [
        "position startpos moves e2e4",
        "go wtime 300000 btime 300000",
        "info depth 12 score cp 35 pv e7e5",
    ])

    # The first position resolves board.ready, which reveals the body.
    await page.wait_for_selector(".wb-livegame .lg-board", state="visible")
    return await _wait_eval_change(page, "")


async def _wait_eval_change(page, previous):
    """Wait for the own-side eval text to differ from `previous`; return it."""
    handle = await page.wait_for_function(
        """(previous) => {
            const e = document.querySelector('.wb-livegame .lg-eval-score-bottom');
            const text = e ? e.textContent : '';
            return text !== '' && text !== previous ? text : null;
        }""",
        arg=previous,
    )
    return await handle.json_value()


@pytest.mark.asyncio
async def test_live_game_window_attaches_during_run(tmp_path, monkeypatch, make_page):
    async with running_tournament(tmp_path, monkeypatch) as (app, base, t, secret):
        page, page_errors = await _open_page(make_page)
        await _watch_proxy(page, base, secret)

        # Bug 4 regression check: when the engine plays Black,
        # the board must orient with Black at the bottom.
        # cm-chessboard's setOrientation goes through an async
        # animation queue, so wait for it to settle before
        # reading the rendered coord labels.
        top_rank_label = await page.wait_for_function(
            """() => {
                const root = document.querySelector('.wb-livegame .lg-board');
                if (!root) return null;
                const ranks = [...root.querySelectorAll('text.coordinate.rank')];
                if (!ranks.length) return null;
                ranks.sort((a, b) => parseFloat(a.getAttribute('y')) - parseFloat(b.getAttribute('y')));
                const top = ranks[0].textContent.trim();
                // Black-at-bottom => topmost rank label is "1".
                return top === "1" ? top : false;
            }""",
        )
        top_rank_label = await top_rank_label.json_value()
        assert top_rank_label == "1", (
            f"Bug 4 regression: expected top rank label '1' "
            f"(black-at-bottom), got {top_rank_label!r}"
        )

        # Waiting overlay must match a filled result banner's height.
        # Review button is pulled out of layout: a post-reconcile
        # addition the waiting box is not meant to match.
        wait_h, result_h = await page.evaluate(
            """() => {
                const wait = document.querySelector('.wb-livegame .lg-waiting-overlay');
                const result = document.querySelector('.wb-livegame .lg-result-overlay');
                const btn = result.querySelector('.lg-result-replay');
                result.querySelector('.lg-result-score').textContent = '1-0';
                result.querySelector('.lg-result-termination').textContent = 'checkmate';
                wait.hidden = false;
                result.hidden = false;
                btn.style.display = 'none';
                const h = [
                    wait.getBoundingClientRect().height,
                    result.getBoundingClientRect().height,
                ];
                btn.style.display = '';
                wait.hidden = true;
                result.hidden = true;
                return h;
            }"""
        )
        assert abs(wait_h - result_h) < 0.5, (
            f"overlay heights diverged: waiting {wait_h}px vs result {result_h}px"
        )

        # Close-on-terminal: stopping the tournament closes
        # stale live windows (proxy-id attaches are always
        # stale) but keeps standard windows so the user can
        # review final state. 5 .winbox up before stop:
        # Standings + Live Games + Event log + Engines (4
        # standard) + 1 live (watch). After stop: 4 standard,
        # 0 live.
        assert await page.locator(".winbox.sturddle-wb").count() == 5
        assert await page.locator(".winbox.sturddle-wb-live").count() == 1

        await app.state.tournament_orch.stop(t.id)

        # Live window goes away (proxy-id, stale on terminal).
        await page.wait_for_function(
            "() => document.querySelectorAll('.winbox.sturddle-wb-live').length === 0",
        )
        # Standard windows remain.
        assert await page.locator(".winbox.sturddle-wb").count() == 4

        assert page_errors == [], "JS errors:\n" + "\n".join(page_errors)


@pytest.mark.asyncio
async def test_live_game_window_survives_dropped_socket(tmp_path, monkeypatch, make_page):
    """Mobile backgrounding drops the watcher socket; the window must stay
    open, reconnect, and repaint from the server's snapshot replay."""
    async with running_tournament(tmp_path, monkeypatch) as (app, base, t, secret):
        page, page_errors = await _open_page(make_page)
        # (page side, server side) per watcher connection.
        connections = []
        reconnected = asyncio.Event()

        def route_proxy_ws(route):
            connections.append((route, route.connect_to_server()))
            if len(connections) > 1:
                reconnected.set()

        await page.route_web_socket(_PROXY_WS_PATTERN, route_proxy_ws)
        first_eval = await _watch_proxy(page, base, secret)

        page_side, server_side = connections[0]
        # Playwright's server-side close is synchronous; the page side's is not.
        server_side.close()
        await page_side.close()
        # Whichever comes first: the reconnect, or the window closing. The
        # latter's default Playwright timeout bounds the wait if neither fires.
        reconnect = asyncio.ensure_future(reconnected.wait())
        window_gone = asyncio.ensure_future(page.wait_for_selector(
            ".winbox.sturddle-wb-live", state="detached",
        ))
        await asyncio.wait({reconnect, window_gone}, return_when=asyncio.FIRST_COMPLETED)
        for f in (reconnect, window_gone):
            if f.done():
                f.exception()  # retrieve, so a timeout can't warn unretrieved
            else:
                f.cancel()
        assert reconnected.is_set(), "watch window closed instead of reconnecting"

        # Reaches the window via the live feed or the snapshot replay.
        await post_proxy(base, secret, ["info depth 14 score cp 80 pv e7e5"])
        await _wait_eval_change(page, first_eval)
        assert await page.locator(".winbox.sturddle-wb-live").count() == 1
        assert page_errors == [], "JS errors:\n" + "\n".join(page_errors)


async def _finished_game(app, base, secret):
    """Confirm a white/black pair, then end its game via ucinewgame (the
    pair dissolves server-side). Returns the pair id."""
    orch = app.state.tournament_orch
    await post_proxy(base, secret, [], proxy_id=_PROXY_ID_BLACK, engine_name=LIVE_ENGINE_B)
    await post_proxy(base, secret, ["position startpos", "bestmove e2e4"])
    await post_proxy(base, secret, ["position startpos moves e2e4"], proxy_id=_PROXY_ID_BLACK)
    pair_id = next(iter(orch._pair_proxies))
    await post_proxy(base, secret, ["ucinewgame"])
    assert pair_id not in orch._pair_proxies
    return pair_id


def _saved_game_board(pair_id):
    return {
        "proxyId": LIVE_PROXY_ID, "gameId": pair_id,
        "label": f"{LIVE_ENGINE_A} vs {LIVE_ENGINE_B}", "engineName": LIVE_ENGINE_A,
        "min": False, "max": False,
    }


async def _reconcile_while_away(app, t, pair_id):
    """Resolve the ended game (PGN on disk + game_reconciled in the event
    history) before any page connects."""
    app.state.tournament_store.pgn_path(t.id).write_text(_RECONCILED_PGN, encoding="utf-8")
    await app.state.tournament_orch._emit_reconciled(ReconciledMatch(
        pair_id=pair_id, white_proxy=LIVE_PROXY_ID, black_proxy=_PROXY_ID_BLACK,
        white_engine=LIVE_ENGINE_A, black_engine=LIVE_ENGINE_B,
        pgn_white=LIVE_ENGINE_A, pgn_black=LIVE_ENGINE_B,
        result=_RECONCILED_RESULT, termination=UNKNOWN_TERMINATION,
        game_n=1, ply_count=2,
    ))


async def _load_studio(page, base, t, board):
    await page.add_init_script(
        f"localStorage.setItem({json.dumps(TOURNAMENT_UX_KEY)}, {json.dumps(TOURNAMENT_UX_STUDIO)});"
        f"localStorage.setItem({json.dumps(_PERSPECTIVE_LS_KEY)}, 'engines');"
        f"localStorage.setItem({json.dumps(_STUDIO_SELECTED_ID_KEY)}, {json.dumps(t.id)});"
        f"localStorage.setItem({json.dumps(_STUDIO_BOARDS_PREFIX + t.id)},"
        f" {json.dumps(json.dumps([board]))});"
    )
    await page.goto(f"{base}/")
    await page.wait_for_selector(".studio-panel")
    await wait_perspective_ready(page)


async def _load_arena(page, base, t, board, before_open=None):
    await pin_arena_tournament_ux(page)
    saved = {"live": [{
        **board, "x": "262px", "y": "108px", "width": "280px", "height": "396px", "z": 15,
    }]}
    await page.add_init_script(
        f"localStorage.setItem({json.dumps(_WORKSPACE_PREFIX + t.id)},"
        f" {json.dumps(json.dumps(saved))});"
    )
    await page.goto(f"{base}/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)
    await page.click('button[data-perspective="engines"]')
    await page.click(".tournament-row")
    if before_open is not None:
        before_open()
    await page.click(".tournaments-ribbon .t-workspace")


_LOADERS = pytest.mark.parametrize("load_ui", [_load_studio, _load_arena], ids=["studio", "arena"])


async def _studio_saved_resolved(page, t):
    return await page.evaluate(
        "(k) => JSON.parse(localStorage.getItem(k))[0].resolved ?? null",
        _STUDIO_BOARDS_PREFIX + t.id,
    )


async def _arena_saved_resolved(page, t):
    # Arena persists its snapshot on beforeunload.
    return await page.evaluate(
        """(k) => {
            window.dispatchEvent(new Event('beforeunload'));
            return JSON.parse(localStorage.getItem(k)).live[0].resolved ?? null;
        }""",
        _WORKSPACE_PREFIX + t.id,
    )


_UIS = pytest.mark.parametrize(
    "load_ui, saved_resolved",
    [(_load_studio, _studio_saved_resolved), (_load_arena, _arena_saved_resolved)],
    ids=["studio", "arena"],
)


@_LOADERS
@pytest.mark.asyncio
async def test_reload_restores_finished_game_board(tmp_path, monkeypatch, make_page, load_ui):
    """Mobile discards the backgrounded tab; on reload a saved board whose
    game ended while away must come back with its result banner."""
    async with running_tournament(tmp_path, monkeypatch) as (app, base, t, secret):
        pair_id = await _finished_game(app, base, secret)
        page, page_errors = await _open_page(make_page)
        await load_ui(page, base, t, _saved_game_board(pair_id))
        await page.wait_for_selector(f"{_GAME_WINDOW} .lg-result-overlay", state="visible")
        assert await page.locator(_GAME_WINDOW).count() == 1
        assert page_errors == [], "JS errors:\n" + "\n".join(page_errors)


@_LOADERS
@pytest.mark.asyncio
async def test_reload_restores_reconciled_game_board_frozen(tmp_path, monkeypatch, make_page, load_ui):
    """Reconciled while away: the backfilled game_reconciled resolves the
    board, so it reopens frozen (with Replay) instead of live."""
    async with running_tournament(tmp_path, monkeypatch) as (app, base, t, secret):
        pair_id = await _finished_game(app, base, secret)
        await _reconcile_while_away(app, t, pair_id)
        page, page_errors = await _open_page(make_page)
        await load_ui(page, base, t, _saved_game_board(pair_id))
        await page.wait_for_selector(f"{_FROZEN_WINDOW} .lg-result-replay", state="visible")
        assert await page.locator(f"{_FROZEN_WINDOW} .lg-result-score").text_content() == _RECONCILED_RESULT
        assert await page.locator(".winbox.sturddle-wb-live").count() == 1
        assert page_errors == [], "JS errors:\n" + "\n".join(page_errors)


@_UIS
@pytest.mark.asyncio
async def test_reload_resolves_board_whose_reconcile_left_history(
    tmp_path, monkeypatch, make_page, load_ui, saved_resolved,
):
    """The REST event history is a capped ring: after a long absence the
    game's game_reconciled is gone, so the board reattaches live. The
    replay's end message must still carry the game number (Review) and the
    opponent's name, and the resolution must persist for the next reload."""
    async with running_tournament(tmp_path, monkeypatch) as (app, base, t, secret):
        pair_id = await _finished_game(app, base, secret)
        await _reconcile_while_away(app, t, pair_id)
        # Stand-in for ring eviction after many later games.
        app.state.tournament_orch._event_history[t.id].clear()
        page, page_errors = await _open_page(make_page)
        await load_ui(page, base, t, _saved_game_board(pair_id))

        await page.wait_for_selector(f"{_GAME_WINDOW} .lg-result-replay", state="visible")
        assert await page.locator(f"{_GAME_WINDOW} .lg-result-score").text_content() == _RECONCILED_RESULT
        assert await page.locator(f"{_GAME_WINDOW} .lg-top-name").text_content() == LIVE_ENGINE_B
        resolved = await saved_resolved(page, t)
        assert resolved == {
            "gameN": 1, "result": _RECONCILED_RESULT, "termination": UNKNOWN_TERMINATION,
        }
        assert page_errors == [], page_errors


@pytest.mark.asyncio
async def test_arena_restore_waits_for_board_style(tmp_path, monkeypatch, make_page):
    """Arena restores saved boards once the tournament detail and event
    history are in. On a slow link the board-style fetch can land after
    them: the restored board must still wear the saved style."""
    async with running_tournament(tmp_path, monkeypatch) as (app, base, t, secret):
        async with AsyncClient(base_url=base) as http:
            r = await http.put(_SETTINGS_PATH, json={"board_style": _BOARD_STYLE})
            assert r.is_success, r.text
        pair_id = await _finished_game(app, base, secret)
        page, page_errors = await _open_page(make_page)

        # Hold the workspace's settings GET until the restore inputs land.
        armed = False
        release = asyncio.Event()
        restore_inputs = {f"/api/tournaments/{t.id}", f"/api/tournaments/{t.id}/events"}
        finished: set[str] = set()

        async def hold_settings(route):
            if armed and route.request.method == _GET:
                await release.wait()
            await route.continue_()

        def on_finished(request):
            path = urlparse(request.url).path
            if armed and path in restore_inputs:
                finished.add(path)
                if finished == restore_inputs:
                    release.set()

        def arm():
            nonlocal armed
            armed = True

        await page.route(lambda url: urlparse(url).path == _SETTINGS_PATH, hold_settings)
        page.on("requestfinished", on_finished)
        await _load_arena(page, base, t, _saved_game_board(pair_id), before_open=arm)

        board = page.locator(f"{_GAME_WINDOW} svg.cm-chessboard")
        await board.wait_for(state="attached")
        classes = (await board.get_attribute("class")).split()
        assert _BOARD_STYLE in classes, classes
        assert page_errors == [], "JS errors:\n" + "\n".join(page_errors)
