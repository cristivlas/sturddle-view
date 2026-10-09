"""E2E: the Import dialog's Recents list never offers the game open in view.

Picking it would re-import what is already on the board, and deleting it
would pull the board out from under the viewer. So its row is disabled,
with a muted "viewing" tag in place of the trash; every other row keeps
its trash. Covers each way a viewed game gets its row: an import, a
committed position edit (the new view at the edited FEN), and a committed
annotation (the annotated copy replaces the row).

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
    run_uvicorn_subprocess,
    wait_perspective_ready,
    watch_page_errors,
)

VIEWPORT = {"width": 1400, "height": 900}

VIEW_OPEN_BTN = "#view-import"
RECENTS_SELECT = "wa-dialog[open] wa-select"
ROW = 'wa-option[data-hash="{}"]'
TRASH = ".recent-del"
VIEWING_TAG = ".recent-viewing"
VIEWING_TEXT = "viewing"

OTHER_PGN = '[White "A"]\n[Black "B"]\n\n1. d4 d5 *\n'
VIEWED_PGN = '[White "C"]\n[Black "D"]\n\n1. e4 e5 *\n'
EDITED_FEN = "4k3/8/8/8/8/8/8/4K3 w - - 0 1"
NOTE = "A note."


@pytest.fixture
def server(tmp_path):
    # The game endpoints need a configured engine; viewing never runs it.
    seed = EngineRegistry(path=tmp_path / REGISTRY_FILE)
    seed.select(seed.add(name="MyEngine", path="/nonexistent/engine").id)
    with run_uvicorn_subprocess(env_overrides=e2e_env(tmp_path)) as base:
        yield base


def _import(base, text) -> str:
    r = httpx.post(f"{base}/game/import", json={"format": "pgn", "text": text})
    r.raise_for_status()
    return r.json()["hash"]


def _view_by_import(base) -> str:
    """Import the viewed game last, so it is the one in view."""
    return _import(base, VIEWED_PGN)


def _view_by_fen_edit(base) -> str:
    """Commit a position edit on top of an imported view: the new view at
    the edited FEN is the one in view, under its own recents row."""
    _import(base, VIEWED_PGN)
    httpx.post(f"{base}/game/edit/start", json={}).raise_for_status()
    r = httpx.post(f"{base}/game/edit/commit", json={"fen": EDITED_FEN})
    r.raise_for_status()
    return r.json()["hash"]


def _view_by_annotation(base) -> str:
    """Commit an annotation (position unchanged) on an imported view: the
    annotated copy replaces its recents row under a new hash."""
    _import(base, VIEWED_PGN)
    fen = httpx.post(f"{base}/game/edit/start", json={}).json()["fen"]
    r = httpx.post(f"{base}/game/edit/commit", json={
        "fen": fen, "apply_comment": True, "comment_text": NOTE,
    })
    r.raise_for_status()
    return r.json()["hash"]


@pytest.mark.parametrize(
    "enter_view", [_view_by_import, _view_by_fen_edit, _view_by_annotation],
)
@pytest.mark.asyncio
async def test_viewed_row_is_disabled_without_trash(server, make_page, enter_view):
    base = server
    other = _import(base, OTHER_PGN)
    viewed = enter_view(base)
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await page.goto(base + "/")
    await wait_perspective_ready(page)

    await page.click(VIEW_OPEN_BTN)
    await page.click(RECENTS_SELECT)
    viewed_row = page.locator(ROW.format(viewed))
    other_row = page.locator(ROW.format(other))
    await viewed_row.wait_for(state="attached")
    await other_row.wait_for(state="attached")

    assert await viewed_row.evaluate("(o) => o.disabled")
    assert await viewed_row.locator(TRASH).count() == 0
    assert await viewed_row.locator(VIEWING_TAG).text_content() == VIEWING_TEXT
    assert not await other_row.evaluate("(o) => o.disabled")
    assert await other_row.locator(TRASH).count() == 1
    assert_no_page_errors(errors)
