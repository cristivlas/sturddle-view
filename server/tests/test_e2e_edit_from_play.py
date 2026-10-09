"""E2E: edit from play and leaving a game in progress.

Edit from play runs on a live clone of the game (same game_id): exits
return to the live board, a changed position asks before leaving the
game, and every path that leaves a game in progress asks one shared,
non-destructive confirm and saves the game to Recents with its notes.
"""
from __future__ import annotations

import httpx
import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import (  # noqa: E402
    PIECE_ON,
    REGISTRY_FILE,
    PageObserver,
    assert_no_page_errors,
    drag_piece_one_rank_up,
    e2e_env,
    make_searching_fake_uci,
    posted,
    run_uvicorn_subprocess,
    wait_perspective_ready,
    watch_page_errors,
)

VIEWPORT = {"width": 1600, "height": 1000}

VIEW_NEW_GAME_BTN = "#view-new-game"
PLAY_EDIT_BTN = "#edit-pos"
VIEW_IMPORT_BTN = "#view-import"
EDIT_CONFIRM_BTN = "#edit-confirm"
EDIT_CANCEL_BTN = "#edit-cancel"
EDIT_CASTLE_BTN = "#edit-castle-btn"
WHITE_SHORT_CASTLE = "#edit-castle-cb-wk"
PLAY_RIBBON = "#board-controls"
EDIT_RIBBON = "#edit-controls"
VIEW_RIBBON = "#view-controls"
MOVE_CELL = ".move-cell.clickable"
PARENT_LINK = ".xgame-toast .xgame-link"
RECENTS_SELECT = "wa-dialog[open] wa-select"
ROW = 'wa-option[data-hash="{}"]'
CONFIRM_MESSAGE = "wa-dialog[open] .confirm-message"
CONFIRM_BUTTON = 'wa-dialog[open] wa-button:has-text("{}")'

CONFIRM_NEW_GAME = "Start a new game? The current game will be saved to Recents."
CONFIRM_IMPORT = "Import and leave the current game? It will be saved to Recents."
CONFIRM_EDIT_LEAVE = (
    "Apply the new position and leave the current game? It will be saved to Recents."
)
CONFIRM_OPEN_PARENT = (
    "Open the parent game and leave the current one? It will be saved to Recents."
)
NEW_GAME_LABEL = "New game"
IMPORT_LABEL = "Import"
APPLY_LABEL = "Apply"
KEEP_EDITING_LABEL = "Keep editing"
OPEN_LABEL = "Open"

NOTE = "A note on the live game."
OTHER_PGN = '[White "A"]\n[Black "B"]\n\n1. d4 Nf6 *\n'
PARENT_PGN = '[White "P"]\n[Black "Q"]\n\n1. d4 d5 *\n'
FORK_PLY = 2
PARENT_PLIES = 2
LIVE_PLIES = 2
NEW_GAME_PATH = "/game/new"
IMPORT_PATH = "/game/import"
EDIT_COMMIT_PATH = "/game/edit/commit"

RIBBON_SHOWN_JS = """(sel) => {
  const el = document.querySelector(sel);
  return !!el && getComputedStyle(el).display !== 'none';
}"""


@pytest.fixture
def server(tmp_path):
    seed = EngineRegistry(path=tmp_path / REGISTRY_FILE)
    seed.select(seed.add(
        name="FakeEngine",
        path=make_searching_fake_uci(tmp_path, "FakeEngine", bestmove="e7e5", pv="e7e5"),
    ).id)
    with run_uvicorn_subprocess(env_overrides=e2e_env(tmp_path)) as base:
        yield base


def _state(base: str) -> dict:
    return httpx.get(f"{base}/_test/hve/state").json()


def _import(base: str, text: str) -> dict:
    r = httpx.post(f"{base}/game/import", json={"format": "pgn", "text": text})
    r.raise_for_status()
    return r.json()


def _row_text(base: str, game_id: str) -> str | None:
    r = httpx.get(f"{base}/game/recent-imports/by-id/{game_id}")
    return r.json()["text"] if r.status_code == 200 else None


def _plies(n: int):
    def pred(p):
        return len(p.get("moves_san") or []) == n and not p.get("view")
    pred.__name__ = f"live_with_{n}_plies"
    return pred


def _viewing_at(cursor: int):
    def pred(p):
        v = p.get("view") or {}
        return bool(v) and v.get("cursor") == cursor and not p.get("editing")
    pred.__name__ = f"viewing_at_{cursor}"
    return pred


def _viewing_game(cursor: int, total: int):
    def pred(p):
        v = p.get("view") or {}
        return v.get("cursor") == cursor and v.get("total_plies") == total
    pred.__name__ = f"viewing_{total}_plies_at_{cursor}"
    return pred


def _editing(p):
    return p.get("editing") is True


_editing.__name__ = "editing"


def _live(p):
    return not p.get("view") and not p.get("editing")


_live.__name__ = "live"


async def _open(make_page, base):
    _ctx, page = await make_page(base_url=base, viewport=VIEWPORT)
    errors = watch_page_errors(page)
    obs = PageObserver(page)
    await page.goto("/")
    await wait_perspective_ready(page)
    return page, errors, obs


async def _start_live_game(page, obs, base) -> str:
    """New game (human White), 1.e4 e5 played, human to move."""
    httpx.post(f"{base}/game/new", json={"human_white": True}).raise_for_status()
    httpx.post(f"{base}/game/move", json={"uci": "e2e4"}).raise_for_status()
    await obs.wait_board_update(_plies(LIVE_PLIES))
    return _state(base)["game_id"]


async def _scrub_to_first_ply(page, obs):
    await page.locator(MOVE_CELL).first.click()
    await obs.wait_board_update(_viewing_at(1))


def _note_on_clone(base: str) -> None:
    fen = httpx.post(f"{base}/game/edit/start", json={}).json()["fen"]
    httpx.post(f"{base}/game/edit/commit", json={
        "fen": fen, "apply_comment": True, "comment_text": NOTE,
    }).raise_for_status()


async def _confirm(page, expected: str, button: str, *, posts: str | None = None) -> None:
    """Answer the confirm; with ``posts``, also await that POST's response
    (it returns after a left game's Recents save)."""
    await page.wait_for_selector(CONFIRM_MESSAGE)
    assert await page.locator(CONFIRM_MESSAGE).text_content() == expected
    if posts is None:
        await page.click(CONFIRM_BUTTON.format(button))
    else:
        async with page.expect_response(posted(posts)):
            await page.click(CONFIRM_BUTTON.format(button))
    await page.wait_for_selector(CONFIRM_MESSAGE, state="detached")


async def _change_castling(page):
    await page.click(EDIT_CASTLE_BTN)
    await page.click(WHITE_SHORT_CASTLE)


@pytest.mark.asyncio
async def test_new_game_from_clone_asks_and_saves_notes(server, make_page):
    base = server
    page, errors, obs = await _open(make_page, base)
    live_id = await _start_live_game(page, obs, base)
    await _scrub_to_first_ply(page, obs)
    _note_on_clone(base)

    await page.click(VIEW_NEW_GAME_BTN)
    await _confirm(page, CONFIRM_NEW_GAME, NEW_GAME_LABEL, posts=NEW_GAME_PATH)
    await page.wait_for_function(RIBBON_SHOWN_JS, arg=PLAY_RIBBON)

    assert _state(base)["game_id"] != live_id
    assert NOTE in _row_text(base, live_id)
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_edit_cancel_from_play_lands_on_live_board(server, make_page):
    base = server
    page, errors, obs = await _open(make_page, base)
    live_id = await _start_live_game(page, obs, base)

    await page.click(PLAY_EDIT_BTN)
    await obs.wait_board_update(_editing)
    await page.click(EDIT_CANCEL_BTN)
    await obs.wait_board_update(_live)
    await page.wait_for_function(RIBBON_SHOWN_JS, arg=PLAY_RIBBON)

    state = _state(base)
    assert state["viewing"] is False
    assert state["game_id"] == live_id
    assert state["n_plies"] == LIVE_PLIES
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_changed_commit_asks_then_keep_editing_stays(server, make_page):
    base = server
    page, errors, obs = await _open(make_page, base)
    live_id = await _start_live_game(page, obs, base)

    await page.click(PLAY_EDIT_BTN)
    await obs.wait_board_update(_editing)
    await _change_castling(page)
    await page.click(EDIT_CONFIRM_BTN)
    await _confirm(page, CONFIRM_EDIT_LEAVE, KEEP_EDITING_LABEL)
    assert await page.evaluate(RIBBON_SHOWN_JS, EDIT_RIBBON)

    # The game-id filter is back: cancel lands on the live board.
    await page.click(EDIT_CANCEL_BTN)
    await obs.wait_board_update(_live)
    await page.wait_for_function(RIBBON_SHOWN_JS, arg=PLAY_RIBBON)
    assert _state(base)["game_id"] == live_id
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_changed_commit_after_reload_still_asks(server, make_page):
    base = server
    page, errors, obs = await _open(make_page, base)
    live_id = await _start_live_game(page, obs, base)

    await page.click(PLAY_EDIT_BTN)
    await obs.wait_board_update(_editing)
    await page.reload()
    await wait_perspective_ready(page)
    await page.wait_for_function(RIBBON_SHOWN_JS, arg=EDIT_RIBBON)
    await _change_castling(page)
    await page.click(EDIT_CONFIRM_BTN)
    await _confirm(page, CONFIRM_EDIT_LEAVE, APPLY_LABEL, posts=EDIT_COMMIT_PATH)
    await page.wait_for_function(RIBBON_SHOWN_JS, arg=VIEW_RIBBON)

    assert _state(base)["game_id"] != live_id
    assert _row_text(base, live_id) is not None
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_import_from_clone_asks_once(server, make_page):
    base = server
    other = _import(base, OTHER_PGN)
    page, errors, obs = await _open(make_page, base)
    live_id = await _start_live_game(page, obs, base)
    await _scrub_to_first_ply(page, obs)

    await page.click(VIEW_IMPORT_BTN)
    await _confirm(page, CONFIRM_IMPORT, IMPORT_LABEL)
    await page.click(RECENTS_SELECT)
    # A second (replace-viewed) confirm would block the import.
    async with page.expect_response(posted(IMPORT_PATH)):
        await page.locator(ROW.format(other["hash"])).click()
    assert _state(base)["game_id"] == other["game_id"]
    assert await page.locator(CONFIRM_MESSAGE).count() == 0
    assert _row_text(base, live_id) is not None
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_parent_toast_on_clone_asks_and_opens_parent(server, make_page):
    base = server
    parent = _import(base, PARENT_PGN)
    httpx.post(f"{base}/game/view/goto", json={"ply": FORK_PLY}).raise_for_status()
    live_id = httpx.post(
        f"{base}/game/view/play-from-here", json={},
    ).json()["game_id"]
    page, errors, obs = await _open(make_page, base)
    httpx.post(f"{base}/game/move", json={"uci": "e2e4"}).raise_for_status()
    await obs.wait_board_update(_plies(FORK_PLY + 2))
    # Save PGN: the exported row carries the fork link the toast reads.
    httpx.get(f"{base}/game/pgn").raise_for_status()

    await page.locator(MOVE_CELL).nth(FORK_PLY - 1).click()
    await obs.wait_board_update(_viewing_at(FORK_PLY))
    await page.click(PARENT_LINK)
    await _confirm(page, CONFIRM_OPEN_PARENT, OPEN_LABEL, posts=IMPORT_PATH)
    await obs.wait_board_update(_viewing_game(FORK_PLY, PARENT_PLIES))

    assert _state(base)["game_id"] == parent["game_id"]
    assert _row_text(base, live_id) is not None
    assert_no_page_errors(errors)


DRAG_FROM = "a2"
DRAG_TO = "a3"


@pytest.mark.asyncio
async def test_pieces_move_in_edit_from_play(server, make_page):
    """Edit from play installs the position editor in the same board update
    that turns view mode on: the board must stay draggable."""
    base = server
    page, errors, obs = await _open(make_page, base)
    await _start_live_game(page, obs, base)

    await page.click(PLAY_EDIT_BTN)
    await obs.wait_board_update(_editing)
    await drag_piece_one_rank_up(page, DRAG_FROM)
    await page.locator(PIECE_ON.format(DRAG_TO)).wait_for(state="attached")
    await page.click(EDIT_CONFIRM_BTN)
    await _confirm(page, CONFIRM_EDIT_LEAVE, KEEP_EDITING_LABEL)
    assert_no_page_errors(errors)


PAUSE_BTN = "#pause"
EDIT_ANNOTATE_BTN = "#edit-annotate"
DIALOG_OK = 'wa-dialog[open] wa-button:has-text("OK")'
ANNOTATION_TEXTAREA = "wa-dialog[open] wa-textarea"
MOVE_PATH = "/game/move"


@pytest.mark.asyncio
async def test_board_takes_moves_after_note_from_play(server, make_page):
    """Edit from play, note, confirm (back to play, paused), resume: the
    board takes a move again."""
    base = server
    page, errors, obs = await _open(make_page, base)
    await _start_live_game(page, obs, base)

    await page.click(PLAY_EDIT_BTN)
    await obs.wait_board_update(_editing)
    await page.click(EDIT_ANNOTATE_BTN)
    await page.locator(ANNOTATION_TEXTAREA).locator("textarea").fill(NOTE)
    await page.click(DIALOG_OK)
    await page.click(EDIT_CONFIRM_BTN)
    await obs.wait_board_update(_live)
    await page.click(PAUSE_BTN)
    async with page.expect_response(posted(MOVE_PATH)):
        await drag_piece_one_rank_up(page, DRAG_FROM)
    assert_no_page_errors(errors)
