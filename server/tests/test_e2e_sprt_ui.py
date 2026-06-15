"""E2E: SPRT UI -- switch behavior, list badge, info dialog (Playwright/Chromium).

Skipped if Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import json
import sys

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.engines import EngineRegistry  # noqa: E402
from sturddle_view.tournament.store import TournamentStore  # noqa: E402

from .conftest import run_uvicorn_subprocess, wait_perspective_ready  # noqa: E402


def _server_env(tmp_path, *, sprt_defaults=None):
    """Seed the engine registry on disk and return SV_* env for an
    out-of-process server. tournament_fastchess_path is sys.executable (a
    real file detect_binary echoes back); fastchess is never spawned."""
    registry = EngineRegistry(path=tmp_path / "engines.json")
    registry.add(name="engine-A", path=sys.executable)
    registry.add(name="engine-B", path=sys.executable)
    env = {
        "SV_PGN_DIR": str(tmp_path / "pgn"),
        "SV_TOURNAMENT_ROOT": str(tmp_path / "tournaments"),
        "SV_ENGINE_REGISTRY_PATH": str(tmp_path / "engines.json"),
        "SV_SETTINGS_FILE": str(tmp_path / "settings.json"),
        "SV_GAME_STATE_PATH": str(tmp_path / "current_game.json"),
        "SV_IMPORTS_DIR": str(tmp_path / "imports"),
        "SV_TOURNAMENT_FASTCHESS_PATH": sys.executable,
    }
    if sprt_defaults:
        env["SV_TOURNAMENT_SPRT_DEFAULTS"] = json.dumps(sprt_defaults)
    return env


async def _nav_to_tournaments(page, base):
    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)
    await page.click('button[data-perspective="engines"]')
    await page.wait_for_selector(".tournaments-panel")


async def _open_settings_tournament_tab(page):
    """Open Settings dialog and click the Tournament tab."""
    await page.click("#settings-btn")
    await page.wait_for_function(
        """() => !!document.querySelector('wa-dialog wa-tab[panel="tournament"]')""",
    )
    await page.click('wa-dialog wa-tab[panel="tournament"]')
    # Wait for template form's SPRT switch to be present.
    await page.wait_for_selector("wa-switch[data-key='sprt']")


@pytest.mark.asyncio
async def test_sprt_switch_disables_rounds_and_type(tmp_path, make_page):
    """Toggling the SPRT switch on must disable the Rounds input and
    Tournament Type select; toggling off re-enables them."""
    with run_uvicorn_subprocess(env_overrides=_server_env(tmp_path)) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on("console", lambda msg: page_errors.append(
            f"console.{msg.type}: {msg.text}"
        ) if msg.type == "error" else None)
        await page.goto(base + "/")
        await page.wait_for_selector("#play-perspective")
        await wait_perspective_ready(page)
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
        )
        off_state = await page.evaluate("""() => ({
            rounds_disabled: document.querySelector('wa-input[data-key="rounds"]').hasAttribute('disabled'),
            type_disabled: document.querySelector('wa-select[data-key="tournament_type"]').hasAttribute('disabled'),
        })""")
        assert off_state["rounds_disabled"] is False
        assert off_state["type_disabled"] is False

        assert page_errors == [], "JS errors:\n" + "\n".join(page_errors)


@pytest.mark.asyncio
async def test_sprt_badge_shown_in_tournament_list(tmp_path, make_page):
    """A tournament created with sprt=True in its template shows the
    SPRT badge in the tournament list row."""
    sprt_defaults = {"elo0": 0, "elo1": 10, "alpha": 0.05, "beta": 0.05, "model": "normalized"}
    env = _server_env(tmp_path, sprt_defaults=sprt_defaults)

    # Pre-seed a tournament with resolved SPRT params (as the API would store).
    TournamentStore(tmp_path / "tournaments").create(
        name="sprt-test",
        template={"tc": "5+0.05", "sprt": sprt_defaults},
        engines=[{"name": "A", "cmd": "/bin/A"}, {"name": "B", "cmd": "/bin/B"}],
    )

    with run_uvicorn_subprocess(env_overrides=env) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on("console", lambda msg: page_errors.append(
            f"console.{msg.type}: {msg.text}"
        ) if msg.type == "error" else None)
        await _nav_to_tournaments(page, base)
        await page.wait_for_selector(".tournament-row")

        has_badge = await page.evaluate("""() => {
            const row = document.querySelector('.tournament-row');
            return !!row.querySelector('.tournament-status--sprt');
        }""")
        assert has_badge is True

        assert page_errors == [], "JS errors:\n" + "\n".join(page_errors)


@pytest.mark.asyncio
async def test_sprt_info_dialog_shows_params(tmp_path, make_page):
    """The Info dialog for an SPRT tournament shows 'unlimited (SPRT)'
    for Rounds and lists elo0/elo1/alpha/beta/model."""
    sprt_params = {"elo0": 0, "elo1": 10, "alpha": 0.05, "beta": 0.05, "model": "normalized"}
    env = _server_env(tmp_path, sprt_defaults=sprt_params)

    TournamentStore(tmp_path / "tournaments").create(
        name="sprt-info",
        template={"tc": "5+0.05", "sprt": sprt_params},
        engines=[{"name": "A", "cmd": "/bin/A"}, {"name": "B", "cmd": "/bin/B"}],
    )

    with run_uvicorn_subprocess(env_overrides=env) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on("console", lambda msg: page_errors.append(
            f"console.{msg.type}: {msg.text}"
        ) if msg.type == "error" else None)
        await _nav_to_tournaments(page, base)
        await page.wait_for_selector(".tournament-row")
        await page.click(".tournament-row")
        await page.click(".tournaments-ribbon .t-info")

        await page.wait_for_function(
            "() => !!document.querySelector('.tournament-info')",
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


@pytest.mark.asyncio
async def test_sprt_settings_validation_marks_invalid_and_skips_persist(tmp_path, make_page):
    """On the Settings > SPRT tab, invalid inputs (elo0>=elo1, alpha<=0,
    etc.) get the .sprt-invalid class and the PUT is skipped. Fixing the
    fields clears the class and persistence resumes."""
    with run_uvicorn_subprocess(env_overrides=_server_env(tmp_path)) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
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

        await page.goto(base + "/")
        await page.wait_for_selector("#play-perspective")
        await wait_perspective_ready(page)
        await page.click("#settings-btn")
        await page.wait_for_function(
            """() => !!document.querySelector('wa-dialog wa-tab[panel="sprt"]')""",
        )
        await page.click('wa-dialog wa-tab[panel="sprt"]')
        await page.wait_for_selector('.sprt-settings-grid wa-input[data-key="elo0"]')

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
        )

        # Now set alpha out of range -- it should also be flagged.
        await page.evaluate("""() => {
            const a = document.querySelector('.sprt-settings-grid wa-input[data-key="alpha"]');
            a.value = "0"; a.dispatchEvent(new Event('input', { bubbles: true }));
        }""")
        await page.wait_for_function(
            """() => document.querySelector('.sprt-settings-grid wa-input[data-key="alpha"]').classList.contains('sprt-invalid')""",
        )

        # Fix all fields; invalid markers should clear and a PUT should fire.
        # ``expect_request`` is the real signal -- it resolves on the next
        # matching PUT, which is the first one (any PUT before would have
        # been emitted during the invalid window).
        async with page.expect_request(
            lambda r: r.method == "PUT" and "tournament-settings" in r.url,
        ):
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
                }"""
            )
        # Exactly one PUT must have fired in total: the post-fix one. If
        # the debounce had fired during the invalid window the counter
        # would be 2.
        assert put_count["n"] == 1, (
            f"expected exactly one PUT (post-fix); got {put_count['n']}"
        )

        assert page_errors == [], "JS errors:\n" + "\n".join(page_errors)
