"""E2E: a legacy tournament (engine_defaults snapshot predating opening-book
support, so it lacks book_* keys) seeds its Edit dialog from the Common book
defaults -- rather than showing an empty book -- so edits/duplicates start from
a sensible value. A tournament whose snapshot DOES carry book_path (including an
explicit book-less null) is authoritative and is NOT re-seeded.

Skipped if Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import (  # noqa: E402
    pin_arena_tournament_ux,
    run_uvicorn_subprocess,
    wait_perspective_ready,
)

COMMON_BOOK = "/common/opening.epd"


def _make_fake_uci(root: Path, id_name: str) -> str:
    py = root / f"{id_name}.py"
    py.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "while True:\n"
        "    line = sys.stdin.readline()\n"
        "    if not line: break\n"
        "    line = line.strip()\n"
        f"    if line == 'uci': sys.stdout.write('id name {id_name}\\nuciok\\n'); sys.stdout.flush()\n"
        "    elif line == 'isready': sys.stdout.write('readyok\\n'); sys.stdout.flush()\n"
        "    elif line == 'quit': break\n"
    )
    if sys.platform.startswith("win"):
        wrapper = root / f"{id_name}.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{py}" %*\r\n')
        return str(wrapper)
    py.chmod(py.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(py)


def _env(tmp_path):
    registry_path = tmp_path / "engines.json"
    seed = EngineRegistry(path=registry_path)
    for n in ("alpha", "beta"):
        seed.add(name=n, path=_make_fake_uci(tmp_path, n))
    (tmp_path / "settings.json").write_text(
        json.dumps({"engine_default_book_path": COMMON_BOOK}), encoding="utf-8",
    )
    return {
        "SV_PGN_DIR": str(tmp_path / "pgn"),
        "SV_TOURNAMENT_ROOT": str(tmp_path / "tournaments"),
        "SV_TOURNAMENT_FASTCHESS_PATH": sys.executable,
        "SV_ENGINE_REGISTRY_PATH": str(registry_path),
        "SV_IMPORTS_DIR": str(tmp_path / "imports"),
        "SV_SETTINGS_FILE": str(tmp_path / "settings.json"),
        "SV_GAME_STATE_PATH": str(tmp_path / "current_game.json"),
    }


def _strip_book_keys(tournaments_root: Path):
    """Rewrite every tournament state.json to drop book_* keys from
    engine_defaults, simulating a snapshot written before book support."""
    for state in tournaments_root.glob("*/state.json"):
        data = json.loads(state.read_text())
        ed = data.get("engine_defaults") or {}
        for k in ("book_path", "book_plies", "book_order"):
            ed.pop(k, None)
        data["engine_defaults"] = ed
        state.write_text(json.dumps(data, indent=2))


async def _open_edit(page):
    await page.click('button[data-perspective="engines"]')
    await page.wait_for_selector(".tournaments-panel")
    await page.wait_for_selector(".tournament-row")
    await page.click(".tournament-row")
    await page.wait_for_function(
        "() => document.querySelector('.t-edit') && !document.querySelector('.t-edit').disabled")
    await page.click(".t-edit")
    await page.locator(".ttf-book .path-field").first.wait_for(state="attached")


async def _book_path_value(page):
    return await page.evaluate(
        """() => document.querySelector('.ttf-book .path-field')?.value ?? null""")


@pytest.mark.asyncio
async def test_legacy_tournament_edit_seeds_common_book(tmp_path, make_page):
    env = _env(tmp_path)
    with run_uvicorn_subprocess(env_overrides=env) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        # Create a tournament, then strip its book snapshot to make it "legacy".
        await pin_arena_tournament_ux(page)
        await page.goto(base + "/")
        await page.wait_for_selector("#play-perspective")
        await wait_perspective_ready(page)
        await page.evaluate(
            """async () => {
                await fetch('/api/tournaments', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        name: 'legacy', engines: [
                            { id: 'alpha', name: 'alpha', cmd: 'alpha' },
                            { id: 'beta', name: 'beta', cmd: 'beta' },
                        ],
                        template: { tc: '10+0.1', rounds: 2 },
                    }),
                });
            }""")
        _strip_book_keys(Path(env["SV_TOURNAMENT_ROOT"]))
        await page.reload()
        await page.wait_for_selector("#play-perspective")
        await wait_perspective_ready(page)

        await _open_edit(page)
        assert (await _book_path_value(page)) == COMMON_BOOK, \
            "legacy tournament (no frozen book) should seed Common book in Edit"
