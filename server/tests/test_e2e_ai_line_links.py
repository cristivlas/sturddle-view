"""E2E: AI prose line links (opening names, the recommended move).

ai_opening_links wraps opening names (exact case; a null-uci item is left
plain, whole); ai_recommendation then wraps the recommended move's mentions
without disturbing them. Move matching: the SAN
as a whole token (O-O never inside O-O-O), check suffix optional, a move
number only if it is the current one, and a pawn push only after that number.
Double-click plays the line.

Driven by seeding the coordinator's replay buffer while the server holds
ANALYZING (engine-only analysis on a fake engine) -- no real LLM.

Skipped if Playwright is missing.
"""
from __future__ import annotations

import httpx
import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import REGISTRY_FILE, e2e_env, make_searching_fake_uci, run_uvicorn_subprocess  # noqa: E402


PLAY_PERSP = "#play-perspective"
LINK_SEL = ".play-ai-prose .play-ai-line-link"
PLAYABLE_SEL = ".play-ai-prose .play-ai-line-link-playable"
PLAYING_SEL = ".play-ai-prose .play-ai-line-link-playing"


@pytest.fixture
def server(tmp_path):
    registry_path = tmp_path / REGISTRY_FILE
    seed = EngineRegistry(path=registry_path)
    engine_path = make_searching_fake_uci(tmp_path, "FakeEngine")
    e = seed.add(name="FakeEngine", path=engine_path)
    seed.select(e.id)
    env = e2e_env(tmp_path)
    with run_uvicorn_subprocess(env_overrides=env) as base:
        yield base


def _start_analysis(base) -> str:
    r = httpx.post(f"{base}/game/new", json={})
    r.raise_for_status()
    gid = r.json().get("game_id")
    httpx.post(f"{base}/game/pause").raise_for_status()
    httpx.post(f"{base}/game/analysis/start").raise_for_status()
    return gid


def _seed(base, gid, deltas, rec, opening_items=()) -> None:
    """One round of prose (`deltas`, streamed as separate chunks), its
    opening links, then the recommendation (stamped with the live FEN, as
    the server does) and done."""
    fen = httpx.get(f"{base}/_test/hve/state").json()["board_fen"]
    events = [
        {"kind": "ai_info", "game_id": gid, "payload": {"delta": d, "round": 0}}
        for d in deltas
    ]
    if opening_items:
        events.append({"kind": "ai_opening_links", "game_id": gid,
                       "payload": {"round": 0, "items": list(opening_items)}})
    events.append({"kind": "ai_recommendation", "game_id": gid,
                   "payload": {**rec, "fen": fen}})
    events.append({"kind": "ai_info", "game_id": gid, "payload": {"done": True}})
    for seq, ev in enumerate(events, start=1):
        ev["payload"]["seq"] = seq
    httpx.post(f"{base}/_test/ai/seed_replay", json={"events": events}).raise_for_status()


async def _link_texts(make_page, base, count: int) -> tuple:
    """Load the page and return it with the link texts, once `count` links
    have rendered (replay lands them all in one rehydrate)."""
    _ctx, page = await make_page(viewport={"width": 1600, "height": 1000})
    await page.goto(base + "/")
    await page.wait_for_selector(PLAY_PERSP)
    await page.locator(LINK_SEL).nth(count - 1).wait_for(state="attached")
    return page, await page.locator(LINK_SEL).all_text_contents()


@pytest.mark.asyncio
async def test_opening_and_pawn_push_links_and_play(server, make_page):
    base = server
    gid = _start_analysis(base)
    # The opening name spans two chunks; the pawn push links only after the
    # current move number (1. White), never bare or after another number.
    _seed(
        base, gid,
        deltas=[
            "The Queen's Pa",
            "wn Game starts 1.d4, and 1. d4 is fine. The pawn on d4 holds; "
            "3.d4 and 3. d4 stay plain.",
        ],
        rec={"uci": "d2d4", "san": "d4", "pv_uci": ["d2d4", "d7d5"]},
        opening_items=[{"surface": "Queen's Pawn Game", "uci": ["d2d4"]}],
    )
    expected = ["Queen's Pawn Game", "d4", "d4"]
    page, texts = await _link_texts(make_page, base, len(expected))
    assert texts == expected

    await page.locator(PLAYABLE_SEL).nth(1).dblclick()
    await page.wait_for_selector(PLAYING_SEL)


@pytest.mark.asyncio
async def test_opening_surface_matching(server, make_page):
    base = server
    gid = _start_analysis(base)
    # Exact case only, and a span the server marked plain (null uci) stays
    # whole even though a linked surface sits inside it.
    _seed(
        base, gid,
        deltas=[
            "The Exchange Variation is calm; an exchange variation in lowercase "
            "and the French Defense Exchange Variation stay plain.",
        ],
        rec={"uci": "d2d4", "san": "d4"},
        opening_items=[
            {"surface": "French Defense Exchange Variation", "uci": None},
            {"surface": "Exchange Variation", "uci": ["e2e4", "c7c6"]},
        ],
    )
    expected = ["Exchange Variation"]
    _page, texts = await _link_texts(make_page, base, len(expected))
    assert texts == expected


@pytest.mark.parametrize(("san", "uci", "prose", "expected"), [
    # Castling: never inside O-O-O; current number spaced or not.
    ("O-O", "e1g1",
     "Not O-O-O; O-O now, as 1.O-O or 1. O-O; 7.O-O and 7. O-O stay plain.",
     ["O-O", "O-O", "O-O"]),
    # Piece move: check suffix optional; a foreign number stays plain.
    ("Nf3", "g1f3",
     "Nf3+ or Nf3 both; 9. Nf3 and 9.Nf3 do not; xNf3 neither.",
     ["Nf3+", "Nf3"]),
])
@pytest.mark.asyncio
async def test_recommended_move_matching(server, make_page, san, uci, prose, expected):
    base = server
    gid = _start_analysis(base)
    _seed(base, gid, deltas=[prose], rec={"uci": uci, "san": san, "pv_uci": [uci]})
    _page, texts = await _link_texts(make_page, base, len(expected))
    assert texts == expected
