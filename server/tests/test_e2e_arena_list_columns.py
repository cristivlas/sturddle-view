"""E2E: Arena tournament list row layout (Playwright/Chromium).

Every row keeps the SPRT slot (an invisible pill on non-SPRT rows), so names
line up whether or not a row carries the SPRT badge. Non-running rows end in
type / time control / games / date columns; games reads played / total, or
the played count alone for SPRT, which has no fixed total.

Skipped if Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from .conftest import (  # noqa: E402
    assert_no_page_errors,
    goto_arena_tournaments,
    run_uvicorn_subprocess,
    seed_tournament,
    tournament_server_env,
    watch_page_errors,
)

ENGINES = ("engine-A", "engine-B")
VIEWPORT = {"width": 1400, "height": 900}
TC = "5+0.05"
# 2 engines x 1 round x 2 games per round = 2 scheduled games.
RR_NAME, RR_TEMPLATE = "rr", {"tc": TC, "tournament_type": "roundrobin", "rounds": 1}
SPRT_NAME, SPRT_TEMPLATE = "sprt", {
    "tc": TC, "tournament_type": "roundrobin",
    "sprt": {"elo0": 0, "elo1": 10, "alpha": 0.05, "beta": 0.05},
}
ONE_GAME = [("1", *ENGINES, "1-0")]

# Per row: name, its left edge, whether it shows the SPRT badge, and the
# metadata column texts.
_ROWS = """() => Object.fromEntries([...document.querySelectorAll('.tournament-row')].map((r) => {
  const name = r.querySelector('.tournament-name');
  return [name.textContent, {
    x: name.getBoundingClientRect().left,
    sprt: !!r.querySelector('.tournament-status--sprt'),
    meta: [...r.querySelectorAll('.tournament-meta > span')].map((s) => s.textContent),
  }];
}))"""


@pytest.fixture
def server(tmp_path):
    seed_tournament(tmp_path, RR_NAME, list(ENGINES), ONE_GAME, template=RR_TEMPLATE)
    seed_tournament(tmp_path, SPRT_NAME, list(ENGINES), ONE_GAME, template=SPRT_TEMPLATE)
    with run_uvicorn_subprocess(env_overrides=tournament_server_env(tmp_path, ENGINES)) as base:
        yield base


async def _rows(make_page, server):
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await goto_arena_tournaments(page, server)
    return await page.evaluate(_ROWS), errors


@pytest.mark.asyncio
async def test_names_line_up_with_and_without_sprt(server, make_page):
    rows, errors = await _rows(make_page, server)
    assert rows[SPRT_NAME]["sprt"] and not rows[RR_NAME]["sprt"]
    assert rows[SPRT_NAME]["x"] == rows[RR_NAME]["x"], rows
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_metadata_columns(server, make_page):
    """Type, TC, games (played / total; SPRT: played only), and a date."""
    rows, errors = await _rows(make_page, server)
    rr_type, rr_tc, rr_games, rr_date = rows[RR_NAME]["meta"]
    assert (rr_type, rr_tc, rr_games) == ("Round-robin", TC, "1 / 2")
    assert rr_date
    assert rows[SPRT_NAME]["meta"][2] == "1"
    assert_no_page_errors(errors)
