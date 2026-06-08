"""E2E: AI natural-completion -> ribbon transition.

When an AI analysis turn finishes naturally (ai_info done, not cancel /
error), the analyze ribbon button must drop its active ("Stop analysis")
state even though the server stays in ANALYSIS mode. A refactor once made
refreshButtons throw (out-of-scope `state` arg) so the button stayed
stuck active/pulsating -- caught only by live use, not the suite.

Pure bus-driven via /_test/ai/publish_event: a synthetic board_update
turns analysis on, then ai_info done turns the turn finished. No real
engine, no LLM agent loop -- the client reacts to the bus identically.

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
_VIEW_ANALYZE_BTN = "#view-analyze"
_ACTIVE_CLASS = "is-active"

_PGN = (
    '[Event "?"]\n[Site "?"]\n[Date "????.??.??"]\n[Round "?"]\n'
    '[White "W"]\n[Black "B"]\n[Result "*"]\n\n'
    '1. e4 e5 2. Nf3 Nc6 *\n'
)


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


def _seed_view_mode(base) -> tuple[str, dict]:
    """Import a PGN (enters view mode) and return (game_id, view payload)."""
    resp = httpx.post(f"{base}/game/import", json={"text": _PGN, "format": "pgn"})
    resp.raise_for_status()
    game_id = resp.json()["game_id"]
    state = httpx.get(f"{base}/_test/hve/state").json()
    # Minimal faithful view payload: the client handler reads cursor /
    # total_plies and defaults the rest. n_plies/move stack come from
    # the imported game so the synthetic board_update stays consistent.
    view = {"cursor": state["n_plies"], "total_plies": state["n_plies"]}
    return game_id, view


def _publish(base, kind, *, game_id, payload):
    resp = httpx.post(
        f"{base}/_test/ai/publish_event",
        json={"kind": kind, "game_id": game_id, "payload": payload},
    )
    resp.raise_for_status()


async def _goto_view_mode(page, base):
    await page.goto(base + "/")
    await page.wait_for_selector(PLAY_PERSP)
    await page.wait_for_function(
        "() => getComputedStyle(document.querySelector('#view-controls')).display !== 'none'",
    )


async def _wait_btn_active(page, expected: bool, timeout_ms=3000):
    await page.wait_for_function(
        f"""(want) => {{
          const b = document.querySelector('{_VIEW_ANALYZE_BTN}');
          return !!b && b.classList.contains('{_ACTIVE_CLASS}') === want;
        }}""",
        arg=expected,
        timeout=timeout_ms,
    )


@pytest.mark.asyncio
async def test_ai_done_clears_active_ribbon(server, make_page):
    base = server
    game_id, view = _seed_view_mode(base)
    _ctx, page, errors = await _new_page(make_page)
    await _goto_view_mode(page, base)

    # Analysis on: ribbon analyze button shows active ("Stop analysis").
    _publish(base, "board_update", game_id=game_id, payload={
        "analyzing": True, "editing": False, "view": view, "human_white": None,
    })
    await _wait_btn_active(page, True)

    # Natural completion: turn finished, button must drop active even
    # though analyzing stays true (server still in ANALYSIS). This is the
    # exact transition the refreshButtons regression broke.
    _publish(base, "ai_info", game_id=game_id, payload={"done": True})
    await _wait_btn_active(page, False)

    assert errors == [], errors
