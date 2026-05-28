"""E2E: AI `analyze` board preview overlay.

When the AI agent invokes `analyze` with a hypothetical FEN, the live
board mirrors that FEN so the user can follow the AI's reasoning;
restored on tool_call_complete or any session-end event.

Driven via /_test/ai/publish_event -- no real LLM agent loop. The
client-side preview wiring reacts to the bus event identically.

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
_BOARD_SVG_SELECTOR = ".game-view-board .board svg.cm-chessboard"

# Hypothetical FEN visibly different from the live position so the
# test can distinguish them on the board SVG.
_HYPO_FEN = "rnb1k1nr/p2p1ppp/3B4/1pbN1N1P/4P1P1/3P1Q2/PqP1K3/6R1 b kq - 0 19"

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


def _seed_view_mode(base) -> tuple[str, str]:
    """Returns (game_id, live_fen) after seeding view mode."""
    resp = httpx.post(f"{base}/game/import", json={"text": _PGN, "format": "pgn"})
    resp.raise_for_status()
    body = resp.json()
    # /game/state reflects the current cursor's FEN.
    state = httpx.get(f"{base}/_test/hve/state").json()
    return body["game_id"], state["board_fen"]


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


def _fen_to_pieces(fen: str) -> list[str]:
    """Convert a FEN's piece-placement field to a sorted list of
    'wq@d1'-style tokens matching cm-chessboard's data-piece /
    data-square SVG attributes (lowercase color+piece)."""
    placement = fen.split()[0]
    out: list[str] = []
    files = "abcdefgh"
    rank = 8
    for row in placement.split("/"):
        file_idx = 0
        for ch in row:
            if ch.isdigit():
                file_idx += int(ch)
                continue
            color = "w" if ch.isupper() else "b"
            piece = ch.lower()
            out.append(f"{color}{piece}@{files[file_idx]}{rank}")
            file_idx += 1
        rank -= 1
    return sorted(out)


async def _wait_board_matches_fen(page, fen: str, timeout_ms=3000):
    expected = _fen_to_pieces(fen)
    await page.wait_for_function(
        f"""(expected) => {{
          const svg = document.querySelector('{_BOARD_SVG_SELECTOR}');
          if (!svg) return false;
          const actual = [...svg.querySelectorAll('[data-piece]')]
            .map(p => p.getAttribute('data-piece') + '@' + p.getAttribute('data-square'))
            .sort();
          return JSON.stringify(actual) === JSON.stringify(expected);
        }}""",
        arg=expected,
        timeout=timeout_ms,
    )


@pytest.mark.asyncio
async def test_analyze_tool_call_previews_hypothetical_fen(server, make_page):
    base = server
    game_id, live_fen = _seed_view_mode(base)
    _ctx, page, _errors = await _new_page(make_page)
    await _goto_view_mode(page, base)
    await _wait_board_matches_fen(page, live_fen, timeout_ms=5000)

    _publish(base, "ai_tool_call", game_id=game_id, payload={
        "round": 0,
        "name": "analyze",
        "input": {"fen": _HYPO_FEN, "depth": 12},
        "tool_use_id": "test-1",
    })
    await _wait_board_matches_fen(page, _HYPO_FEN)


@pytest.mark.asyncio
async def test_analyze_complete_restores_live_fen(server, make_page):
    base = server
    game_id, live_fen = _seed_view_mode(base)
    _ctx, page, _errors = await _new_page(make_page)
    await _goto_view_mode(page, base)
    await _wait_board_matches_fen(page, live_fen)

    _publish(base, "ai_tool_call", game_id=game_id, payload={
        "round": 0,
        "name": "analyze",
        "input": {"fen": _HYPO_FEN, "depth": 12},
        "tool_use_id": "test-2",
    })
    await _wait_board_matches_fen(page, _HYPO_FEN)

    _publish(base, "ai_tool_call_complete", game_id=game_id, payload={
        "round": 0,
        "name": "analyze",
        "tool_use_id": "test-2",
    })
    await _wait_board_matches_fen(page, live_fen)


@pytest.mark.asyncio
async def test_ai_session_done_restores_live_fen(server, make_page):
    # Defensive restore: ai_info with done=true snaps back even when
    # tool_call_complete was never emitted (cancel / round cap / crash).
    base = server
    game_id, live_fen = _seed_view_mode(base)
    _ctx, page, _errors = await _new_page(make_page)
    await _goto_view_mode(page, base)
    await _wait_board_matches_fen(page, live_fen)

    _publish(base, "ai_tool_call", game_id=game_id, payload={
        "round": 0,
        "name": "analyze",
        "input": {"fen": _HYPO_FEN, "depth": 12},
        "tool_use_id": "test-3",
    })
    await _wait_board_matches_fen(page, _HYPO_FEN)

    _publish(base, "ai_info", game_id=game_id, payload={"done": True, "cancelled": True})
    await _wait_board_matches_fen(page, live_fen)
