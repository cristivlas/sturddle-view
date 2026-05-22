"""Regression test: entering edit mode from play must land the view
cursor at the last ply (the live position the user wants to edit), not
at ply 0.

Bug: with view_show_pgn_comments=true, syncCommentsVisibility's
seeding POST /view/goto fires on the first board_update (cursor=0
from enter_view_mode), overwriting the cursor=n produced by
view_last() in /game/view/start.

Adjacent paths whose existing behavior must be preserved:
  * /game/import (PGN/FEN import) lands at cursor=0.
  * Tournament Replay (import + activate-perspective) lands at cursor=0.

Synchronization: tests are timeout-free. We observe HTTP requests via
Playwright's CDP-level request/requestfinished events (no JS injection
needed -- so no leaked listeners) and await the request lifecycles that
deterministically bound when the cursor would have been mutated."""
from __future__ import annotations

import asyncio
import json as _json
import stat
import sys
from pathlib import Path

import httpx
import pytest

pytest.importorskip("playwright.async_api")

from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import run_uvicorn_subprocess, wait_perspective_ready  # noqa: E402


class PageObserver:
    """Tracks HTTP request lifecycles and WS board_update frames for a
    Playwright page. All observation happens on the Playwright (CDP) side
    -- no JS injection, so nothing to clean up in the page itself.

    Helpers (all timeout-free):
      * ``wait_quiet(substr)`` -- await zero in-flight requests whose
        URL contains ``substr``. Pre-registers the substring so a later
        request can be counted.
      * ``wait_board_update(predicate)`` -- await the next board_update
        whose payload satisfies ``predicate(payload)``. Re-checks the
        last observed payload first, so a state already reached resolves
        immediately.
    """

    def __init__(self, page):
        self._page = page
        self._inflight: dict[str, int] = {}
        self._quiet_waiters: list[tuple[str, asyncio.Future]] = []
        self._last_board: dict | None = None
        self._board_waiters: list[tuple[callable, asyncio.Future]] = []
        page.on("request", self._on_request)
        page.on("requestfinished", self._on_finished)
        page.on("requestfailed", self._on_finished)
        page.on("websocket", self._on_websocket)

    def _on_request(self, req):
        for key in self._inflight:
            if key in req.url:
                self._inflight[key] += 1

    def _on_finished(self, req):
        for key in list(self._inflight):
            if key in req.url:
                self._inflight[key] = max(0, self._inflight[key] - 1)
                if self._inflight[key] == 0:
                    still = []
                    for k, f in self._quiet_waiters:
                        if k == key and not f.done():
                            f.set_result(None)
                        else:
                            still.append((k, f))
                    self._quiet_waiters = still

    def _on_websocket(self, ws):
        ws.on("framereceived", self._on_frame)

    def _on_frame(self, frame):
        try:
            ev = _json.loads(frame)
        except Exception:
            return
        if ev.get("kind") != "board_update":
            return
        payload = ev.get("payload") or {}
        self._last_board = payload
        still = []
        for pred, fut in self._board_waiters:
            if not fut.done() and pred(payload):
                fut.set_result(payload)
            else:
                still.append((pred, fut))
        self._board_waiters = still

    def track(self, substr: str) -> None:
        self._inflight.setdefault(substr, 0)

    async def wait_quiet(self, substr: str) -> None:
        self.track(substr)
        if self._inflight[substr] == 0:
            return
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self._quiet_waiters.append((substr, fut))
        await fut

    async def wait_board_update(self, predicate):
        """Resolve to the first board_update payload matching predicate.

        Checks the last observed payload first so callers don't race."""
        if self._last_board is not None and predicate(self._last_board):
            return self._last_board
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self._board_waiters.append((predicate, fut))
        return await fut


def _make_fake_uci(root: Path, name: str) -> str:
    py = root / f"{name}.py"
    py.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "while True:\n"
        "    line = sys.stdin.readline()\n"
        "    if not line: break\n"
        "    line = line.strip()\n"
        f"    if line == 'uci': sys.stdout.write('id name {name}\\nuciok\\n'); sys.stdout.flush()\n"
        "    elif line == 'isready': sys.stdout.write('readyok\\n'); sys.stdout.flush()\n"
        "    elif line.startswith('go'):\n"
        "        sys.stdout.write('bestmove e7e5\\n'); sys.stdout.flush()\n"
        "    elif line == 'quit': break\n"
    )
    if sys.platform.startswith("win"):
        wrapper = root / f"{name}.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{py}" %*\r\n')
        return str(wrapper)
    py.chmod(py.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(py)


@pytest.fixture
def server(tmp_path):
    registry_path = tmp_path / "engines.json"
    seed = EngineRegistry(path=registry_path)
    eng = seed.add(name="FakeEngine", path=_make_fake_uci(tmp_path, "FakeEngine"))
    seed.select(eng.id)
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

    # Click Edit position; accept the confirm dialog (a wa-button whose
    # label is "Edit position", distinct from the ribbon button id).
    await page.click("#edit-pos")
    await page.wait_for_function(
        "() => Array.from(document.querySelectorAll('wa-button'))"
        ".some(b => /Edit position/.test(b.textContent || ''))"
    )
    await page.evaluate(
        "() => Array.from(document.querySelectorAll('wa-button'))"
        ".find(b => /Edit position/.test(b.textContent || '')).click()"
    )
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
