"""E2E: view-mode "Play from here" button.

Clicking #view-play-from-here forks a live play game from the current
cursor (plies 0..cursor) and exits view mode: the play ribbon replaces
the view ribbon. The click handler reads state.el.viewPlayFromHereBtn;
a prior refactor left that as a bare (out-of-scope) ref that threw on
click -- caught only by manual review, since no e2e drove the button.

Uses a real (fake) UCI engine so play_from_here can spawn it; the
fixture's /nonexistent engine would 400 the fork.

Skipped if Playwright is missing.
"""
from __future__ import annotations

import httpx
import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import (  # noqa: E402
    make_searching_fake_uci,
    run_uvicorn_subprocess,
    wait_perspective_ready,
)


PLAY_PERSP = "#play-perspective"
_PLAY_FROM_HERE = "#view-play-from-here"
_VIEW_FORWARD = "#view-forward"
_VIEW_CONTROLS = "#view-controls"
_PLAY_CONTROLS = "#board-controls"

_FORK_PLY = 2  # fork after 1. e4 e5 -- mid-game, button enabled (not over).

_PGN = (
    '[Event "?"]\n[White "W"]\n[Black "B"]\n[Result "*"]\n\n'
    '1. e4 e5 2. Nf3 Nc6 *\n'
)


@pytest.fixture
def server(tmp_path):
    registry_path = tmp_path / "engines.json"
    seed = EngineRegistry(path=registry_path)
    e = seed.add(name="FakeEngine", path=make_searching_fake_uci(tmp_path, "FakeEngine"))
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


def _import(base, text):
    resp = httpx.post(f"{base}/game/import", json={"text": text, "format": "pgn"})
    resp.raise_for_status()


def _hve_state(base) -> dict:
    return httpx.get(f"{base}/_test/hve/state").json()


async def _goto_view_mode(page, base):
    await page.goto(base + "/")
    await page.wait_for_selector(PLAY_PERSP)
    await wait_perspective_ready(page)
    await page.wait_for_function(
        f"() => getComputedStyle(document.querySelector('{_VIEW_CONTROLS}')).display !== 'none'",
    )


@pytest.mark.asyncio
async def test_play_from_here_exits_view_to_play(server, make_page):
    base = server
    _import(base, _PGN)
    _ctx, page, errors = await _new_page(make_page)
    await _goto_view_mode(page, base)

    # Advance to the fork ply (precise nav via the ribbon). Each click is
    # awaited, then we gate on the move list's highlighted cell reaching
    # index fork_ply-1 so the cursor is settled before the fork.
    for _ in range(_FORK_PLY):
        await page.click(_VIEW_FORWARD)
    await page.wait_for_function(
        """(ply) => {
          const cells = [...document.querySelectorAll('.move-cell')];
          const cur = cells.findIndex(c => c.classList.contains('is-current'));
          return cur === ply - 1;
        }""",
        arg=_FORK_PLY,
    )

    # Fork: view ribbon goes away, play ribbon shows.
    await page.click(_PLAY_FROM_HERE)
    await page.wait_for_function(
        f"() => getComputedStyle(document.querySelector('{_PLAY_CONTROLS}')).display !== 'none'"
        f" && getComputedStyle(document.querySelector('{_VIEW_CONTROLS}')).display === 'none'",
    )

    state = _hve_state(base)
    assert state["viewing"] is False
    assert state["n_plies"] == _FORK_PLY  # forked at the cursor
    assert errors == [], errors
