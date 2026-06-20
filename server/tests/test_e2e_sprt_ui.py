"""E2E: SPRT UI -- chip popup behavior, list badge, info dialog (Playwright/Chromium).

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

from .conftest import (  # noqa: E402
    pin_arena_tournament_ux,
    run_uvicorn_subprocess,
    wait_perspective_ready,
)


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
    await pin_arena_tournament_ux(page)
    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)
    await page.click('button[data-perspective="engines"]')
    await page.wait_for_selector(".tournaments-panel")


async def _open_new_tournament_with_two_engines(page, base):
    """Open New Tournament and pick exactly 2 engines so SPRT is available."""
    await _nav_to_tournaments(page, base)
    await page.wait_for_function(
        "() => document.querySelector('.t-new') && !document.querySelector('.t-new').disabled",
    )
    await page.click(".t-new")
    await page.locator(".new-tournament-form").first.wait_for(state="attached")
    await page.locator(".ne-available-list .ne-item").first.wait_for(state="attached")
    # Add both seeded engines (engine-A, engine-B) so the roster is exactly 2.
    await page.locator(".ne-available-list .ne-item", has_text="engine-A").click()
    await page.locator(".ne-available-list .ne-item", has_text="engine-B").click(
        modifiers=["Control"]
    )
    await page.locator(".ne-add").click()
    # SPRT toggle enables once the roster is exactly 2.
    await page.wait_for_function(
        "() => { const t = document.querySelector('.sprt-toggle');"
        " return t && !t.disabled; }",
    )


async def _open_sprt_popup(page):
    """Click the SPRT gear and wait for the params popup grid."""
    await page.click(".sprt-gear")
    await page.wait_for_selector('.sprt-params-dialog .sprt-settings-grid wa-input[data-key="elo0"]')


@pytest.mark.asyncio
async def test_sprt_toggle_disables_rounds_and_type(tmp_path, make_page):
    """Flipping the SPRT toggle on must disable the Rounds input and
    Tournament Type select; off re-enables them."""
    with run_uvicorn_subprocess(env_overrides=_server_env(tmp_path)) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on("console", lambda msg: page_errors.append(
            f"console.{msg.type}: {msg.text}"
        ) if msg.type == "error" else None)
        await _open_new_tournament_with_two_engines(page, base)

        # Both enabled before turning SPRT on.
        initial = await page.evaluate("""() => ({
            rounds_disabled: document.querySelector('wa-input[data-key="rounds"]').hasAttribute('disabled'),
            type_disabled: document.querySelector('wa-select[data-key="tournament_type"]').hasAttribute('disabled'),
        })""")
        assert initial["rounds_disabled"] is False
        assert initial["type_disabled"] is False

        # Flip the toggle on.
        await page.evaluate("""() => {
            const t = document.querySelector('.sprt-toggle');
            t.checked = true;
            t.dispatchEvent(new Event('change', { bubbles: true }));
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

        # Flip the toggle off.
        await page.evaluate("""() => {
            const t = document.querySelector('.sprt-toggle');
            t.checked = false;
            t.dispatchEvent(new Event('change', { bubbles: true }));
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
async def test_sprt_create_omits_rounds_from_template(tmp_path, make_page):
    """A SPRT tournament must be created WITHOUT a rounds field -- fastchess
    self-terminates, so a stored rounds would cap the run (and mis-render a
    bogus N/rounds total in the list)."""
    with run_uvicorn_subprocess(env_overrides=_server_env(tmp_path)) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on("console", lambda msg: page_errors.append(
            f"console.{msg.type}: {msg.text}"
        ) if msg.type == "error" else None)
        await _open_new_tournament_with_two_engines(page, base)

        # Name + time control + flip SPRT on (wa-input/wa-switch via JS+events).
        await page.evaluate("""() => {
            const set = (sel, v) => {
                const el = document.querySelector(sel);
                el.value = v;
                el.dispatchEvent(new Event('input', { bubbles: true }));
            };
            set('.nt-name', 'sprt-create');
            set('wa-input[data-key="tc"]', '10+0.1');
            const t = document.querySelector('.sprt-toggle');
            t.checked = true;
            t.dispatchEvent(new Event('change', { bubbles: true }));
        }""")

        # Create button enables once name + 2 engines are set.
        await page.wait_for_function(
            """() => { const b = [...document.querySelectorAll('wa-button')]
                .find(el => el.textContent.trim() === 'Create');
                return b && !b.disabled; }""",
        )
        # The create POST carries the full template; assert it has sprt but no
        # rounds (fastchess self-terminates -- a stored rounds would cap it).
        async with page.expect_request(
            lambda r: r.method == "POST"
            and r.url.endswith("/api/tournaments"),
        ) as req_info:
            await page.evaluate(
                """() => [...document.querySelectorAll('wa-button')]
                    .find(el => el.textContent.trim() === 'Create').click()"""
            )
        template = (await req_info.value).post_data_json["template"]
        assert template.get("sprt"), f"sprt missing from template: {template}"
        assert "rounds" not in template, f"rounds leaked into SPRT template: {template}"

        assert page_errors == [], "JS errors:\n" + "\n".join(page_errors)


@pytest.mark.asyncio
async def test_sprt_badge_shown_in_tournament_list(tmp_path, make_page):
    """A tournament created with sprt=True in its template shows the
    SPRT badge in the tournament list row."""
    sprt_defaults = {"elo0": 0, "elo1": 10, "alpha": 0.05, "beta": 0.05}
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
    for Rounds and lists elo0/elo1/alpha/beta."""
    sprt_params = {"elo0": 0, "elo1": 10, "alpha": 0.05, "beta": 0.05}
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
        # Model choice was removed -- the SPRT line no longer carries it.
        assert "model=" not in info_text

        assert page_errors == [], "JS errors:\n" + "\n".join(page_errors)


@pytest.mark.asyncio
async def test_sprt_gear_popup_marks_invalid_params(tmp_path, make_page):
    """In the SPRT gear popup, invalid params (elo0>=elo1) get the
    .sprt-invalid class; fixing them clears the markers. Params commit on
    close and stay dormant -- the toggle (not the popup) drives on/off."""
    with run_uvicorn_subprocess(env_overrides=_server_env(tmp_path)) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on("console", lambda msg: page_errors.append(
            f"console.{msg.type}: {msg.text}"
        ) if msg.type == "error" else None)

        await _open_new_tournament_with_two_engines(page, base)
        await _open_sprt_popup(page)

        # elo0 > elo1 -- both fields flagged.
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

        # Fix elo0 -- markers clear.
        await page.evaluate("""() => {
            const e0 = document.querySelector('.sprt-settings-grid wa-input[data-key="elo0"]');
            e0.value = "0"; e0.dispatchEvent(new Event('input', { bubbles: true }));
        }""")
        await page.wait_for_function(
            """() => [...document.querySelectorAll('.sprt-settings-grid wa-input')]
                .every(el => !el.classList.contains('sprt-invalid'))""",
        )

        # Close the gear popup -- the toggle is untouched (SPRT stays off).
        await page.evaluate("""() => document.querySelector('.sprt-params-dialog').open = false""")
        await page.wait_for_function(
            "() => !document.querySelector('.sprt-params-dialog')",
        )
        toggle_on = await page.evaluate(
            "() => document.querySelector('.sprt-toggle').checked"
        )
        assert toggle_on is False

        assert page_errors == [], "JS errors:\n" + "\n".join(page_errors)
