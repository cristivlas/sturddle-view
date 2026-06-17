"""E2E regression: thinking-duration label on the replay-render path.

The server attaches the measured ``thinking_ms`` to a round's first prose
delta -- which may be whitespace-only (a leading ``"\n\n"``). The client
strips that leading whitespace; the bug dropped ``thinking_ms`` along with
it, so the label fell back to a client Date.now() delta. Live that delta is
the real elapsed; on replay every event fires at once, collapsing it to
"Thought for 1s".

Driven by seeding the coordinator's replay buffer (the same dicts a real
turn buffers) then loading the page -- no real LLM agent, no engine. The
page rehydrates the panel via GET /game/analysis/replay exactly as a client
reconnecting after a turn would.

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
_THINKING_SUMMARY = ".play-ai-rounds .play-ai-thinking summary"

# Large enough that the real server duration ("8s") is unmistakable next to
# the buggy Date.now()-collapse fallback ("1s").
_THINKING_MS = 8000
_EXPECTED_LABEL = "Thought for 8s"
_BUG_LABEL = "Thought for 1s"

_PGN = (
    '[Event "?"]\n[Site "?"]\n[Date "????.??.??"]\n[Round "?"]\n'
    '[White "W"]\n[Black "B"]\n[Result "*"]\n\n'
    '1. e4 e5 2. Nf3 Nc6 *\n'
)


# Mirrors the verified server output for a thinking round whose first text
# chunk is whitespace: thinking_ms rides that whitespace delta, not the real
# prose that follows. seq drives the client replay-dedupe protocol.
def _replay_events(game_id: str) -> list[dict]:
    return [
        {"kind": "ai_thinking", "game_id": game_id,
         "payload": {"delta": "weighing the candidate moves...", "round": 0, "seq": 1}},
        {"kind": "ai_info", "game_id": game_id,
         "payload": {"delta": "\n\n", "round": 0, "thinking_ms": _THINKING_MS, "seq": 2}},
        {"kind": "ai_info", "game_id": game_id,
         "payload": {"delta": "The knight eyes d4.", "round": 0, "seq": 3}},
        {"kind": "ai_info", "game_id": game_id,
         "payload": {"done": True, "seq": 4}},
    ]


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


def _seed_view_mode(base) -> str:
    resp = httpx.post(f"{base}/game/import", json={"text": _PGN, "format": "pgn"})
    resp.raise_for_status()
    return resp.json()["game_id"]


def _seed_replay(base, events: list[dict]) -> None:
    resp = httpx.post(f"{base}/_test/ai/seed_replay", json={"events": events})
    resp.raise_for_status()


@pytest.mark.asyncio
async def test_replay_shows_server_thinking_duration_not_one_second(server, make_page):
    base = server
    game_id = _seed_view_mode(base)
    # Prime the buffer BEFORE load so the mount-time rehydrate renders it.
    _seed_replay(base, _replay_events(game_id))

    _ctx, page = await make_page(viewport={"width": 1600, "height": 1000})
    await page.goto(base + "/")
    await page.wait_for_selector(PLAY_PERSP)

    summary = page.locator(_THINKING_SUMMARY).first
    await summary.wait_for(state="attached", timeout=5000)
    # The server duration must survive the whitespace strip; the regression
    # collapsed it to the 1s Date.now() fallback on the all-at-once replay.
    await page.wait_for_function(
        """(sel) => {
          const el = document.querySelector(sel);
          return el && el.textContent.trim().startsWith('Thought for ');
        }""",
        arg=_THINKING_SUMMARY,
        timeout=5000,
    )
    text = (await summary.text_content() or "").strip()
    assert text == _EXPECTED_LABEL, f"expected {_EXPECTED_LABEL!r}, got {text!r}"
    assert text != _BUG_LABEL
