"""E2E: Tournaments perspective renders correctly (Playwright/Chromium).

Skipped if Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import sys

import pytest

pytest.importorskip("playwright.async_api")

from sturddle_view.app import create_app  # noqa: E402
from sturddle_view.config import Settings  # noqa: E402
from sturddle_view.engines import EngineRegistry  # noqa: E402
from sturddle_view.tournament.fastchess import FastchessRunner  # noqa: E402

from .conftest import run_uvicorn  # noqa: E402


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(
        FastchessRunner, "detect_binary",
        staticmethod(lambda configured: configured),
    )

    settings = Settings(token="test-token", auth_disabled=True)
    settings.pgn_dir = tmp_path / "pgn"
    settings.tournament_root = str(tmp_path / "tournaments")
    # Start with no fastchess configured → empty state.
    settings.tournament_fastchess_path = None
    registry = EngineRegistry(path=tmp_path / "engines.json")
    # Pre-register two engines so the New Tournament dialog has something
    # to work with later if we add a clicking-test.
    registry.add(name="engine-A", path=sys.executable)
    registry.add(name="engine-B", path=sys.executable)
    app = create_app(settings=settings, engine_registry=registry)
    with run_uvicorn(app) as (base, _s):
        yield base, app


@pytest.mark.asyncio
async def test_tournaments_perspective_smoke(server, make_page):
    base, app = server

    _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    page.on("console", lambda msg: page_errors.append(f"console.{msg.type}: {msg.text}")
            if msg.type == "error" else None)
    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")

    await page.click('button[data-perspective="engines"]')
    await page.wait_for_selector("#engines-perspective")
    await page.wait_for_selector(".tournaments-panel")

    await page.wait_for_function(
        """() => {
            const e = document.querySelector('.tournaments-empty .empty-message');
            return e && /fastchess not configured/i.test(e.textContent);
        }""",
    )

    disabled = await page.evaluate(
        "() => document.querySelector('.t-new').disabled"
    )
    assert disabled is True

    assert page_errors == [], "JS errors during test:\n" + "\n".join(page_errors)


@pytest.mark.asyncio
async def test_tournaments_perspective_with_existing_tournament(tmp_path, monkeypatch, make_page):
    """Boot the server with fastchess configured AND a tournament already
    on disk. The Tournaments tab should render the row with the four
    expected verbs and an 'idle' status badge."""
    monkeypatch.setattr(
        FastchessRunner, "detect_binary",
        staticmethod(lambda configured: configured),
    )

    settings = Settings(token="test-token", auth_disabled=True)
    settings.pgn_dir = tmp_path / "pgn"
    settings.tournament_root = str(tmp_path / "tournaments")
    settings.tournament_fastchess_path = sys.executable
    registry = EngineRegistry(path=tmp_path / "engines.json")
    registry.add(name="engine-A", path=sys.executable)
    registry.add(name="engine-B", path=sys.executable)
    app = create_app(settings=settings, engine_registry=registry)

    # Pre-seed a tournament on disk.
    app.state.tournament_store.create(
        name="smoke",
        template={"tc": "10+0.1"},
        engines=[{"name": "A", "cmd": "/bin/A"}, {"name": "B", "cmd": "/bin/B"}],
    )

    with run_uvicorn(app) as (base, _s):
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on("console", lambda msg: page_errors.append(
            f"console.{msg.type}: {msg.text}"
        ) if msg.type == "error" else None)
        await page.goto(base + "/")
        await page.wait_for_selector("#play-perspective")
        await page.click('button[data-perspective="engines"]')
        await page.wait_for_selector(".tournament-row")

        # Select the row so ribbon verbs reflect that tournament.
        await page.click('.tournament-row')
        row_info = await page.evaluate(
            """() => {
                const row = document.querySelector('.tournament-row');
                const ribbon = document.querySelector('.tournaments-ribbon');
                const labelOf = (sel) => ribbon.querySelector(sel)?.getAttribute('aria-label');
                return {
                    name: row.querySelector('.tournament-name').textContent,
                    status: row.querySelector('.tournament-status').textContent,
                    ribbon_labels: {
                        start: labelOf('.t-start'),
                        stop: labelOf('.t-stop'),
                        workspace: labelOf('.t-workspace'),
                        info: labelOf('.t-info'),
                        remove: labelOf('.t-remove'),
                    },
                    new_button_disabled: document.querySelector('.t-new').disabled,
                };
            }"""
        )
        assert row_info["name"] == "smoke"
        assert row_info["status"].strip() == "idle"
        assert row_info["ribbon_labels"] == {
            "start": "Start",
            "stop": "Pause",
            "workspace": "Open workspace",
            "info": "Info",
            "remove": "Remove",
        }
        assert row_info["new_button_disabled"] is False

        await page.click("#settings-btn")
        await page.wait_for_function(
            """() => document.querySelector('wa-dialog wa-tab[panel="tournament"]')""",
        )
        await page.click('wa-dialog wa-tab[panel="tournament"]')
        await page.wait_for_function(
            """() => document.querySelector('wa-dialog wa-tab-panel[name="tournament"] wa-input[data-key="tc"]')""",
        )
        await page.evaluate(
            """() => {
                const setVal = (k, v) => {
                    const el = document.querySelector(
                        `wa-dialog wa-tab-panel[name="tournament"] wa-input[data-key="${k}"]`
                    );
                    el.value = v;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                };
                setVal('tc', '60+0.6');
                setVal('rounds', '42');
                setVal('games_in_parallel', '4');
            }"""
        )
        await page.wait_for_function(
            """async () => {
                const r = await fetch('/api/tournament-settings');
                const tpl = (await r.json()).default_template || {};
                return tpl.tc === '60+0.6' && tpl.rounds === 42 && tpl.games_in_parallel === 4;
            }""",
        )

        assert page_errors == [], "JS errors during test:\n" + "\n".join(page_errors)


@pytest.mark.asyncio
async def test_tournament_workspace_opens_three_windows(tmp_path, monkeypatch, make_page):
    """Slice 8: clicking 'Open workspace' on an idle tournament spawns
    two WinBox windows (Standings / Schedule); event log is deferred."""
    monkeypatch.setattr(
        FastchessRunner, "detect_binary",
        staticmethod(lambda configured: configured),
    )

    settings = Settings(token="test-token", auth_disabled=True)
    settings.pgn_dir = tmp_path / "pgn"
    settings.tournament_root = str(tmp_path / "tournaments")
    settings.tournament_fastchess_path = sys.executable
    registry = EngineRegistry(path=tmp_path / "engines.json")
    registry.add(name="engine-A", path=sys.executable)
    registry.add(name="engine-B", path=sys.executable)
    app = create_app(settings=settings, engine_registry=registry)
    app.state.tournament_store.create(
        name="ws-smoke",
        template={"tc": "10+0.1"},
        engines=[{"name": "A", "cmd": "/bin/A"}, {"name": "B", "cmd": "/bin/B"}],
    )

    with run_uvicorn(app) as (base, _s):
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on("console", lambda msg: page_errors.append(
            f"console.{msg.type}: {msg.text}"
        ) if msg.type == "error" else None)
        await page.goto(base + "/")
        await page.wait_for_selector("#play-perspective")
        await page.click('button[data-perspective="engines"]')
        await page.wait_for_selector(".tournament-row")

        await page.click('.tournament-row')
        await page.click('.tournaments-ribbon .t-workspace')

        # Idle tournament: only Standings auto-opens. Live Games
        # opens lazily when the tournament is running.
        await page.wait_for_function(
            "() => document.querySelectorAll('.winbox.sturddle-wb').length === 1",
        )
        titles = await page.evaluate(
            """() => [...document.querySelectorAll('.winbox.sturddle-wb .wb-title')]
                        .map(t => t.textContent)"""
        )
        assert any("Standings" in t for t in titles)

        await page.wait_for_function(
            """() => /No games/.test(
                document.querySelector('.wb-standings .wb-empty')?.textContent || ''
            )""",
        )

        await page.evaluate(
            """() => document.querySelectorAll('.winbox.sturddle-wb .wb-close')
                        .forEach(b => b.click())"""
        )
        await page.wait_for_function(
            "() => document.querySelectorAll('.winbox.sturddle-wb').length === 0",
        )
        await page.click('.tournament-row')
        await page.click('.tournaments-ribbon .t-workspace')
        await page.wait_for_function(
            "() => document.querySelectorAll('.winbox.sturddle-wb').length === 1",
        )

        assert page_errors == [], "JS errors during test:\n" + "\n".join(page_errors)
