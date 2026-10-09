"""E2E: the pencil after a finished AI turn opens the annotation modal
prefilled with the AI prose (pencil + comment button in one go). From play
mode it flips to view first, skipping the discard-game confirm.

- No comment at the ply: the modal holds the latest visible prose -- rounds
  folded into a revision or hidden as a tool leak are skipped -- and the
  "stop analysis?" confirm is skipped.
- A comment at the ply: comment, blank line, prose; prose the comment
  already holds is not added twice.
- OK then the edit confirm saves the prose as the ply's comment.
- Cancel leaves edit mode open with nothing staged; the prose is single
  use, so the comment button then preloads the plain comment.
- No visible prose: the normal confirm flows (stop analysis in view,
  discard game in play).

Driven by seeding the coordinator's replay buffer while the server holds
ANALYZING (engine-only analysis on a fake engine) with ai_enabled on for
the client -- no real LLM.

Skipped if Playwright is missing.
"""
from __future__ import annotations

import httpx
import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import (  # noqa: E402
    REGISTRY_FILE,
    assert_no_page_errors,
    e2e_env,
    make_searching_fake_uci,
    run_uvicorn_subprocess,
    wait_perspective_ready,
    watch_page_errors,
)


VIEW_EDIT_BTN = "#view-edit"
PLAY_EDIT_BTN = "#edit-pos"
EDIT_ANNOTATE_BTN = "#edit-annotate"
EDIT_CONFIRM_BTN = "#edit-confirm"
EDIT_CANCEL_BTN = "#edit-cancel"
EDIT_RIBBON = "#edit-controls"
VIEW_RIBBON = "#view-controls"
PLAY_RIBBON = "#board-controls"
OPEN_DIALOG = "wa-dialog[open]"
ANNOTATION_TEXTAREA = f"{OPEN_DIALOG} wa-textarea"
DIALOG_BUTTON = f"{OPEN_DIALOG} wa-button"
OK_LABEL = "OK"
CONFIRM_MESSAGE = f"{OPEN_DIALOG} .confirm-message"
STOP_ANALYSIS_CONFIRM = "Stop analysis and edit the position?"
ANALYSIS_DONE = "Analysis Done"
PARAGRAPH_BREAK = "\n\n"
# The PGN's plies; the comment sits on the last one.
PGN_PLIES = 4
# Fake engine's canned search, legal at that last ply (python-chess logs an
# error parsing an illegal pv).
ENGINE_BESTMOVE = "f1b5"
ENGINE_PV = "f1b5 a7a6"

FOLDED_PROSE = "Early thought, later corrected."
VERDICT = "Nc6 defends e5 and develops; equal."
LEAKED_PROSE = "Calling top_moves next."
OLD_COMMENT = "Old note."

# Server in ANALYZING + the AI turn's events replayed + the AI-done latch.
READY_JS = """(done) => document.body.classList.contains('xgame-nav-locked')
  && document.querySelector('.play-ai-status-text')?.textContent === done"""
RIBBON_SHOWN_JS = """(sel) => {
  const el = document.querySelector(sel);
  return !!el && getComputedStyle(el).display !== 'none';
}"""


def _pgn(comment: str | None) -> str:
    note = f" {{{comment}}}" if comment else ""
    return (
        '[Event "?"]\n[White "W"]\n[Black "B"]\n[Result "*"]\n\n'
        f"1. e4 e5 2. Nf3 Nc6{note} *\n"
    )


@pytest.fixture
def server(tmp_path):
    registry_path = tmp_path / REGISTRY_FILE
    seed = EngineRegistry(path=registry_path)
    engine_path = make_searching_fake_uci(
        tmp_path, "FakeEngine", bestmove=ENGINE_BESTMOVE, pv=ENGINE_PV,
    )
    e = seed.add(name="FakeEngine", path=engine_path)
    seed.select(e.id)
    with run_uvicorn_subprocess(env_overrides=e2e_env(tmp_path)) as base:
        yield base


def _round_events(gid: str, rounds) -> list[dict]:
    """`rounds`: (prose, note) per round; note is None, "fold" (flagged
    claim -> revision) or "hide" (tool leak)."""
    events = []
    for i, (prose, note) in enumerate(rounds):
        events.append({"kind": "ai_info", "game_id": gid,
                       "payload": {"delta": prose, "round": i}})
        if note == "fold":
            events.append({"kind": "ai_position_note", "game_id": gid,
                           "payload": {"round": i, "surfaces": [prose.split(",")[0]]}})
        elif note == "hide":
            events.append({"kind": "ai_position_note", "game_id": gid,
                           "payload": {"round": i, "surfaces": [], "hide_prose": True}})
    events.append({"kind": "ai_info", "game_id": gid, "payload": {"done": True}})
    for seq, ev in enumerate(events, start=1):
        ev["payload"]["seq"] = seq
    return events


def _import_at_last_ply(base, comment) -> str:
    r = httpx.post(f"{base}/game/import", json={
        "format": "pgn", "text": _pgn(comment), "land_at_ply": PGN_PLIES,
    })
    r.raise_for_status()
    return r.json()["game_id"]


async def _view_with_finished_ai(make_page, base, *, comment, rounds):
    """View the PGN's last ply under a finished AI turn; return the page
    and its error log."""
    gid = _import_at_last_ply(base, comment)
    return await _load_with_finished_ai(make_page, base, gid, rounds)


async def _play_with_finished_ai(make_page, base, *, rounds):
    """Live play game seeded from the PGN (moves played, human to move),
    paused under a finished AI turn; return the page and its error log."""
    _import_at_last_ply(base, None)
    r = httpx.post(f"{base}/game/view/play-from-here", json={})
    r.raise_for_status()
    gid = r.json()["game_id"]
    httpx.post(f"{base}/game/pause").raise_for_status()
    return await _load_with_finished_ai(make_page, base, gid, rounds)


async def _load_with_finished_ai(make_page, base, gid, rounds):
    httpx.post(f"{base}/game/analysis/start").raise_for_status()
    # After the start: the client reads ai_enabled; the server already
    # chose the engine-only path, so no real AI turn is kicked.
    httpx.put(f"{base}/settings", json={"ai_enabled": True}).raise_for_status()
    httpx.post(f"{base}/_test/ai/seed_replay",
               json={"events": _round_events(gid, rounds)}).raise_for_status()
    _ctx, page = await make_page(viewport={"width": 1600, "height": 1000})
    errors = watch_page_errors(page)
    await page.goto(base + "/")
    await wait_perspective_ready(page)
    await page.wait_for_function(READY_JS, arg=ANALYSIS_DONE)
    return page, errors


async def _annotation_text(page) -> str:
    await page.wait_for_selector(ANNOTATION_TEXTAREA)
    return await page.locator(ANNOTATION_TEXTAREA).evaluate("(ta) => ta.value")


async def _close_dialog(page) -> None:
    await page.keyboard.press("Escape")
    await page.wait_for_selector(OPEN_DIALOG, state="detached")


async def _confirm_message(page) -> str:
    await page.wait_for_selector(CONFIRM_MESSAGE)
    return await page.locator(CONFIRM_MESSAGE).text_content()


@pytest.mark.asyncio
async def test_pencil_prefills_latest_visible_prose(server, make_page):
    page, errors = await _view_with_finished_ai(
        make_page, server, comment=None,
        rounds=[(FOLDED_PROSE, "fold"), (VERDICT, None), (LEAKED_PROSE, "hide")],
    )
    await page.click(VIEW_EDIT_BTN)
    assert await _annotation_text(page) == VERDICT
    assert await page.locator(CONFIRM_MESSAGE).count() == 0
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_pencil_appends_prose_to_existing_comment(server, make_page):
    page, errors = await _view_with_finished_ai(
        make_page, server, comment=OLD_COMMENT, rounds=[(VERDICT, None)],
    )
    await page.click(VIEW_EDIT_BTN)
    assert await _annotation_text(page) == OLD_COMMENT + PARAGRAPH_BREAK + VERDICT
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_pencil_skips_prose_the_comment_already_holds(server, make_page):
    held = f"{OLD_COMMENT} {VERDICT}"
    page, errors = await _view_with_finished_ai(
        make_page, server, comment=held, rounds=[(VERDICT, None)],
    )
    await page.click(VIEW_EDIT_BTN)
    assert await _annotation_text(page) == held
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_cancel_keeps_edit_mode_and_prose_is_single_use(server, make_page):
    page, errors = await _view_with_finished_ai(
        make_page, server, comment=OLD_COMMENT, rounds=[(VERDICT, None)],
    )
    await page.click(VIEW_EDIT_BTN)
    await _annotation_text(page)
    await _close_dialog(page)
    assert await page.evaluate(RIBBON_SHOWN_JS, EDIT_RIBBON)
    # Nothing staged and no leftover prose: the plain comment preloads.
    await page.click(EDIT_ANNOTATE_BTN)
    assert await _annotation_text(page) == OLD_COMMENT
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_ok_then_confirm_saves_prose_as_comment(server, make_page):
    page, errors = await _view_with_finished_ai(
        make_page, server, comment=None, rounds=[(VERDICT, None)],
    )
    await page.click(VIEW_EDIT_BTN)
    await _annotation_text(page)
    await page.locator(DIALOG_BUTTON, has_text=OK_LABEL).click()
    await page.wait_for_selector(OPEN_DIALOG, state="detached")
    await page.click(EDIT_CONFIRM_BTN)
    await page.wait_for_function(RIBBON_SHOWN_JS, arg=VIEW_RIBBON)
    # Saved server-side: a plain edit entry (analysis is over) preloads it.
    await page.click(VIEW_EDIT_BTN)
    await page.wait_for_function(RIBBON_SHOWN_JS, arg=EDIT_RIBBON)
    await page.click(EDIT_ANNOTATE_BTN)
    assert await _annotation_text(page) == VERDICT
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_no_visible_prose_keeps_confirm_flow(server, make_page):
    page, errors = await _view_with_finished_ai(
        make_page, server, comment=None, rounds=[(LEAKED_PROSE, "hide")],
    )
    await page.click(VIEW_EDIT_BTN)
    assert await _confirm_message(page) == STOP_ANALYSIS_CONFIRM
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_play_pencil_carries_prose_without_confirm(server, make_page):
    page, errors = await _play_with_finished_ai(make_page, server, rounds=[(VERDICT, None)])
    await page.click(PLAY_EDIT_BTN)
    assert await _annotation_text(page) == VERDICT
    assert await page.locator(CONFIRM_MESSAGE).count() == 0
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_play_pencil_ok_then_confirm_returns_to_play_paused(server, make_page):
    page, errors = await _play_with_finished_ai(make_page, server, rounds=[(VERDICT, None)])
    await page.click(PLAY_EDIT_BTN)
    assert await _annotation_text(page) == VERDICT
    await page.locator(DIALOG_BUTTON, has_text=OK_LABEL).click()
    await page.wait_for_selector(OPEN_DIALOG, state="detached")
    await page.click(EDIT_CONFIRM_BTN)
    # The clone sat on the last ply: the commit returns to the live game.
    await page.wait_for_function(RIBBON_SHOWN_JS, arg=PLAY_RIBBON)
    state = httpx.get(f"{server}/_test/hve/state").json()
    assert state["viewing"] is False
    assert state["paused"] is True
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_play_pencil_cancel_returns_to_play(server, make_page):
    page, errors = await _play_with_finished_ai(make_page, server, rounds=[(VERDICT, None)])
    await page.click(PLAY_EDIT_BTN)
    await _annotation_text(page)
    await _close_dialog(page)
    await page.click(EDIT_CANCEL_BTN)
    # The edit ran on a live clone at its last ply: cancel returns to play.
    await page.wait_for_function(RIBBON_SHOWN_JS, arg=PLAY_RIBBON)
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_play_pencil_without_prose_asks_only_to_stop_analysis(server, make_page):
    # Edit from play discards nothing: no discard confirm, only analysis's.
    page, errors = await _play_with_finished_ai(
        make_page, server, rounds=[(LEAKED_PROSE, "hide")],
    )
    await page.click(PLAY_EDIT_BTN)
    assert await _confirm_message(page) == STOP_ANALYSIS_CONFIRM
    assert_no_page_errors(errors)
