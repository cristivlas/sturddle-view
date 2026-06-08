"""E2E: cross-game (fork) navigation toasts in view mode.

When a viewed game has a parent (or live children) and the cursor lands
precisely on the fork ply, a neutral toast appears -- "Forked from <parent>"
or "N variations from this position". The build/refresh logic is pure DOM
construction reading state.xgame.*; refactor bare-ref bugs there pass parse
AND the rest of the suite (these paths had none) and surface only on live use.

Seeded via /_test/recents/seed_fork (real RecentImports.save). The child's
PGN is then imported normally -- it reuses the seeded game_id, so by-id
serves the fork link for a real view-mode game. Navigation is driven by
clicking the ribbon forward button so lastViewNavKind stays "precise"
(the gating condition).

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
_VIEW_FORWARD = "#view-forward"
_XGAME_TOAST = ".xgame-toast"

_FORK_PLY = 2  # child diverges from parent after 1. e4 e5 (2 plies).

# Parent and child share the first two plies; the child forks at ply 2.
# Distinct player names make the toast label assertable.
_PARENT_PGN = (
    '[Event "?"]\n[White "ParentW"]\n[Black "ParentB"]\n[Result "*"]\n\n'
    '1. e4 e5 2. Nf3 Nc6 *\n'
)
_CHILD_PGN = (
    '[Event "?"]\n[White "ChildW"]\n[Black "ChildB"]\n[Result "*"]\n\n'
    '1. e4 e5 2. Bc4 Bc5 *\n'
)
_PARENT_ID = "parent-xg"
_CHILD_ID = "child-xg"


@pytest.fixture
def server(tmp_path):
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


def _seed_fork(base):
    resp = httpx.post(f"{base}/_test/recents/seed_fork", json={
        "parent": {"game_id": _PARENT_ID, "text": _PARENT_PGN,
                   "summary": {"white": "ParentW", "black": "ParentB", "result": "*"}},
        "child": {"game_id": _CHILD_ID, "text": _CHILD_PGN, "fork_ply": _FORK_PLY,
                  "summary": {"white": "ChildW", "black": "ChildB", "result": "*"}},
    })
    resp.raise_for_status()


def _import(base, text) -> str:
    resp = httpx.post(f"{base}/game/import", json={"text": text, "format": "pgn"})
    resp.raise_for_status()
    return resp.json()["game_id"]


async def _goto_view_mode(page, base):
    await page.goto(base + "/")
    await page.wait_for_selector(PLAY_PERSP)
    await page.wait_for_function(
        "() => getComputedStyle(document.querySelector('#view-controls')).display !== 'none'",
    )


async def _toast_count(page) -> int:
    return await page.eval_on_selector_all(_XGAME_TOAST, "els => els.length")


@pytest.mark.asyncio
async def test_parent_toast_appears_at_fork_ply(server, make_page):
    base = server
    _seed_fork(base)
    gid = _import(base, _CHILD_PGN)
    assert gid == _CHILD_ID  # reused the seeded id (first-save-wins)
    _ctx, page, errors = await _new_page(make_page)
    await _goto_view_mode(page, base)

    # Off the fork ply: no x-game toast.
    assert await _toast_count(page) == 0

    # Land precisely on the fork ply -> "Forked from ParentW vs ParentB".
    await page.click(_VIEW_FORWARD)
    await page.click(_VIEW_FORWARD)
    await page.wait_for_selector(_XGAME_TOAST)
    txt = await page.text_content(_XGAME_TOAST)
    assert "ParentW vs ParentB" in txt, txt

    # Navigate off the fork ply -> toast auto-closes (not a dismiss).
    await page.click("#view-back")
    await page.wait_for_function(
        f"() => document.querySelectorAll('{_XGAME_TOAST}').length === 0",
    )
    assert errors == [], errors


@pytest.mark.asyncio
async def test_children_toast_appears_on_parent_at_fork_ply(server, make_page):
    base = server
    _seed_fork(base)
    gid = _import(base, _PARENT_PGN)
    assert gid == _PARENT_ID
    _ctx, page, errors = await _new_page(make_page)
    await _goto_view_mode(page, base)

    assert await _toast_count(page) == 0

    # Parent at the child's fork ply -> "1 variation from this position".
    await page.click(_VIEW_FORWARD)
    await page.click(_VIEW_FORWARD)
    await page.wait_for_selector(_XGAME_TOAST)
    txt = await page.text_content(_XGAME_TOAST)
    assert "1 variation from this position" in txt, txt
    assert errors == [], errors
