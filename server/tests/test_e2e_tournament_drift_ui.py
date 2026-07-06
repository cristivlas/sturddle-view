"""E2E: engine-settings drift gate at tournament start (Playwright).

Covers docs/engine-drift-restart.md: drifted engine settings surface the
three-way dialog (Update & start / Start with saved / Esc-cancel); a
drift-free start shows no dialog. fastchess is sys.executable, so a
started tournament promptly lands in ``failed`` -- the assertions target
the gate's behavior, not the run.

NOTE: ``wait_for_function`` predicates must be SYNC -- Playwright does
not await an async predicate; the returned Promise is truthy and the
wait passes immediately. Async work (module imports, fetch polling)
goes through ``page.evaluate`` (which does await) or
``conftest.wait_for_async_predicate``.

Skipped if Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import sys

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.engines import EngineRegistry  # noqa: E402
from sturddle_view.tournament.store import TournamentStore  # noqa: E402

from .conftest import (  # noqa: E402
    pin_arena_tournament_ux,
    run_uvicorn_subprocess,
    wait_for_async_predicate,
    wait_perspective_ready,
)

_SEED = 42
_DRIFT_OPTIONS = {"Foo": "bar"}


def _server_env(tmp_path):
    return {
        "SV_PGN_DIR": str(tmp_path / "pgn"),
        "SV_TOURNAMENT_ROOT": str(tmp_path / "tournaments"),
        "SV_TOURNAMENT_FASTCHESS_PATH": sys.executable,
        "SV_ENGINE_REGISTRY_PATH": str(tmp_path / "engines.json"),
        "SV_SETTINGS_FILE": str(tmp_path / "settings.json"),
        "SV_GAME_STATE_PATH": str(tmp_path / "current_game.json"),
        "SV_IMPORTS_DIR": str(tmp_path / "imports"),
    }


def _seed(tmp_path, *, engine_options=None, ref_options=None):
    """Registry with two engines + one idle tournament whose frozen refs
    mirror the registry exactly, except ``engine_options`` (applied to the
    first registry entry) and ``ref_options`` (frozen into its ref)."""
    registry = EngineRegistry(path=tmp_path / "engines.json")
    a = registry.add(name="engine-A", path=sys.executable, options=engine_options)
    b = registry.add(name="engine-B", path=sys.executable)
    ref_a = {"id": a.id, "name": a.name, "cmd": a.path}
    if ref_options:
        ref_a["options"] = dict(ref_options)
    t = TournamentStore(tmp_path / "tournaments").create(
        name="drift",
        template={"tc": "10+0.1", "rounds": 1, "seed": _SEED},
        engines=[ref_a, {"id": b.id, "name": b.name, "cmd": b.path}],
    )
    return t


def _watch_errors(page):
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(f"console.{msg.type}: {msg.text}")
            if msg.type == "error" else None)
    return errors


async def _open_tournaments(page, base):
    await pin_arena_tournament_ux(page)
    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)
    await page.click('button[data-perspective="engines"]')
    await page.wait_for_selector(".tournament-row")
    await page.click(".tournament-row")


async def _drift_labels(page) -> dict:
    """Dialog label + verb labels from the production module (evaluate
    awaits promises, unlike wait_for_function)."""
    return await page.evaluate(
        """async () => {
            const m = await import('/ui/app/tournament-restart.js');
            return {
                dialog: m.DRIFT_DIALOG_LABEL,
                update: m.UPDATE_AND_START_LABEL,
                saved: m.START_WITH_SAVED_LABEL,
            };
        }"""
    )


async def _wait_drift_dialog_open(page, labels):
    await page.wait_for_function(
        """(label) => {
            const d = [...document.querySelectorAll('wa-dialog')]
                .find((x) => x.label === label);
            return !!(d && d.open);
        }""",
        arg=labels["dialog"],
    )


async def _wait_status_left_idle(page):
    await wait_for_async_predicate(
        page,
        """async () => {
            const r = await fetch('/api/tournaments');
            const t = (await r.json()).tournaments[0];
            return t && t.status !== 'idle';
        }""",
    )


async def _fetch_tournament(page) -> dict:
    return await page.evaluate(
        "async () => (await (await fetch('/api/tournaments')).json()).tournaments[0]"
    )


@pytest.mark.asyncio
async def test_no_drift_starts_without_dialog(tmp_path, make_page):
    _seed(tmp_path)
    with run_uvicorn_subprocess(env_overrides=_server_env(tmp_path)) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        errors = _watch_errors(page)
        await _open_tournaments(page, base)

        await page.click(".tournaments-ribbon .t-start")
        # The start POST fires straight through -- a dialog would block it
        # and this wait would time out.
        await _wait_status_left_idle(page)
        assert not await page.evaluate(
            "() => [...document.querySelectorAll('wa-dialog')].some((d) => d.open)"
        )
        assert errors == [], "JS errors during test:\n" + "\n".join(errors)


@pytest.mark.asyncio
async def test_drift_update_and_start_refreezes(tmp_path, make_page):
    _seed(tmp_path, engine_options=_DRIFT_OPTIONS)  # registry drifted vs ref
    with run_uvicorn_subprocess(env_overrides=_server_env(tmp_path)) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        errors = _watch_errors(page)
        await _open_tournaments(page, base)
        labels = await _drift_labels(page)

        await page.click(".tournaments-ribbon .t-start")
        await _wait_drift_dialog_open(page, labels)
        await page.click(f'wa-dialog wa-button:has-text("{labels["update"]}")')
        await _wait_status_left_idle(page)

        t = await _fetch_tournament(page)
        # Re-frozen from the registry; round-tripped template kept its seed
        # and gained the re-folded resource caps.
        assert t["engines"][0]["options"] == _DRIFT_OPTIONS
        assert t["template"]["seed"] == _SEED
        assert t["template"]["max_threads"] == 1
        assert t["template"]["max_hash_mb"] == 16
        assert errors == [], "JS errors during test:\n" + "\n".join(errors)


@pytest.mark.asyncio
async def test_drift_cancel_then_start_with_saved(tmp_path, make_page):
    _seed(tmp_path, engine_options=_DRIFT_OPTIONS)
    with run_uvicorn_subprocess(env_overrides=_server_env(tmp_path)) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        errors = _watch_errors(page)
        await _open_tournaments(page, base)
        labels = await _drift_labels(page)

        # Esc cancels: dialog closes, tournament stays idle.
        await page.click(".tournaments-ribbon .t-start")
        await _wait_drift_dialog_open(page, labels)
        await page.keyboard.press("Escape")
        await page.wait_for_function(
            "() => ![...document.querySelectorAll('wa-dialog')].some((d) => d.open)"
        )
        t = await _fetch_tournament(page)
        assert t["status"] == "idle"

        # Start with saved: runs the snapshot; frozen ref stays as saved.
        await page.click(".tournaments-ribbon .t-start")
        await _wait_drift_dialog_open(page, labels)
        await page.click(f'wa-dialog wa-button:has-text("{labels["saved"]}")')
        await _wait_status_left_idle(page)

        t = await _fetch_tournament(page)
        assert "options" not in t["engines"][0]
        assert errors == [], "JS errors during test:\n" + "\n".join(errors)
