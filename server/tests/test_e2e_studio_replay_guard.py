"""E2E: Studio Games-tab click must confirm before clobbering an
in-progress play game -- even on a fresh page load where the Play
perspective never mounted.

Repro for the reload bug: play HVE, close/reopen the browser straight
into Studio, click a recorded game -> the import silently overwrote the
game in play because the client-side isPlayInProgress mirror was never
seeded. The guard must ask the server, not a mirror.
"""
from __future__ import annotations

import httpx
import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.engines import EngineRegistry  # noqa: E402
from sturddle_view.tournament.store import TournamentStore  # noqa: E402

from .conftest import (  # noqa: E402
    TOURNAMENT_UX_KEY,
    TOURNAMENT_UX_STUDIO,
    run_uvicorn_subprocess,
    wait_perspective_ready,
    watch_page_errors,
)

FAKE_ENGINE_PATH = "/nonexistent/engine"
PERSPECTIVE_LS_KEY = "sturddle:active-perspective"
DISCARD_MSG = "Discard your in-progress game"
PLAY_MOVES = ["e2e4", "e7e5"]

_GAME_PGN = "\n".join([
    '[Event "test"]',
    '[Round "1"]',
    '[White "engine-A"]',
    '[Black "engine-B"]',
    '[Result "1-0"]',
    "",
    "1. e4 e5 1-0",
    "",
])


def _seed_tournament(tmp_path):
    """One finished game on disk so the Studio Games tab has a row."""
    store = TournamentStore(tmp_path / "tournaments")
    t = store.create(
        name="replay-guard",
        template={"tc": "5+0.05"},
        engines=[
            {"name": "engine-A", "cmd": "/bin/a"},
            {"name": "engine-B", "cmd": "/bin/b"},
        ],
    )
    store.pgn_path(t.id).write_text(_GAME_PGN, encoding="utf-8")


@pytest.mark.asyncio
async def test_studio_replay_confirms_without_play_mount(tmp_path, make_page):
    registry_path = tmp_path / "engines.json"
    seed = EngineRegistry(path=registry_path)
    e = seed.add(name="MyEngine", path=FAKE_ENGINE_PATH)
    seed.select(e.id)
    _seed_tournament(tmp_path)
    env = {
        "SV_PGN_DIR": str(tmp_path / "pgn"),
        "SV_TOURNAMENT_ROOT": str(tmp_path / "tournaments"),
        "SV_ENGINE_REGISTRY_PATH": str(registry_path),
        "SV_IMPORTS_DIR": str(tmp_path / "imports"),
        "SV_SETTINGS_FILE": str(tmp_path / "settings.json"),
        "SV_GAME_STATE_PATH": str(tmp_path / "current_game.json"),
    }
    with run_uvicorn_subprocess(env_overrides=env) as base:
        # In-progress HVE game on the server (2 plies, human to move).
        httpx.post(
            f"{base}/_test/hve/install",
            json={
                "engine_path": FAKE_ENGINE_PATH,
                "human_white": True,
                "moves_uci": PLAY_MOVES,
            },
        ).raise_for_status()

        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        errors = watch_page_errors(page)

        # Boot straight into Studio -- the Play perspective never mounts,
        # so no client-side mirror of the play state exists.
        await page.add_init_script(
            f"localStorage.setItem('{TOURNAMENT_UX_KEY}', '{TOURNAMENT_UX_STUDIO}');"
            f"localStorage.setItem('{PERSPECTIVE_LS_KEY}', 'engines')"
        )
        await page.goto(base + "/")
        await page.wait_for_selector(".studio-panel")
        await wait_perspective_ready(page)

        await page.click('.studio-bottom-right wa-tab[panel="history"]')
        await page.wait_for_selector(".studio-history-row")
        await page.click(".studio-history-row")

        # The reload bug: no confirm appeared and the game was clobbered.
        await page.wait_for_selector(".confirm-message")
        msg = await page.text_content(".confirm-message")
        assert DISCARD_MSG in msg, f"unexpected confirm text: {msg!r}"

        # Cancel must leave the server-side game untouched.
        await page.click('wa-dialog wa-button:has-text("Cancel")')
        await page.wait_for_selector(".confirm-message", state="detached")
        state = httpx.get(f"{base}/_test/hve/state").json()
        assert state["viewing"] is False
        assert state["move_stack_uci"] == PLAY_MOVES

        assert errors == [], "JS errors:\n" + "\n".join(errors)
