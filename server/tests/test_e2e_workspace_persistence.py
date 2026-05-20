"""E2E: Tournament workspace persistence rules (docs/tourney-workspace.md).

Each test drives a Chromium browser through the workspace lifecycle and
asserts the rules in the spec doc. Skipped if Playwright is missing.
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


WORKSPACE_KEY_PREFIX = "sturddle:workspace:"
ROW_SEL = ".tournament-row"
WB_SEL = ".winbox.sturddle-wb"
RIBBON_WS = ".tournaments-ribbon .t-workspace"


def _build_app(tmp_path, monkeypatch, *, tournaments=("A", "B")):
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
    for name in tournaments:
        app.state.tournament_store.create(
            name=name,
            template={"tc": "10+0.1"},
            engines=[{"name": "A", "cmd": "/bin/A"}, {"name": "B", "cmd": "/bin/B"}],
        )
    # Capture the server's running event loop on app.state so tests can
    # publish onto the EventBus via run_coroutine_threadsafe.
    @app.middleware("http")
    async def _capture_loop(request, call_next):
        import asyncio as _asyncio
        if not hasattr(request.app.state, "loop"):
            request.app.state.loop = _asyncio.get_running_loop()
        return await call_next(request)

    return app


@pytest.fixture
def server(tmp_path, monkeypatch):
    app = _build_app(tmp_path, monkeypatch)
    with run_uvicorn(app) as (base, _s):
        yield base


@pytest.fixture
def server_app(tmp_path, monkeypatch):
    """Like ``server`` but also exposes the FastAPI app so tests can pre-seed
    tournament status (and publish into the event bus for transition tests).
    """
    app = _build_app(tmp_path, monkeypatch)
    with run_uvicorn(app) as (base, _s):
        yield base, app


async def _goto_app(page, base):
    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")
    await page.click('button[data-perspective="engines"]')
    await page.wait_for_selector(ROW_SEL)


async def _row_ids(page):
    return await page.evaluate(
        "() => [...document.querySelectorAll('.tournament-row')]"
        ".map(r => r.dataset.id)"
    )


async def _click_row(page, idx):
    await page.evaluate(
        "(i) => document.querySelectorAll('.tournament-row')[i].click()", idx
    )


async def _open_workspace_via_ribbon(page):
    await page.click(RIBBON_WS)


async def _wb_count(page):
    return await page.evaluate(f"() => document.querySelectorAll('{WB_SEL}').length")


async def _wait_wb_count(page, n):
    await page.wait_for_function(
        f"() => document.querySelectorAll('{WB_SEL}').length === {n}",
    )


async def _wb_titles(page):
    return await page.evaluate(
        f"() => [...document.querySelectorAll('{WB_SEL} .wb-title')]"
        ".map(t => t.textContent)"
    )


async def _close_all_via_menu(page):
    # Click the underlying button directly; bypasses CSS hover gating.
    await page.evaluate("() => document.querySelector('.tmb-closeall').click()")


async def _open_extra_window(page, key):
    """Open a system window via the menu item (key in {standings,schedule,engines,log})."""
    await page.evaluate(
        f"() => document.querySelector('.tmb-sys-{key}').click()"
    )


async def _x_close_window(page, title_substring):
    await page.evaluate(
        """(needle) => {
            const wbs = [...document.querySelectorAll('.winbox.sturddle-wb')];
            const wb = wbs.find(w => w.querySelector('.wb-title')?.textContent.includes(needle));
            wb?.querySelector('.wb-close')?.click();
        }""",
        title_substring,
    )


async def _x_close_all_windows(page):
    await page.evaluate(
        f"() => document.querySelectorAll('{WB_SEL} .wb-close').forEach(b => b.click())"
    )


async def _saved_state(page, tournament_id):
    return await page.evaluate(
        "(k) => { const v = localStorage.getItem(k); return v ? JSON.parse(v) : null; }",
        WORKSPACE_KEY_PREFIX + tournament_id,
    )


async def _set_saved_state(page, tournament_id, state):
    await page.evaluate(
        "([k, v]) => localStorage.setItem(k, JSON.stringify(v))",
        [WORKSPACE_KEY_PREFIX + tournament_id, state],
    )


def _new_context_args():
    return {"viewport": {"width": 1400, "height": 900}}


async def _new_page(make_page):
    ctx, page = await make_page(**_new_context_args())
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(f"console.{msg.type}: {msg.text}")
            if msg.type == "error" else None)
    return ctx, page, errors


def _assert_no_errors(errors):
    benign = ("Failed to load resource",)
    real = [e for e in errors if not any(b in e for b in benign)]
    assert real == [], "JS errors:\n" + "\n".join(real)


# ---- Ribbon "Open Workspace" ------------------------------------------------

@pytest.mark.asyncio
async def test_TR1_fresh_open_workspace_default(server, make_page):
    _ctx, page, errors = await _new_page(make_page)
    await _goto_app(page, server)
    await _click_row(page, 0)
    await _open_workspace_via_ribbon(page)
    # idle tournament -> only Standings auto-opens.
    await _wait_wb_count(page, 1)
    titles = await _wb_titles(page)
    assert any("Standings" in t for t in titles)
    _assert_no_errors(errors)


@pytest.mark.asyncio
async def test_TR2_open_workspace_restores_saved_set(server, make_page):
    _ctx, page, errors = await _new_page(make_page)
    await _goto_app(page, server)
    ids = await _row_ids(page)
    # Pre-seed a saved state with Standings + Event Log open.
    await _set_saved_state(page, ids[0], {
        "_closed": False,
        "standings": {"open": True, "x": "100px", "y": "100px",
                      "width": "500px", "height": "300px",
                      "min": False, "max": False, "z": 1},
        "log": {"open": True, "x": "200px", "y": "450px",
                "width": "600px", "height": "200px",
                "min": False, "max": False, "z": 2},
        "schedule": {"open": False},
        "engines": {"open": False},
    })
    await _click_row(page, 0)
    await _open_workspace_via_ribbon(page)
    await _wait_wb_count(page, 2)
    titles = await _wb_titles(page)
    assert any("Standings" in t for t in titles)
    assert any("Event log" in t for t in titles)
    _assert_no_errors(errors)


@pytest.mark.asyncio
async def test_TR3_close_all_then_open_workspace_restores(server, make_page):
    _ctx, page, errors = await _new_page(make_page)
    await _goto_app(page, server)
    await _click_row(page, 0)
    await _open_workspace_via_ribbon(page)
    await _wait_wb_count(page, 1)
    await _open_extra_window(page, "log")
    await _wait_wb_count(page, 2)
    await _close_all_via_menu(page)
    await _wait_wb_count(page, 0)
    await _open_workspace_via_ribbon(page)
    await _wait_wb_count(page, 2)
    _assert_no_errors(errors)


@pytest.mark.asyncio
async def test_TR5_x_close_all_then_open_workspace_uses_default(server, make_page):
    _ctx, page, errors = await _new_page(make_page)
    await _goto_app(page, server)
    await _click_row(page, 0)
    await _open_workspace_via_ribbon(page)
    await _wait_wb_count(page, 1)
    await _open_extra_window(page, "log")
    await _wait_wb_count(page, 2)
    await _x_close_all_windows(page)
    await _wait_wb_count(page, 0)
    await _open_workspace_via_ribbon(page)
    # Default: idle tournament -> only Standings.
    await _wait_wb_count(page, 1)
    titles = await _wb_titles(page)
    assert any("Standings" in t for t in titles)
    _assert_no_errors(errors)


@pytest.mark.asyncio
async def test_TR6_x_close_one_then_close_all_excludes_x_closed(server, make_page):
    _ctx, page, errors = await _new_page(make_page)
    await _goto_app(page, server)
    await _click_row(page, 0)
    await _open_workspace_via_ribbon(page)
    await _wait_wb_count(page, 1)
    await _open_extra_window(page, "log")
    await _wait_wb_count(page, 2)
    # X-close Event log; only Standings should remain.
    await _x_close_window(page, "Event log")
    await _wait_wb_count(page, 1)
    await _close_all_via_menu(page)
    await _wait_wb_count(page, 0)
    await _open_workspace_via_ribbon(page)
    await _wait_wb_count(page, 1)
    titles = await _wb_titles(page)
    assert any("Standings" in t for t in titles)
    assert not any("Event log" in t for t in titles)
    _assert_no_errors(errors)


# ---- Navigation -------------------------------------------------------------

@pytest.mark.asyncio
async def test_TN1_no_workspace_click_fresh_does_not_open(server, make_page):
    _ctx, page, errors = await _new_page(make_page)
    await _goto_app(page, server)
    await _click_row(page, 0)
    # Brief settle then assert nothing opened.
    await page.wait_for_timeout(200)
    assert await _wb_count(page) == 0
    _assert_no_errors(errors)



@pytest.mark.asyncio
async def test_TN4_close_all_then_navigate_back_does_not_open(server, make_page):
    _ctx, page, errors = await _new_page(make_page)
    await _goto_app(page, server)
    await _click_row(page, 0)
    await _open_workspace_via_ribbon(page)
    await _wait_wb_count(page, 1)
    await _close_all_via_menu(page)
    await _wait_wb_count(page, 0)
    # Navigate to B; A torn down so hadWorkspace=false; B has no saved state -> no open.
    await _click_row(page, 1)
    await page.wait_for_timeout(200)
    assert await _wb_count(page) == 0
    # Navigate back to A; A has _closed=true -> no open.
    await _click_row(page, 0)
    await page.wait_for_timeout(200)
    assert await _wb_count(page) == 0
    _assert_no_errors(errors)


@pytest.mark.asyncio
async def test_TN5_navigate_to_b_with_saved_state_restores(server, make_page):
    _ctx, page, errors = await _new_page(make_page)
    await _goto_app(page, server)
    ids = await _row_ids(page)
    # Pre-seed B with saved Standings + Engines.
    await _set_saved_state(page, ids[1], {
        "_closed": False,
        "standings": {"open": True, "x": "10px", "y": "10px",
                      "width": "400px", "height": "300px",
                      "min": False, "max": False, "z": 1},
        "engines": {"open": True, "x": "420px", "y": "10px",
                    "width": "300px", "height": "300px",
                    "min": False, "max": False, "z": 2},
        "schedule": {"open": False},
        "log": {"open": False},
    })
    # Open A first to get into "workspace mode".
    await _click_row(page, 0)
    await _open_workspace_via_ribbon(page)
    await _wait_wb_count(page, 1)
    await _click_row(page, 1)
    await _wait_wb_count(page, 2)
    titles = await _wb_titles(page)
    assert any("Standings" in t for t in titles)
    assert any("Engine Instances" in t for t in titles)
    _assert_no_errors(errors)


@pytest.mark.asyncio
async def test_TN6_navigate_to_b_with_closed_flag_does_not_open(server, make_page):
    _ctx, page, errors = await _new_page(make_page)
    await _goto_app(page, server)
    ids = await _row_ids(page)
    await _set_saved_state(page, ids[1], {
        "_closed": True,
        "standings": {"open": True, "x": "10px", "y": "10px",
                      "width": "400px", "height": "300px",
                      "min": False, "max": False, "z": 1},
        "schedule": {"open": False},
        "engines": {"open": False},
        "log": {"open": False},
    })
    await _click_row(page, 0)
    await _open_workspace_via_ribbon(page)
    await _wait_wb_count(page, 1)
    await _click_row(page, 1)
    # Close All on B is gated by _closed=true; A's workspace was closed
    # by navigation; so no windows should be open after settle.
    await page.wait_for_timeout(300)
    assert await _wb_count(page) == 0
    _assert_no_errors(errors)


# ---- State integrity --------------------------------------------------------

@pytest.mark.asyncio
async def test_TS3_navigate_away_restores_geometry(server, make_page):
    _ctx, page, errors = await _new_page(make_page)
    await _goto_app(page, server)
    await _click_row(page, 0)
    await _open_workspace_via_ribbon(page)
    await _wait_wb_count(page, 1)
    # Pick a y safely below the menubar/ribbon clamp.
    await page.evaluate(
        """() => {
            const wb = document.querySelector('.winbox.sturddle-wb').winbox;
            wb.move(180, 320).resize(640, 380);
        }"""
    )
    await _click_row(page, 1)
    await _click_row(page, 0)
    await _wait_wb_count(page, 1)
    geom = await page.evaluate(
        """() => {
            const wb = document.querySelector('.winbox.sturddle-wb').winbox;
            return { x: wb.x, y: wb.y, w: wb.width, h: wb.height };
        }"""
    )
    assert geom["x"] == 180 and geom["y"] == 320
    assert geom["w"] == 640 and geom["h"] == 380
    _assert_no_errors(errors)


@pytest.mark.asyncio
async def test_TS1_minimized_state_restored(server, make_page):
    _ctx, page, errors = await _new_page(make_page)
    await _goto_app(page, server)
    await _click_row(page, 0)
    await _open_workspace_via_ribbon(page)
    await _wait_wb_count(page, 1)
    await page.evaluate(
        "() => document.querySelector('.winbox.sturddle-wb').winbox.minimize()"
    )
    await _click_row(page, 1)
    await _click_row(page, 0)
    await _wait_wb_count(page, 1)
    is_min = await page.evaluate(
        "() => !!document.querySelector('.winbox.sturddle-wb').winbox.min"
    )
    assert is_min is True
    _assert_no_errors(errors)


@pytest.mark.asyncio
async def test_TS0_zorder_topmost_restored(server, make_page):
    _ctx, page, errors = await _new_page(make_page)
    await _goto_app(page, server)
    await _click_row(page, 0)
    await _open_workspace_via_ribbon(page)
    await _wait_wb_count(page, 1)
    await _open_extra_window(page, "log")
    await _wait_wb_count(page, 2)
    # Click Standings to make it the topmost.
    await page.evaluate(
        """() => {
            const wbs = [...document.querySelectorAll('.winbox.sturddle-wb')];
            const standings = wbs.find(w => w.querySelector('.wb-title')
                .textContent.includes('Standings'));
            standings.winbox.focus();
        }"""
    )
    await _click_row(page, 1)
    await _click_row(page, 0)
    await _wait_wb_count(page, 2)
    topmost_title = await page.evaluate(
        """() => {
            const wbs = [...document.querySelectorAll('.winbox.sturddle-wb')];
            let top = wbs[0], topZ = parseInt(getComputedStyle(top).zIndex || '0', 10);
            for (const w of wbs) {
                const z = parseInt(getComputedStyle(w).zIndex || '0', 10);
                if (z > topZ) { top = w; topZ = z; }
            }
            return top.querySelector('.wb-title').textContent;
        }"""
    )
    assert "Standings" in topmost_title
    _assert_no_errors(errors)


# ---- Regression guard -------------------------------------------------------

@pytest.mark.asyncio
async def test_TBUG1_x_close_one_then_close_all_then_open(server, make_page):
    """X-closed window must NOT reappear; visible window MUST."""
    _ctx, page, errors = await _new_page(make_page)
    await _goto_app(page, server)
    await _click_row(page, 0)
    await _open_workspace_via_ribbon(page)
    await _wait_wb_count(page, 1)
    await _open_extra_window(page, "log")
    await _wait_wb_count(page, 2)
    await _x_close_window(page, "Event log")
    await _wait_wb_count(page, 1)
    await _close_all_via_menu(page)
    await _wait_wb_count(page, 0)
    await _open_workspace_via_ribbon(page)
    await _wait_wb_count(page, 1)
    titles = await _wb_titles(page)
    assert any("Standings" in t for t in titles)
    assert not any("Event log" in t for t in titles)
    _assert_no_errors(errors)


@pytest.mark.asyncio
async def test_TBUG2_close_all_navigate_away_back_no_reopen(server, make_page):
    _ctx, page, errors = await _new_page(make_page)
    await _goto_app(page, server)
    await _click_row(page, 0)
    await _open_workspace_via_ribbon(page)
    await _wait_wb_count(page, 1)
    await _close_all_via_menu(page)
    await _wait_wb_count(page, 0)
    await _click_row(page, 1)
    await page.wait_for_timeout(200)
    await _click_row(page, 0)
    await page.wait_for_timeout(300)
    assert await _wb_count(page) == 0
    _assert_no_errors(errors)


@pytest.mark.asyncio
async def test_TBUG3_no_prior_save_close_all_open_restores_what_was_open(server, make_page):
    _ctx, page, errors = await _new_page(make_page)
    await _goto_app(page, server)
    await _click_row(page, 0)
    await _open_workspace_via_ribbon(page)
    await _wait_wb_count(page, 1)
    await _open_extra_window(page, "engines")
    await _wait_wb_count(page, 2)
    await _close_all_via_menu(page)
    await _wait_wb_count(page, 0)
    await _open_workspace_via_ribbon(page)
    await _wait_wb_count(page, 2)
    titles = await _wb_titles(page)
    assert any("Standings" in t for t in titles)
    assert any("Engine Instances" in t for t in titles)
    _assert_no_errors(errors)


# ---- Status-driven defaults (no saved state) -------------------------------
# Covers the gap from the rules audit: ribbon "Open Workspace" against a
# tournament with no saved state, for each non-idle status.

@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["stopped", "done"])
async def test_default_open_terminal_status_only_standings(server_app, make_page, status):
    """STOPPED / DONE with no replayed events: log is empty and tournament
    is not RUNNING -> default logic opens Standings only."""
    base, app = server_app
    tids = [t.id for t in app.state.tournament_store.list()]
    app.state.tournament_store.update_status(tids[0], status)

    _ctx, page, errors = await _new_page(make_page)
    await _goto_app(page, base)
    await _click_row(page, 0)
    await _open_workspace_via_ribbon(page)
    await _wait_wb_count(page, 1)
    titles = await _wb_titles(page)
    assert any("Standings" in t for t in titles)
    assert not any("Live Games" in t for t in titles)
    assert not any("Event log" in t for t in titles)
    _assert_no_errors(errors)


@pytest.mark.asyncio
async def test_default_open_failed_status_surfaces_error_banner(server_app, make_page):
    """FAILED tournament: default opens Standings only (no log entries to
    trigger the log auto-open). Manually opening the Event Log via the
    Window menu must surface the persisted last_error banner."""
    base, app = server_app
    tids = [t.id for t in app.state.tournament_store.list()]
    app.state.tournament_store.update_status(
        tids[0],
        "failed",
        last_error={"rc": 137, "stderr_tail": ["fatal: engine crashed"]},
    )

    _ctx, page, errors = await _new_page(make_page)
    await _goto_app(page, base)
    await _click_row(page, 0)
    await _open_workspace_via_ribbon(page)
    await _wait_wb_count(page, 1)
    titles = await _wb_titles(page)
    assert any("Standings" in t for t in titles)
    # Open the Event Log via the menu and wait for the banner to render.
    await _open_extra_window(page, "log")
    await _wait_wb_count(page, 2)
    await page.wait_for_function(
        """() => {
            const b = document.querySelector('.wb-error-banner');
            return b && !b.hidden && /rc=137/.test(b.textContent);
        }""",
    )
    _assert_no_errors(errors)


@pytest.mark.asyncio
async def test_default_open_running_opens_three_windows(server_app, make_page):
    """RUNNING tournament with no saved state: Standings + Live Games +
    Event Log all auto-open (Live Games and Event Log because RUNNING)."""
    base, app = server_app
    tids = [t.id for t in app.state.tournament_store.list()]
    app.state.tournament_store.update_status(tids[0], "running")

    _ctx, page, errors = await _new_page(make_page)
    await _goto_app(page, base)
    await _click_row(page, 0)
    await _open_workspace_via_ribbon(page)
    await _wait_wb_count(page, 3)
    titles = await _wb_titles(page)
    assert any("Standings" in t for t in titles)
    assert any("Live Games" in t for t in titles)
    assert any("Event log" in t for t in titles)
    _assert_no_errors(errors)


@pytest.mark.asyncio
async def test_idle_to_running_transition_auto_opens_live_games(server_app, make_page):
    """Workspace open on an idle tournament with no saved state: when a
    tournament_status RUNNING event fires, Live Games window auto-opens."""
    import asyncio
    from sturddle_view.events import Event

    base, app = server_app
    tids = [t.id for t in app.state.tournament_store.list()]

    _ctx, page, errors = await _new_page(make_page)
    await _goto_app(page, base)
    await _click_row(page, 0)
    await _open_workspace_via_ribbon(page)
    await _wait_wb_count(page, 1)
    # Publish a status change onto the live event bus -- the WS pipes
    # it to the browser and the workspace's pushEvent handler opens
    # Live Games when payload.status === "running".
    loop = app.state.loop  # captured by the loop-capture middleware
    fut = asyncio.run_coroutine_threadsafe(
        app.state.event_bus.publish(Event(
            kind="tournament_status",
            payload={"tournament_id": tids[0], "status": "running", "_seq": 9999},
        )),
        loop,
    )
    fut.result(timeout=3)
    await page.wait_for_function(
        """() => [...document.querySelectorAll('.winbox.sturddle-wb .wb-title')]
                   .some(t => /Live Games/.test(t.textContent))""",
    )
    _assert_no_errors(errors)


# ---- Frozen-window rehydration on workspace reopen -------------------------
# Snapshot entries with `resolved` are reopened as frozen windows that fetch
# the PGN; unresolved entries for ended games surface a toast. The PGN file
# is the canonical source for engine names + final position.

_FROZEN_ONE_GAME_PGN = """[Event "g1"]
[White "engine-A"]
[Black "engine-B"]
[Result "1-0"]
[Termination "checkmate"]

1. f3 e5 2. g4 Qh4# 1-0
"""


def _seed_pgn(app, tournament_id: str, pgn_text: str) -> None:
    pgn_path = app.state.tournament_store.pgn_path(tournament_id)
    pgn_path.parent.mkdir(parents=True, exist_ok=True)
    pgn_path.write_text(pgn_text, encoding="utf-8")


@pytest.mark.asyncio
async def test_TFROZEN1_resolved_snapshot_rehydrates_frozen_window(server_app, make_page):
    """Snapshot with a resolved live entry -> reopen creates a frozen window
    that fetches the PGN and paints the result banner."""
    base, app = server_app
    tids = [t.id for t in app.state.tournament_store.list()]
    tid = tids[0]
    _seed_pgn(app, tid, _FROZEN_ONE_GAME_PGN)

    _ctx, page, errors = await _new_page(make_page)
    await _goto_app(page, base)
    # Pre-seed: standings open + one resolved live window for a
    # synthetic pair_id, gameN=1 in the PGN above.
    await _set_saved_state(page, tid, {
        "_closed": False,
        "standings": {"open": True, "x": "100px", "y": "100px",
                      "width": "500px", "height": "300px",
                      "min": False, "max": False, "z": 1},
        "schedule": {"open": False},
        "engines": {"open": False},
        "log": {"open": False},
        "live": [{
            "proxyId": "proxy-a", "gameId": "pair-1",
            "label": "engine-A vs engine-B", "engineName": "engine-A",
            "x": "200px", "y": "200px", "width": "320px", "height": "560px",
            "min": False, "max": False, "z": 2,
            "resolved": {"gameN": 1, "result": "1-0", "termination": "checkmate"},
        }],
    })
    await _click_row(page, 0)
    await _open_workspace_via_ribbon(page)
    # Two windows: Standings + the frozen game window.
    await _wait_wb_count(page, 2)
    # Frozen variant class is added by openFrozenGameWindow.
    await page.wait_for_selector(".winbox.sturddle-wb-live-frozen")
    # Banner painted with the result from the snapshot (also matches PGN).
    await page.wait_for_function(
        """() => {
            const el = document.querySelector('.winbox.sturddle-wb-live-frozen .lg-result-score');
            return el && el.textContent.trim() === '1-0';
        }""",
    )
    _assert_no_errors(errors)


@pytest.mark.asyncio
async def test_TFROZEN2_unresolved_snapshot_non_running_shows_toast(server_app, make_page):
    """Snapshot with an unresolved live entry on a non-RUNNING tournament:
    no window is restored for that entry, and the 'finished while away' toast
    surfaces."""
    base, app = server_app
    tids = [t.id for t in app.state.tournament_store.list()]
    tid = tids[0]
    # Force terminal status so the restore loop takes the drop branch.
    app.state.tournament_store.update_status(tid, "done")

    _ctx, page, errors = await _new_page(make_page)
    await _goto_app(page, base)
    await _set_saved_state(page, tid, {
        "_closed": False,
        "standings": {"open": True, "x": "100px", "y": "100px",
                      "width": "500px", "height": "300px",
                      "min": False, "max": False, "z": 1},
        "schedule": {"open": False},
        "engines": {"open": False},
        "log": {"open": False},
        "live": [{
            "proxyId": "proxy-a", "gameId": "pair-1",
            "label": "engine-A vs engine-B", "engineName": "engine-A",
            "x": "200px", "y": "200px", "width": "320px", "height": "560px",
            "min": False, "max": False, "z": 2,
            # No `resolved` key -- the close-time gap case.
        }],
    })
    await _click_row(page, 0)
    await _open_workspace_via_ribbon(page)
    # Only Standings should restore; the unresolved entry is dropped.
    await _wait_wb_count(page, 1)
    # Toast surfaces with the "finished while away" copy.
    await page.wait_for_function(
        """() => [...document.querySelectorAll('.toast')]
                   .some(t => /finished while away/.test(t.textContent))""",
    )
    _assert_no_errors(errors)
