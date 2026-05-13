"""E2E: SPRT UI -- switch behavior, list badge, info dialog (Playwright/Chromium).

Skipped if Playwright/Chromium isn't installed.
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


def _ignore_wsproto_closed(args):
    # uvicorn calls connection.shutdown() on already-closed WS connections
    # during teardown; the resulting LocalProtocolError is harmless.
    try:
        from wsproto.utilities import LocalProtocolError
    except ImportError:
        pass
    else:
        if isinstance(args.exc_value, LocalProtocolError):
            return
    threading.__excepthook__(args)


def _make_server(tmp_path, monkeypatch, *, sprt_defaults=None):
    monkeypatch.setattr(
        FastchessRunner, "detect_binary",
        staticmethod(lambda configured: configured),
    )
    import uvicorn

    settings = Settings(token="test-token", auth_disabled=True)
    settings.pgn_dir = tmp_path / "pgn"
    settings.tournament_root = str(tmp_path / "tournaments")
    settings.tournament_fastchess_path = sys.executable
    if sprt_defaults:
        settings.tournament_sprt_defaults = sprt_defaults
    registry = EngineRegistry(path=tmp_path / "engines.json")
    registry.add(name="engine-A", path=sys.executable)
    registry.add(name="engine-B", path=sys.executable)
    app = create_app(settings=settings, engine_registry=registry)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws="wsproto")
    s = uvicorn.Server(config)
    prev_excepthook = threading.excepthook
    threading.excepthook = _ignore_wsproto_closed
    thread = threading.Thread(target=s.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline and not s.started:
        time.sleep(0.05)
    return s, thread, port, app, prev_excepthook


async def _nav_to_tournaments(page, base):
    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective", timeout=5000)
    await page.click('button[data-perspective="engines"]')
    await page.click('#engines-perspective wa-tab[panel="tournaments"]')
    await page.wait_for_selector(".tournaments-panel", timeout=5000)


async def _open_settings_tournament_tab(page):
    """Open Settings dialog and click the Tournament tab."""
    await page.click("#settings-btn")
    await page.wait_for_function(
        """() => !!document.querySelector('wa-dialog wa-tab[panel="tournament"]')""",
        timeout=5000,
    )
    await page.click('wa-dialog wa-tab[panel="tournament"]')
    # Wait for template form's SPRT switch to be present.
    await page.wait_for_selector("wa-switch[data-key='sprt']", timeout=5000)


@pytest.mark.asyncio
async def test_sprt_switch_disables_rounds_and_type(tmp_path, monkeypatch, browser):
    """Toggling the SPRT switch on must disable the Rounds input and
    Tournament Type select; toggling off re-enables them."""
    if browser is None:
        pytest.skip("chromium not installed")

    s, thread, port, app, prev_hook = _make_server(tmp_path, monkeypatch)
    base = f"http://127.0.0.1:{port}"
    try:
        ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
        page = await ctx.new_page()
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on("console", lambda msg: page_errors.append(
            f"console.{msg.type}: {msg.text}"
        ) if msg.type == "error" else None)
        try:
            await page.goto(base + "/")
            await page.wait_for_selector("#play-perspective", timeout=5000)
            await _open_settings_tournament_tab(page)

            # Both should be enabled before toggling SPRT on.
            initial = await page.evaluate("""() => ({
                rounds_disabled: document.querySelector('wa-input[data-key="rounds"]').hasAttribute('disabled'),
                type_disabled: document.querySelector('wa-select[data-key="tournament_type"]').hasAttribute('disabled'),
            })""")
            assert initial["rounds_disabled"] is False
            assert initial["type_disabled"] is False

            # Toggle SPRT on via JS (wa-switch checked + change event).
            await page.evaluate("""() => {
                const sw = document.querySelector('wa-switch[data-key="sprt"]');
                sw.checked = true;
                sw.dispatchEvent(new Event('change', { bubbles: true }));
            }""")
            await page.wait_for_function(
                """() => document.querySelector('wa-input[data-key="rounds"]').hasAttribute('disabled')""",
                timeout=3000,
            )
            on_state = await page.evaluate("""() => ({
                rounds_disabled: document.querySelector('wa-input[data-key="rounds"]').hasAttribute('disabled'),
                type_disabled: document.querySelector('wa-select[data-key="tournament_type"]').hasAttribute('disabled'),
            })""")
            assert on_state["rounds_disabled"] is True
            assert on_state["type_disabled"] is True

            # Toggle SPRT off.
            await page.evaluate("""() => {
                const sw = document.querySelector('wa-switch[data-key="sprt"]');
                sw.checked = false;
                sw.dispatchEvent(new Event('change', { bubbles: true }));
            }""")
            await page.wait_for_function(
                """() => !document.querySelector('wa-input[data-key="rounds"]').hasAttribute('disabled')""",
                timeout=3000,
            )
            off_state = await page.evaluate("""() => ({
                rounds_disabled: document.querySelector('wa-input[data-key="rounds"]').hasAttribute('disabled'),
                type_disabled: document.querySelector('wa-select[data-key="tournament_type"]').hasAttribute('disabled'),
            })""")
            assert off_state["rounds_disabled"] is False
            assert off_state["type_disabled"] is False

            assert page_errors == [], "JS errors:\n" + "\n".join(page_errors)
        finally:
            await ctx.close()
    finally:
        s.should_exit = True
        s.force_exit = True
        thread.join(timeout=2)
        threading.excepthook = prev_hook


@pytest.mark.asyncio
async def test_sprt_badge_shown_in_tournament_list(tmp_path, monkeypatch, browser):
    """A tournament created with sprt=True in its template shows the
    SPRT badge in the tournament list row."""
    if browser is None:
        pytest.skip("chromium not installed")

    sprt_defaults = {"elo0": 0, "elo1": 10, "alpha": 0.05, "beta": 0.05, "model": "normalized"}
    s, thread, port, app, prev_hook = _make_server(tmp_path, monkeypatch, sprt_defaults=sprt_defaults)
    base = f"http://127.0.0.1:{port}"

    # Pre-seed a tournament with resolved SPRT params (as the API would store).
    app.state.tournament_store.create(
        name="sprt-test",
        template={"tc": "5+0.05", "sprt": sprt_defaults},
        engines=[{"name": "A", "cmd": "/bin/A"}, {"name": "B", "cmd": "/bin/B"}],
    )

    try:
        ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
        page = await ctx.new_page()
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on("console", lambda msg: page_errors.append(
            f"console.{msg.type}: {msg.text}"
        ) if msg.type == "error" else None)
        try:
            await _nav_to_tournaments(page, base)
            await page.wait_for_selector(".tournament-row", timeout=5000)

            has_badge = await page.evaluate("""() => {
                const row = document.querySelector('.tournament-row');
                return !!row.querySelector('.tournament-sprt-badge');
            }""")
            assert has_badge is True

            assert page_errors == [], "JS errors:\n" + "\n".join(page_errors)
        finally:
            await ctx.close()
    finally:
        s.should_exit = True
        s.force_exit = True
        thread.join(timeout=2)
        threading.excepthook = prev_hook


@pytest.mark.asyncio
async def test_sprt_info_dialog_shows_params(tmp_path, monkeypatch, browser):
    """The Info dialog for an SPRT tournament shows 'unlimited (SPRT)'
    for Rounds and lists elo0/elo1/alpha/beta/model."""
    if browser is None:
        pytest.skip("chromium not installed")

    sprt_params = {"elo0": 0, "elo1": 10, "alpha": 0.05, "beta": 0.05, "model": "normalized"}
    s, thread, port, app, prev_hook = _make_server(tmp_path, monkeypatch, sprt_defaults=sprt_params)
    base = f"http://127.0.0.1:{port}"

    app.state.tournament_store.create(
        name="sprt-info",
        template={"tc": "5+0.05", "sprt": sprt_params},
        engines=[{"name": "A", "cmd": "/bin/A"}, {"name": "B", "cmd": "/bin/B"}],
    )

    try:
        ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
        page = await ctx.new_page()
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on("console", lambda msg: page_errors.append(
            f"console.{msg.type}: {msg.text}"
        ) if msg.type == "error" else None)
        try:
            await _nav_to_tournaments(page, base)
            await page.wait_for_selector(".tournament-row", timeout=5000)
            await page.click(".tournament-row")
            await page.click(".tournaments-ribbon .t-info")

            await page.wait_for_function(
                "() => !!document.querySelector('.tournament-info')",
                timeout=5000,
            )

            info_text = await page.evaluate("""() =>
                document.querySelector('.tournament-info')?.textContent || ''
            """)
            assert "unlimited" in info_text.lower()
            assert "elo0=0" in info_text
            assert "elo1=10" in info_text
            assert "alpha=0.05" in info_text
            assert "model=normalized" in info_text

            assert page_errors == [], "JS errors:\n" + "\n".join(page_errors)
        finally:
            await ctx.close()
    finally:
        s.should_exit = True
        s.force_exit = True
        thread.join(timeout=2)
        threading.excepthook = prev_hook


@pytest.mark.asyncio
async def test_sprt_settings_validation_marks_invalid_and_skips_persist(tmp_path, monkeypatch, browser):
    """On the Settings > SPRT tab, invalid inputs (elo0>=elo1, alpha<=0,
    etc.) get the .sprt-invalid class and the PUT is skipped. Fixing the
    fields clears the class and persistence resumes."""
    if browser is None:
        pytest.skip("chromium not installed")

    s, thread, port, app, prev_hook = _make_server(tmp_path, monkeypatch)
    base = f"http://127.0.0.1:{port}"
    try:
        ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
        page = await ctx.new_page()
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on("console", lambda msg: page_errors.append(
            f"console.{msg.type}: {msg.text}"
        ) if msg.type == "error" else None)

        put_count = {"n": 0}
        async def _on_request(req):
            if req.method == "PUT" and "tournament-settings" in req.url:
                put_count["n"] += 1
        page.on("request", _on_request)

        try:
            await page.goto(base + "/")
            await page.wait_for_selector("#play-perspective", timeout=5000)
            await page.click("#settings-btn")
            await page.wait_for_function(
                """() => !!document.querySelector('wa-dialog wa-tab[panel="sprt"]')""",
                timeout=5000,
            )
            await page.click('wa-dialog wa-tab[panel="sprt"]')
            await page.wait_for_selector('.sprt-settings-grid wa-input[data-key="elo0"]', timeout=5000)

            # Set elo0 > elo1 -- both fields should pick up .sprt-invalid.
            await page.evaluate("""() => {
                const e0 = document.querySelector('.sprt-settings-grid wa-input[data-key="elo0"]');
                const e1 = document.querySelector('.sprt-settings-grid wa-input[data-key="elo1"]');
                e0.value = "50"; e0.dispatchEvent(new Event('input', { bubbles: true }));
                e1.value = "10"; e1.dispatchEvent(new Event('input', { bubbles: true }));
            }""")
            await page.wait_for_function(
                """() => {
                    const e0 = document.querySelector('.sprt-settings-grid wa-input[data-key="elo0"]');
                    const e1 = document.querySelector('.sprt-settings-grid wa-input[data-key="elo1"]');
                    return e0.classList.contains('sprt-invalid') && e1.classList.contains('sprt-invalid');
                }""",
                timeout=3000,
            )

            # Now set alpha out of range -- it should also be flagged.
            await page.evaluate("""() => {
                const a = document.querySelector('.sprt-settings-grid wa-input[data-key="alpha"]');
                a.value = "0"; a.dispatchEvent(new Event('input', { bubbles: true }));
            }""")
            await page.wait_for_function(
                """() => document.querySelector('.sprt-settings-grid wa-input[data-key="alpha"]').classList.contains('sprt-invalid')""",
                timeout=3000,
            )

            # Wait past the debounce; no PUT should have fired while invalid.
            await page.wait_for_timeout(600)
            assert put_count["n"] == 0, f"expected no PUTs while invalid, got {put_count['n']}"

            # Fix all fields; invalid markers should clear and a PUT should fire.
            await page.evaluate("""() => {
                const e0 = document.querySelector('.sprt-settings-grid wa-input[data-key="elo0"]');
                const e1 = document.querySelector('.sprt-settings-grid wa-input[data-key="elo1"]');
                const a  = document.querySelector('.sprt-settings-grid wa-input[data-key="alpha"]');
                e0.value = "0";    e0.dispatchEvent(new Event('input', { bubbles: true }));
                e1.value = "10";   e1.dispatchEvent(new Event('input', { bubbles: true }));
                a.value  = "0.05"; a.dispatchEvent(new Event('input', { bubbles: true }));
            }""")
            await page.wait_for_function(
                """() => {
                    const all = document.querySelectorAll('.sprt-settings-grid wa-input');
                    return [...all].every(el => !el.classList.contains('sprt-invalid'));
                }""",
                timeout=3000,
            )
            await page.wait_for_timeout(600)  # debounce
            assert put_count["n"] >= 1, "expected at least one PUT after fields became valid"

            assert page_errors == [], "JS errors:\n" + "\n".join(page_errors)
        finally:
            await ctx.close()
    finally:
        s.should_exit = True
        s.force_exit = True
        thread.join(timeout=2)
        threading.excepthook = prev_hook
