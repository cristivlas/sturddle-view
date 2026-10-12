"""E2E: the Settings Recents tab and the Import dropdown's gear deep link.

A recents row with forks shows a gear instead of the trash; the gear closes
the Import dialog (asking first if the user typed text) and opens Settings
on the Recents tab with that row selected and expanded. The tab shows the
fork tree, cascades a parent's delete over its forks after a confirm,
refuses a cascade over the game in view, opens a row's game in Play, and
refetches when a game ends while it is open.

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
    XGAME_BY_ID_PATH,
    PageObserver,
    assert_no_page_errors,
    e2e_env,
    make_searching_fake_uci,
    run_uvicorn_subprocess,
    wait_perspective_ready,
    watch_page_errors,
)

VIEWPORT = {"width": 1400, "height": 900}

OPEN_DIALOG = "wa-dialog[open]"
VIEW_OPEN_BTN = "#view-import"
SETTINGS_BTN = "#settings-btn"
RECENTS_SELECT = f"{OPEN_DIALOG} wa-select"
PGN_TEXTAREA = f"{OPEN_DIALOG} wa-textarea textarea"
OPTION = 'wa-option[data-hash="{}"]'
GEAR = ".recent-manage"
TRASH = ".recent-del"
RECENTS_TAB = f'{OPEN_DIALOG} wa-tab[panel="recents"]'
ROW = f'{OPEN_DIALOG} .recents-row[data-hash="{{}}"]'
SELECTED_ROW = f"{OPEN_DIALOG} .recents-row.selected"
ROW_TWISTY = ".recents-twisty button"
ROW_LABEL = ".recents-label"
ROW_FORK = ".recents-fork"
ROW_TAG = ".recents-tag"
ROW_OPEN = '.recents-btn[aria-label="Open"]'
ROW_DELETE = '.recents-btn[aria-label="Delete"]'
CONFIRM_MESSAGE = f"{OPEN_DIALOG} .confirm-message"
CONFIRM_BUTTON = OPEN_DIALOG + ' wa-button:has-text("{}")'
HASH_ATTR = "data-hash"
LEVEL_ATTR = "aria-level"

SEED_FORK_PATH = "/_test/recents/seed_fork"
IMPORT_PATH = "/game/import"
STATUS_PATH = "/game/status"
FORK_PLY = 2
FORK_LABEL = "move 2."
VIEWING_TAG = "viewing"
PLAYING_TAG = "playing"
EXPORT_PGN_PATH = "/game/pgn"
OPEN_SETTINGS_LABEL = "Open Settings"
DELETE_LABEL = "Delete"
REPLACE_LABEL = "Replace"
TYPED_TEXT = "1. e4"
DELETE_TWO_FORKS = "Delete with 2 forks?"
CHILD_LEVEL = "2"

# Each pair: distinct parent/child texts so canonical hashes differ.
PAIRS = [
    ("1. e4 e5 *", "1. e4 e5 2. Nf3 *"),
    ("1. d4 d5 *", "1. d4 d5 2. c4 *"),
    ("1. c4 c5 *", "1. c4 c5 2. Nc3 *"),
]
GRANDCHILD_TEXT = "1. e4 e5 2. Nf3 Nc6 *"
VIEWED_PGN = '[White "V"]\n[Black "W"]\n\n1. g3 g6 *\n'


@pytest.fixture
def server(tmp_path):
    # The game endpoints need a configured engine; viewing never runs it.
    seed = EngineRegistry(path=tmp_path / REGISTRY_FILE)
    seed.select(seed.add(name="MyEngine", path="/nonexistent/engine").id)
    with run_uvicorn_subprocess(env_overrides=e2e_env(tmp_path)) as base:
        yield base


def _hash_of(base, game_id) -> str:
    return httpx.get(f"{base}{XGAME_BY_ID_PATH}{game_id}").json()["hash"]


def _seed_fork(base, parent_id, parent_text, child_id, child_text):
    httpx.post(f"{base}{SEED_FORK_PATH}", json={
        "parent": {"game_id": parent_id, "text": parent_text, "summary": {"white": parent_id}},
        "child": {
            "game_id": child_id, "text": child_text,
            "summary": {"white": child_id}, "fork_ply": FORK_PLY,
        },
    }).raise_for_status()


def _seed_pair(base, i) -> str:
    """Seed parent i with one fork; return the parent's hash."""
    parent_text, child_text = PAIRS[i]
    _seed_fork(base, f"parent-{i}", parent_text, f"child-{i}", child_text)
    return _hash_of(base, f"parent-{i}")


def _seed_chain(base) -> tuple[str, str, str]:
    """parent-0 -> child-0 -> grandchild; returns their hashes."""
    _seed_pair(base, 0)
    _seed_fork(base, "child-0", PAIRS[0][1], "grandchild", GRANDCHILD_TEXT)
    return _hash_of(base, "parent-0"), _hash_of(base, "child-0"), _hash_of(base, "grandchild")


def _import(base, text) -> str:
    r = httpx.post(f"{base}{IMPORT_PATH}", json={"format": "pgn", "text": text})
    r.raise_for_status()
    return r.json()["hash"]


async def _open_page(base, make_page):
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await page.goto(base + "/")
    await wait_perspective_ready(page)
    return page, errors


async def _open_recents_tab(page):
    await page.click(SETTINGS_BTN)
    await page.click(RECENTS_TAB)


async def _expand(page, row_hash):
    await page.locator(ROW.format(row_hash)).locator(ROW_TWISTY).click()


@pytest.mark.asyncio
async def test_forked_row_shows_gear_not_trash(server, make_page):
    base = server
    parent = _seed_pair(base, 0)
    child = _hash_of(base, "child-0")
    _import(base, VIEWED_PGN)
    page, errors = await _open_page(base, make_page)
    await page.click(VIEW_OPEN_BTN)
    await page.click(RECENTS_SELECT)
    parent_opt = page.locator(OPTION.format(parent))
    child_opt = page.locator(OPTION.format(child))
    await parent_opt.wait_for(state="attached")
    assert await parent_opt.locator(GEAR).count() == 1
    assert await parent_opt.locator(TRASH).count() == 0
    assert await child_opt.locator(GEAR).count() == 0
    assert await child_opt.locator(TRASH).count() == 1
    assert_no_page_errors(errors)


@pytest.mark.parametrize("target", range(len(PAIRS)))
@pytest.mark.asyncio
async def test_gear_opens_recents_on_its_row(server, make_page, target):
    base = server
    parents = [_seed_pair(base, i) for i in range(len(PAIRS))]
    _import(base, VIEWED_PGN)
    page, errors = await _open_page(base, make_page)

    await page.click(VIEW_OPEN_BTN)
    await page.click(RECENTS_SELECT)
    await page.locator(OPTION.format(parents[target])).locator(GEAR).click()

    selected = page.locator(SELECTED_ROW)
    await selected.wait_for()
    assert await selected.get_attribute(HASH_ATTR) == parents[target]
    # Expanded: its fork is showing.
    await page.locator(ROW.format(_hash_of(base, f"child-{target}"))).wait_for()
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_gear_confirms_before_discarding_typed_text(server, make_page):
    base = server
    parent = _seed_pair(base, 0)
    _import(base, VIEWED_PGN)
    page, errors = await _open_page(base, make_page)

    await page.click(VIEW_OPEN_BTN)
    await page.fill(PGN_TEXTAREA, TYPED_TEXT)
    await page.click(RECENTS_SELECT)
    await page.locator(OPTION.format(parent)).locator(GEAR).click()
    await page.wait_for_selector(CONFIRM_MESSAGE)
    await page.click(CONFIRM_BUTTON.format(OPEN_SETTINGS_LABEL))

    selected = page.locator(SELECTED_ROW)
    await selected.wait_for()
    assert await selected.get_attribute(HASH_ATTR) == parent
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_tree_starts_collapsed_outside_the_current_game(server, make_page):
    base = server
    parent, child, _grandchild = _seed_chain(base)
    viewed = _import(base, VIEWED_PGN)
    page, errors = await _open_page(base, make_page)
    await _open_recents_tab(page)

    await page.locator(ROW.format(parent)).wait_for()
    assert await page.locator(ROW.format(child)).count() == 0
    assert await page.locator(SELECTED_ROW).get_attribute(HASH_ATTR) == viewed
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_tree_reveals_and_tags_the_viewed_fork(server, make_page):
    base = server
    parent, child, grandchild = _seed_chain(base)
    # Importing the child's text reuses its game_id: the fork is in view.
    _import(base, PAIRS[0][1])
    page, errors = await _open_page(base, make_page)
    await _open_recents_tab(page)

    # Revealed and selected on open; the fork's own forks stay folded.
    child_row = page.locator(ROW.format(child))
    await child_row.wait_for()
    assert await page.locator(SELECTED_ROW).get_attribute(HASH_ATTR) == child
    assert await page.locator(ROW.format(grandchild)).count() == 0
    parent_row = page.locator(ROW.format(parent))

    assert await child_row.get_attribute(LEVEL_ATTR) == CHILD_LEVEL
    assert await child_row.locator(ROW_FORK).text_content() == FORK_LABEL
    assert await child_row.locator(ROW_TAG).text_content() == VIEWING_TAG
    # The viewed fork pins its parent against a cascade.
    assert await parent_row.locator(ROW_DELETE).is_disabled()
    await _expand(page, child)
    await page.locator(ROW.format(grandchild)).wait_for()
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_parent_delete_cascades_after_confirm(server, make_page):
    base = server
    parent, child, grandchild = _seed_chain(base)
    _import(base, VIEWED_PGN)
    page, errors = await _open_page(base, make_page)
    await _open_recents_tab(page)

    await page.locator(ROW.format(parent)).locator(ROW_DELETE).click()
    assert await page.locator(CONFIRM_MESSAGE).text_content() == DELETE_TWO_FORKS
    await page.click(CONFIRM_BUTTON.format(DELETE_LABEL))

    await page.locator(ROW.format(parent)).wait_for(state="detached")
    for gid in ("parent-0", "child-0", "grandchild"):
        assert httpx.get(f"{base}{XGAME_BY_ID_PATH}{gid}").status_code == 404
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_open_shows_the_row_in_play(server, make_page):
    base = server
    target = _seed_pair(base, 0)
    _import(base, VIEWED_PGN)
    page, errors = await _open_page(base, make_page)
    await _open_recents_tab(page)

    await page.locator(ROW.format(target)).locator(ROW_OPEN).click()
    await page.click(CONFIRM_BUTTON.format(REPLACE_LABEL))
    await page.wait_for_selector(OPEN_DIALOG, state="detached")
    assert httpx.get(f"{base}{STATUS_PATH}").json()["view_hash"] == target
    assert_no_page_errors(errors)


# ---- a game ending while Settings is open ----

LIVE_PLIES = 2


@pytest.fixture
def play_server(tmp_path):
    seed = EngineRegistry(path=tmp_path / REGISTRY_FILE)
    seed.select(seed.add(
        name="FakeEngine",
        path=make_searching_fake_uci(tmp_path, "FakeEngine", bestmove="e7e5", pv="e7e5"),
    ).id)
    with run_uvicorn_subprocess(env_overrides=e2e_env(tmp_path)) as base:
        yield base


def _live_with_plies(p):
    return len(p.get("moves_san") or []) == LIVE_PLIES and not p.get("view")


@pytest.mark.asyncio
async def test_game_end_refreshes_open_tab_keeping_selection(play_server, make_page):
    base = play_server
    kept = _seed_pair(base, 0)
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    # Before goto: the observer hooks the page's websocket as it connects.
    obs = PageObserver(page)
    await page.goto(base + "/")
    await wait_perspective_ready(page)
    httpx.post(f"{base}/game/new", json={"human_white": True}).raise_for_status()
    httpx.post(f"{base}/game/move", json={"uci": "e2e4"}).raise_for_status()
    await obs.wait_board_update(_live_with_plies)
    live_id = httpx.get(f"{base}/_test/hve/state").json()["game_id"]

    await _open_recents_tab(page)
    await page.locator(ROW.format(kept)).locator(ROW_LABEL).click()
    httpx.post(f"{base}/game/resign", json={}).raise_for_status()

    await page.locator(ROW.format(_hash_of(base, live_id))).wait_for()
    assert await page.locator(SELECTED_ROW).get_attribute(HASH_ATTR) == kept
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_tab_selects_the_exported_game_in_play(play_server, make_page):
    base = play_server
    _seed_pair(base, 0)
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    obs = PageObserver(page)
    await page.goto(base + "/")
    await wait_perspective_ready(page)
    httpx.post(f"{base}/game/new", json={"human_white": True}).raise_for_status()
    httpx.post(f"{base}/game/move", json={"uci": "e2e4"}).raise_for_status()
    await obs.wait_board_update(_live_with_plies)
    # Saving the PGN exports the live game to Recents.
    httpx.get(f"{base}{EXPORT_PGN_PATH}").raise_for_status()
    live_id = httpx.get(f"{base}/_test/hve/state").json()["game_id"]

    await _open_recents_tab(page)
    selected = page.locator(SELECTED_ROW)
    await selected.wait_for()
    assert await selected.get_attribute(HASH_ATTR) == _hash_of(base, live_id)
    assert await selected.locator(ROW_TAG).text_content() == PLAYING_TAG
    assert_no_page_errors(errors)
