"""E2E: PerspectiveRouter activation ordering rules.

The router serializes activate() calls and coalesces queued ones:
- serialization: a switch never starts while another is mid-flight
  (interleaving double-mounted perspectives and leaked controllers);
- last-wins: among queued requests only the newest runs, skipped ones
  resolve false;
- same-tick pair on an idle queue: the first request never runs at all
  (its .then fires after the second already displaced it) -- intended;
- deliberate priority: an involuntary switch (persist:false) never
  displaces a queued deliberate one;
- involuntary on an idle queue: runs normally after the in-flight switch;
- drain cleanup + failure: a mount throw clears is-pending (no
  permanently blank root), the router keeps working afterwards, and the
  settled queue drains _activating/_latestRequest back to null.

All scenarios run in-page against a fresh router instance with fake
perspectives whose mounts block on test-resolved promises; ordering is
driven purely by promise resolution (microtask hops, no timers).

Skipped if Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import sys

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import (  # noqa: E402
    run_uvicorn_subprocess,
    wait_perspective_ready,
    watch_page_errors,
)


def _server_env(tmp_path):
    registry = EngineRegistry(path=tmp_path / "engines.json")
    registry.add(name="engine-A", path=sys.executable)
    return {
        "SV_PGN_DIR": str(tmp_path / "pgn"),
        "SV_TOURNAMENT_ROOT": str(tmp_path / "tournaments"),
        "SV_ENGINE_REGISTRY_PATH": str(tmp_path / "engines.json"),
        "SV_SETTINGS_FILE": str(tmp_path / "settings.json"),
        "SV_GAME_STATE_PATH": str(tmp_path / "current_game.json"),
        "SV_IMPORTS_DIR": str(tmp_path / "imports"),
    }


# Runs every scenario against its own router + detached root and returns a
# per-scenario result object. hopUntil drains microtasks (bounded), so the
# only scheduling in play is promise resolution -- fully deterministic.
ROUTER_SCENARIOS = """async () => {
  const { PerspectiveRouter } = await import("/ui/app/perspectives.js");
  const out = {};

  const deferred = () => {
    const d = {};
    d.p = new Promise((resolve, reject) => { d.resolve = resolve; d.reject = reject; });
    return d;
  };
  const hops = async (n) => { for (let i = 0; i < n; i++) await Promise.resolve(); };
  const hopUntil = async (log, entry) => {
    for (let i = 0; i < 1000 && !log.includes(entry); i++) await Promise.resolve();
    if (!log.includes(entry)) throw new Error("hopUntil: never saw " + entry);
  };
  // Grace for a BROKEN router to interleave before a snapshot asserts it
  // did not (can't hop-until an absence).
  const SETTLE_HOPS = 50;

  const setup = () => {
    const log = [];
    // Detached root: computed transitionDuration is empty, so the fade
    // setTimeout never arms -- load-bearing for determinism. A class-based
    // .is-pending transition would silently reintroduce a timer here.
    const root = document.createElement("div");
    const router = new PerspectiveRouter({ root, ctx: {} });
    const add = (id, opts = {}) => router.register({
      id,
      label: id,
      async mount() {
        log.push("mount:" + id);
        if (opts.gate) await opts.gate.p;
        if (opts.boom) throw new Error("boom:" + id);
        return { unmount() { log.push("unmount:" + id); } };
      },
    });
    return { log, root, router, add };
  };

  // 1: serialization -- B must not touch A while A's mount is in flight.
  {
    const { log, router, add } = setup();
    const gate = deferred();
    add("A", { gate });
    add("B");
    const pa = router.activate("A");
    await hopUntil(log, "mount:A");
    const pb = router.activate("B");
    await hops(SETTLE_HOPS);
    const preGate = [...log];
    gate.resolve();
    out.s1 = { rA: await pa, rB: await pb, preGate, log: [...log] };
  }

  // 2: last-wins among queued -- B queued, C displaces it.
  {
    const { log, router, add } = setup();
    const gate = deferred();
    add("A", { gate });
    add("B");
    add("C");
    const pa = router.activate("A");
    await hopUntil(log, "mount:A");
    const pb = router.activate("B");
    const pc = router.activate("C");
    gate.resolve();
    out.s2 = { rA: await pa, rB: await pb, rC: await pc, log: [...log] };
  }

  // 3: same-tick pair on an idle queue -- the first never runs.
  {
    const { log, router, add } = setup();
    add("A");
    add("B");
    const pa = router.activate("A");
    const pb = router.activate("B");
    out.s3 = { rA: await pa, rB: await pb, log: [...log] };
  }

  // 4: involuntary (persist:false) never displaces a queued deliberate.
  {
    const { log, router, add } = setup();
    const gate = deferred();
    add("A", { gate });
    add("B");
    add("C");
    const pa = router.activate("A");
    await hopUntil(log, "mount:A");
    const pb = router.activate("B");
    const pc = router.activate("C", { persist: false });
    gate.resolve();
    out.s4 = { rA: await pa, rB: await pb, rC: await pc, log: [...log] };
  }

  // 5: involuntary with nothing queued runs normally after the in-flight one.
  {
    const { log, router, add } = setup();
    const gate = deferred();
    add("A", { gate });
    add("B");
    const pa = router.activate("A");
    await hopUntil(log, "mount:A");
    const pb = router.activate("B", { persist: false });
    gate.resolve();
    out.s5 = { rA: await pa, rB: await pb, log: [...log] };
  }

  // 6: mount throw rejects, clears is-pending, and the router recovers.
  {
    const { log, root, router, add } = setup();
    add("F", { boom: true });
    add("A");
    let threw = false;
    try { await router.activate("F"); } catch { threw = true; }
    const pendingAfterThrow = root.classList.contains("is-pending");
    const rA = await router.activate("A");
    // Drain cleanup fires on a microtask attached before our await; one
    // hop guarantees it ran. White-box (private fields) -- the only
    // observable form of the drain, so an internal rename lands here.
    await hops(1);
    const drained = router._activating === null && router._latestRequest === null;
    out.s6 = { threw, pendingAfterThrow, rA, drained, log: [...log] };
  }

  return out;
}"""


def _check_serialization(s):
    assert (s["rA"], s["rB"]) == (True, True)
    # B stayed queued (no unmount/mount) until A's gate resolved.
    assert s["preGate"] == ["mount:A"]
    assert s["log"] == ["mount:A", "unmount:A", "mount:B"]


def _check_last_wins(s):
    assert (s["rA"], s["rB"], s["rC"]) == (True, False, True)
    assert s["log"] == ["mount:A", "unmount:A", "mount:C"]


def _check_same_tick_first_skipped(s):
    assert (s["rA"], s["rB"]) == (False, True)
    assert s["log"] == ["mount:B"]


def _check_deliberate_priority(s):
    assert (s["rA"], s["rB"], s["rC"]) == (True, True, False)
    assert s["log"] == ["mount:A", "unmount:A", "mount:B"]


def _check_involuntary_idle_queue(s):
    assert (s["rA"], s["rB"]) == (True, True)
    assert s["log"] == ["mount:A", "unmount:A", "mount:B"]


def _check_throw_recovery_and_drain(s):
    assert s["threw"] is True
    assert s["pendingAfterThrow"] is False
    assert s["rA"] is True
    assert s["drained"] is True
    assert s["log"] == ["mount:F", "mount:A"]


# Scenario key -> named rule check. Soft-collected below so one broken
# rule can't mask the others (one server spawn covers all six).
SCENARIO_CHECKS = {
    "s1": _check_serialization,
    "s2": _check_last_wins,
    "s3": _check_same_tick_first_skipped,
    "s4": _check_deliberate_priority,
    "s5": _check_involuntary_idle_queue,
    "s6": _check_throw_recovery_and_drain,
}


@pytest.mark.asyncio
async def test_router_activation_ordering(tmp_path, make_page):
    with run_uvicorn_subprocess(env_overrides=_server_env(tmp_path)) as base:
        _ctx, page = await make_page(viewport={"width": 1280, "height": 800})
        errors = watch_page_errors(page)
        await page.goto(base + "/")
        await wait_perspective_ready(page)

        out = await page.evaluate(ROUTER_SCENARIOS)

        failures = []
        missing = sorted(set(SCENARIO_CHECKS) - set(out))
        if missing:
            failures.append(f"scenarios missing from page result: {missing}")
        for key, check in SCENARIO_CHECKS.items():
            if key not in out:
                continue
            try:
                check(out[key])
            except AssertionError as e:
                failures.append(f"{check.__name__}[{key}]: {e}\n  data: {out[key]}")
        if errors:
            failures.append(f"page errors: {errors}")
        assert not failures, "\n".join(failures)
