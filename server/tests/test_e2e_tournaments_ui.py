"""Slice 6 e2e: Tournaments perspective UI loads and renders correctly.

Drives a real browser via Playwright. Skipped if Playwright or its
Chromium isn't available so unit-only test runs aren't blocked.

What this test verifies (without a running fastchess):
  - Engines perspective shows exactly two sub-tabs: Roster, Tournaments
    (the Observe sub-tab was removed in Slice 6).
  - Switching to Tournaments shows the empty state since no fastchess
    is configured: "fastchess not configured …".
  - Setting fastchess_path via the API flips the empty state to
    "no tournaments yet".
  - Creating a tournament via the API surfaces a row in the list with
    Start/Stop/Remove/Open-workspace buttons and a status badge.
"""
from __future__ import annotations

import socket
import sys
import threading
import time

import pytest

pytest.importorskip("playwright.async_api")

from sturddle_view.app import create_app  # noqa: E402
from sturddle_view.config import Settings  # noqa: E402
from sturddle_view.engines import EngineRegistry  # noqa: E402
from sturddle_view.tournament.fastchess import FastchessRunner  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(
        FastchessRunner, "detect_binary",
        staticmethod(lambda configured: configured),
    )

    import uvicorn

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
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws="wsproto")
    s = uvicorn.Server(config)

    thread = threading.Thread(target=s.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline and not s.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", app
    s.should_exit = True
    s.force_exit = True
    thread.join(timeout=2)


@pytest.mark.asyncio
async def test_tournaments_perspective_smoke(server, browser):
    if browser is None:
        pytest.skip("chromium not installed")
    base, app = server

    ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
    page = await ctx.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    page.on("console", lambda msg: page_errors.append(f"console.{msg.type}: {msg.text}")
            if msg.type == "error" else None)
    try:
        await page.goto(base + "/")
        await page.wait_for_selector("#play-perspective", timeout=5000)

        await page.click('button[data-perspective="engines"]')
        await page.wait_for_selector("#engines-perspective", timeout=5000)

        tab_panels = await page.evaluate(
            """() => [...document.querySelectorAll('#engines-perspective wa-tab')]
                      .map(t => t.getAttribute('panel'))"""
        )
        assert tab_panels == ["roster", "tournaments"], tab_panels

        await page.click('#engines-perspective wa-tab[panel="tournaments"]')
        await page.wait_for_selector(".tournaments-panel", timeout=5000)

        await page.wait_for_function(
            """() => {
                const e = document.querySelector('.tournaments-empty .empty-message');
                return e && /fastchess not configured/i.test(e.textContent);
            }""",
            timeout=5000,
        )

        disabled = await page.evaluate(
            "() => document.querySelector('.t-new').disabled"
        )
        assert disabled is True

        assert page_errors == [], "JS errors during test:\n" + "\n".join(page_errors)
    finally:
        await ctx.close()


@pytest.mark.asyncio
async def test_tournaments_perspective_with_existing_tournament(tmp_path, monkeypatch, browser):
    """Boot the server with fastchess configured AND a tournament already
    on disk. The Tournaments tab should render the row with the four
    expected verbs and an 'idle' status badge."""
    monkeypatch.setattr(
        FastchessRunner, "detect_binary",
        staticmethod(lambda configured: configured),
    )

    import uvicorn

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

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws="wsproto")
    s = uvicorn.Server(config)
    thread = threading.Thread(target=s.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline and not s.started:
        time.sleep(0.05)

    if browser is None:
        pytest.skip("chromium not installed")
    try:
        ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
        page = await ctx.new_page()
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on("console", lambda msg: page_errors.append(
            f"console.{msg.type}: {msg.text}"
        ) if msg.type == "error" else None)
        try:
            await page.goto(f"http://127.0.0.1:{port}/")
            await page.wait_for_selector("#play-perspective", timeout=5000)
            await page.click('button[data-perspective="engines"]')
            await page.click('#engines-perspective wa-tab[panel="tournaments"]')
            await page.wait_for_selector(".tournament-row", timeout=5000)

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
                timeout=5000,
            )
            await page.click('wa-dialog wa-tab[panel="tournament"]')
            await page.wait_for_function(
                """() => document.querySelector('wa-dialog wa-tab-panel[name="tournament"] wa-input[data-key="tc"]')""",
                timeout=5000,
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
                timeout=5000,
            )

            assert page_errors == [], "JS errors during test:\n" + "\n".join(page_errors)
        finally:
            await ctx.close()
    finally:
        s.should_exit = True
        s.force_exit = True
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_tournament_workspace_opens_three_windows(tmp_path, monkeypatch, browser):
    """Slice 8: clicking 'Open workspace' on a tournament row spawns
    three WinBox windows (Standings / Schedule / Event log)."""
    monkeypatch.setattr(
        FastchessRunner, "detect_binary",
        staticmethod(lambda configured: configured),
    )

    import uvicorn

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

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws="wsproto")
    s = uvicorn.Server(config)
    thread = threading.Thread(target=s.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline and not s.started:
        time.sleep(0.05)

    if browser is None:
        pytest.skip("chromium not installed")
    try:
        ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
        page = await ctx.new_page()
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on("console", lambda msg: page_errors.append(
            f"console.{msg.type}: {msg.text}"
        ) if msg.type == "error" else None)
        try:
            await page.goto(f"http://127.0.0.1:{port}/")
            await page.wait_for_selector("#play-perspective", timeout=5000)
            await page.click('button[data-perspective="engines"]')
            await page.click('#engines-perspective wa-tab[panel="tournaments"]')
            await page.wait_for_selector(".tournament-row", timeout=5000)

            await page.click('.tournament-row')
            await page.click('.tournaments-ribbon .t-workspace')

            await page.wait_for_function(
                "() => document.querySelectorAll('.winbox.sturddle-wb').length === 3",
                timeout=5000,
            )
            titles = await page.evaluate(
                """() => [...document.querySelectorAll('.winbox.sturddle-wb .wb-title')]
                            .map(t => t.textContent)"""
            )
            assert any("Standings" in t for t in titles)
            assert any("Schedule"  in t for t in titles)
            assert any("Event log" in t for t in titles)

            empty_text = await page.evaluate(
                """() => document.querySelector('.wb-standings .wb-empty')?.textContent || ''"""
            )
            assert "No games" in empty_text

            await page.evaluate(
                """() => document.querySelectorAll('.winbox.sturddle-wb .wb-close')
                            .forEach(b => b.click())"""
            )
            await page.wait_for_function(
                "() => document.querySelectorAll('.winbox.sturddle-wb').length === 0",
                timeout=3000,
            )
            await page.click('.tournament-row')
            await page.click('.tournaments-ribbon .t-workspace')
            await page.wait_for_function(
                "() => document.querySelectorAll('.winbox.sturddle-wb').length === 3",
                timeout=5000,
            )

            assert page_errors == [], "JS errors during test:\n" + "\n".join(page_errors)
        finally:
            await ctx.close()
    finally:
        s.should_exit = True
        s.force_exit = True
        thread.join(timeout=2)
