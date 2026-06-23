"""E2E: verbose AI-error toast shows a first-sentence summary plus a
Details affordance that opens the full text in a selectable modal.

Provider errors (quota, outage) are often many lines with URLs. The toast
shows only the leading sentence to stay glanceable; when more was dropped a
"Details" button opens the whole error in a modal the user can copy.

Driven by seeding the coordinator's replay buffer with a done-with-error
event -- no real LLM agent, no engine. The page rehydrates the panel via
the mount-time replay exactly as a client reconnecting after a turn would.

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
_TOAST = "#toast-stack .toast-danger"
_DETAILS_BTN = "#toast-stack .toast-danger .toast-action-btn"
_MODAL_BODY = "wa-dialog .error-detail-text"

_SUMMARY = "You exceeded your current quota."
_URL = "https://example.com/plan/billing-details"
# Trailing period after the URL exercises the link-boundary stop.
_REST = f"Please check your plan and billing details at {_URL}."
_FULL_ERROR = f"{_SUMMARY} {_REST}"

_PGN = (
    '[Event "?"]\n[Site "?"]\n[Date "????.??.??"]\n[Round "?"]\n'
    '[White "W"]\n[Black "B"]\n[Result "*"]\n\n'
    '1. e4 e5 2. Nf3 Nc6 *\n'
)


def _replay_events(game_id: str) -> list[dict]:
    return [
        {"kind": "ai_info", "game_id": game_id,
         "payload": {"done": True, "error": "quota_exceeded",
                     "error_detail": _FULL_ERROR, "seq": 1}},
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
async def test_verbose_error_toast_summary_and_details_modal(server, make_page):
    base = server
    game_id = _seed_view_mode(base)
    _seed_replay(base, _replay_events(game_id))

    _ctx, page = await make_page(viewport={"width": 1600, "height": 1000})
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    await page.goto(base + "/")
    await page.wait_for_selector(PLAY_PERSP)

    toast = page.locator(_TOAST)
    await toast.wait_for(state="attached", timeout=5000)
    toast_text = await toast.text_content() or ""
    # The summary shows; the dropped remainder does not.
    assert _SUMMARY in toast_text, toast_text
    assert "billing details" not in toast_text, toast_text

    # Details is an icon button (no text label) that opens the full text.
    details = page.locator(_DETAILS_BTN)
    await details.wait_for(state="attached", timeout=5000)
    assert await details.locator("wa-icon").count() == 1
    assert (await details.get_attribute("aria-label")) == "Error details"
    await details.click()

    body = page.locator(_MODAL_BODY)
    await body.wait_for(state="visible", timeout=5000)
    full = await body.text_content() or ""
    assert _SUMMARY in full and "billing details" in full, full

    # The URL in the error is a clickable new-tab link, not bare text.
    link = body.locator("a")
    assert await link.count() == 1
    assert (await link.get_attribute("href")) == _URL
    assert (await link.get_attribute("target")) == "_blank"
    assert "noopener" in (await link.get_attribute("rel") or "")
    assert errors == [], errors
