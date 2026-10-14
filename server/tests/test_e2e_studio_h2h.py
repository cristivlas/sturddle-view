"""E2E: Studio Head-to-Head tab (Playwright/Chromium).

Seeds a games.pgn on disk, mounts the Studio UX, opens the Head-to-Head
tab, and pins the rendered per-color Elo / margin / LOS against the same
formulas the server uses (pgn_stats) -- so the client-side reimplementation
can't drift unnoticed. Also checks the 2-engine banner gating.

Skipped if Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import math
import re

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.tournament.pgn_stats import (  # noqa: E402
    elo_from_score,
    elo_margin_from_wld,
)

from .conftest import (  # noqa: E402
    open_studio_tab,
    run_uvicorn_subprocess,
    seed_tournament,
    tournament_server_env,
    watch_page_errors,
)

WHITE_WIN, BLACK_WIN, DRAW = "1-0", "0-1", "1/2-1/2"

# Tolerances: rendered values are toFixed(1); expected are unrounded, and the
# JS LOS uses an erf approximation vs Python's exact math.erf.
ELO_TOL = 0.1
LOS_TOL = 0.2


ENGINES = ("engine-A", "engine-B", "engine-C")


def _split(games, name):
    """Tally an engine's W/L/D from its own POV, split by color -- the same
    reduction the client does over the games list."""
    white = {"w": 0, "l": 0, "d": 0}
    black = {"w": 0, "l": 0, "d": 0}
    for _rnd, w, b, result in games:
        if w == name:
            key = "w" if result == WHITE_WIN else "l" if result == BLACK_WIN else "d"
            white[key] += 1
        elif b == name:
            key = "w" if result == BLACK_WIN else "l" if result == WHITE_WIN else "d"
            black[key] += 1
    return white, black


def _los(rec):
    """LOS = P(true Elo > 0) from the same SE that yields the CI -- mirrors
    the client's los(). None when n<2; 100/0 for a perfect score."""
    wins, losses, draws = rec["w"], rec["l"], rec["d"]
    n = wins + losses + draws
    if n == 0:
        return None
    elo = elo_from_score((wins + 0.5 * draws) / n)
    if elo is None:
        return 100.0 if wins > losses else 0.0
    margin = elo_margin_from_wld(wins, losses, draws)
    if margin is None or margin <= 0:
        return None
    return 0.5 * (1 + math.erf((elo / (margin / 1.96)) / math.sqrt(2))) * 100


def _expect_elo(rec):
    wins, losses, draws = rec["w"], rec["l"], rec["d"]
    n = wins + losses + draws
    return elo_from_score((wins + 0.5 * draws) / n), elo_margin_from_wld(wins, losses, draws)


# Margin glyph is U+00B1 since 18065e8; keep +/- accepted for safety.
_ELO_RE = re.compile(r"\s*([+-]?\d+(?:\.\d+)?)(?:\s*(?:\+/-|\u00b1)\s*(\d+(?:\.\d+)?))?")


def _parse_elo(text):
    m = _ELO_RE.match(text)
    return float(m.group(1)), (float(m.group(2)) if m.group(2) else None)


_READ_ROWS = """() => {
  const out = {};
  let cur = null;
  for (const tr of document.querySelectorAll('.h2h-tbl tbody tr')) {
    if (tr.classList.contains('h2h-group')) { cur = tr.textContent.trim(); out[cur] = {}; continue; }
    const td = [...tr.querySelectorAll('td')];
    out[cur][td[0].textContent.trim()] = {
      wld: td[3].textContent.trim(), elo: td[4].textContent.trim(), los: td[5].textContent.trim(),
    };
  }
  const banner = document.querySelector('.h2h-banner');
  return { rows: out, banner: banner ? banner.textContent.trim() : null };
}"""


@pytest.mark.asyncio
async def test_h2h_two_engine_values_and_banner(tmp_path, make_page):
    """A 2-engine match shows the vs banner and per-color rows whose Elo /
    margin / LOS match the server's formulas; Overall == White + Black."""
    games = [
        ("1", "engine-A", "engine-B", WHITE_WIN),
        ("2", "engine-A", "engine-B", WHITE_WIN),
        ("3", "engine-A", "engine-B", WHITE_WIN),
        ("4", "engine-A", "engine-B", WHITE_WIN),
        ("5", "engine-A", "engine-B", DRAW),
        ("6", "engine-A", "engine-B", BLACK_WIN),
        ("1", "engine-B", "engine-A", BLACK_WIN),
        ("2", "engine-B", "engine-A", BLACK_WIN),
        ("3", "engine-B", "engine-A", DRAW),
        ("4", "engine-B", "engine-A", WHITE_WIN),
        ("5", "engine-B", "engine-A", WHITE_WIN),
        ("6", "engine-B", "engine-A", WHITE_WIN),
    ]
    seed_tournament(tmp_path, "h2h-pair", ["engine-A", "engine-B"], games)

    with run_uvicorn_subprocess(env_overrides=tournament_server_env(tmp_path, ENGINES)) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        errors = watch_page_errors(page)
        await open_studio_tab(page, base, "h2h")
        await page.wait_for_selector(".h2h-banner")
        await page.wait_for_function(
            "() => document.querySelectorAll('.h2h-tbl tbody tr.h2h-group').length === 2",
        )
        data = await page.evaluate(_READ_ROWS)

        white, black = _split(games, "engine-A")
        rows = data["rows"]["engine-A"]

        for side, rec in (("White", white), ("Black", black)):
            exp_elo, exp_margin = _expect_elo(rec)
            elo, margin = _parse_elo(rows[side]["elo"])
            assert abs(elo - exp_elo) <= ELO_TOL, f"{side} elo {elo} vs {exp_elo}"
            assert abs(margin - exp_margin) <= ELO_TOL, f"{side} margin {margin} vs {exp_margin}"
            los = float(rows[side]["los"].rstrip("%"))
            assert abs(los - _los(rec)) <= LOS_TOL, f"{side} los {los} vs {_los(rec)}"

        # Overall is the white+black sum (single source -- #3 review item).
        overall = {k: white[k] + black[k] for k in white}
        assert rows["Overall"]["wld"] == f"+{overall['w']} ={overall['d']} -{overall['l']}"

        assert data["banner"] is not None
        assert "engine-A vs engine-B" in data["banner"]
        assert errors == [], "JS errors:\n" + "\n".join(errors)


@pytest.mark.asyncio
async def test_h2h_three_engine_no_banner(tmp_path, make_page):
    """A 3-engine round-robin shows a per-engine group for each engine and no
    vs banner (banner gates on the 2-engine roster, not played-so-far)."""
    games = [
        ("1", "engine-A", "engine-B", WHITE_WIN),
        ("1", "engine-B", "engine-A", DRAW),
        ("2", "engine-A", "engine-C", WHITE_WIN),
        ("2", "engine-C", "engine-A", WHITE_WIN),
        ("3", "engine-B", "engine-C", DRAW),
        ("3", "engine-C", "engine-B", BLACK_WIN),
    ]
    seed_tournament(tmp_path, "h2h-rr3", list(ENGINES), games)

    with run_uvicorn_subprocess(env_overrides=tournament_server_env(tmp_path, ENGINES)) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        errors = watch_page_errors(page)
        await open_studio_tab(page, base, "h2h")
        await page.wait_for_function(
            "() => document.querySelectorAll('.h2h-tbl tbody tr.h2h-group').length === 3",
        )
        data = await page.evaluate(_READ_ROWS)

        assert data["banner"] is None
        assert set(data["rows"]) == {"engine-A", "engine-B", "engine-C"}
        assert errors == [], "JS errors:\n" + "\n".join(errors)
