"""E2E regression: verifier verdict badge on the "Verifying line" row.

A delegate tool call's result carries a free-form ``verdict`` string that
opens with "holds" or "refuted" (enforced by the verifier prompt). The
panel classifies off that first word and badges the delegate row -- red
cross for refuted, green check for holds.

Two scoping traps this guards:
 - the badge class must land on the delegate row, not a nested sub-op row
   that happens to sit inside it (the CSS uses ``> head >``; the JS guard
   must match with ``:scope >`` or an unscoped querySelector mis-badges);
 - a nested sub-op whose output also carries a ``verdict`` key (a future
   tool, or a replayed engine result) must never be badged.

Driven by seeding the coordinator's replay buffer with the same event
dicts a real turn buffers -- no real LLM agent, no engine. The page
rehydrates the panel via the mount-time replay exactly as a client
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
_DELEGATE_ROW = ".play-ai-tool-call:has(> .play-ai-tool-head > .play-ai-tool-dot-delegate)"
_CHILD_ROW = ".play-ai-tool-children .play-ai-tool-call"
_REFUTED_CLASS = "play-ai-tool-call-refuted"
_HOLDS_CLASS = "play-ai-tool-call-holds"

_DELEGATE_TU = "tu_delegate"
_CHILD_TU = "tu_child"

_PGN = (
    '[Event "?"]\n[Site "?"]\n[Date "????.??.??"]\n[Round "?"]\n'
    '[White "W"]\n[Black "B"]\n[Result "*"]\n\n'
    '1. e4 e5 2. Nf3 Nc6 *\n'
)


# A delegate ("Verifying line") row with one nested engine sub-op. Both
# completes carry a verdict-shaped string: only the delegate row may be
# badged. seq drives the client replay-dedupe protocol.
def _replay_events(game_id: str) -> list[dict]:
    return [
        {"kind": "ai_tool_call", "game_id": game_id,
         "payload": {"name": "delegate", "tool_use_id": _DELEGATE_TU,
                     "input": {"move": "Ne5", "question": "does it hold?"},
                     "round": 0, "seq": 1}},
        {"kind": "ai_tool_call", "game_id": game_id,
         "payload": {"name": "top_moves", "tool_use_id": _CHILD_TU,
                     "parent_tool_use_id": _DELEGATE_TU,
                     "input": {"depth": 12}, "round": 0, "seq": 2}},
        # Child completes first; its output also opens with "holds" -- it
        # must NOT get badged (scoping guard).
        {"kind": "ai_tool_call_complete", "game_id": game_id,
         "payload": {"name": "top_moves", "tool_use_id": _CHILD_TU,
                     "output": {"verdict": "holds, engine line is fine"},
                     "seq": 3}},
        {"kind": "ai_tool_call_complete", "game_id": game_id,
         "payload": {"name": "delegate", "tool_use_id": _DELEGATE_TU,
                     "output": {"move_uci": "f3e5",
                                "verdict": "refuted: drops the knight to dxe5"},
                     "seq": 4}},
        {"kind": "ai_info", "game_id": game_id,
         "payload": {"done": True, "seq": 5}},
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
async def test_refuted_verdict_badges_only_the_delegate_row(server, make_page):
    base = server
    game_id = _seed_view_mode(base)
    _seed_replay(base, _replay_events(game_id))

    _ctx, page = await make_page(viewport={"width": 1600, "height": 1000})
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    await page.goto(base + "/")
    await page.wait_for_selector(PLAY_PERSP)

    delegate = page.locator(_DELEGATE_ROW)
    await delegate.wait_for(state="attached", timeout=5000)
    # The refuted verdict badges the delegate row...
    await page.wait_for_function(
        """(sel) => {
          const el = document.querySelector(sel);
          return el && el.classList.contains('%s');
        }""" % _REFUTED_CLASS,
        arg=_DELEGATE_ROW,
        timeout=5000,
    )
    cls = await delegate.get_attribute("class")
    assert _REFUTED_CLASS in cls, cls
    assert _HOLDS_CLASS not in cls, cls

    # ...and the nested sub-op row is never badged, despite its own
    # verdict-shaped output (the scoping guard).
    child = page.locator(_CHILD_ROW)
    await child.wait_for(state="attached", timeout=5000)
    child_cls = await child.get_attribute("class") or ""
    assert _REFUTED_CLASS not in child_cls, child_cls
    assert _HOLDS_CLASS not in child_cls, child_cls
    assert errors == [], errors
