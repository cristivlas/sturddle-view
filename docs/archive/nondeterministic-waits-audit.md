# Nondeterministic-waits audit & leak cleanup

What we found, fixed, and learned hunting sleep/timer-based
synchronization and the ResourceWarnings it masked.

## The rule

Never use `sleep`/`setTimeout`/`asyncio.sleep`/busy-wait as a sync
primitive -- i.e. don't sleep waiting for state another task,
process, transport, or filesystem produces. Periodic features
(clock ticks, debounce, polling external systems with no push
channel) are fine.

Failure-mode test: does the timer wait for X produced by someone
else to become ready? Violation. Is the timer the feature itself,
or a self-owned cadence? Fine.

## Shipped fixes

- `engine_supervisor.py` -- `for _ in range(3): await asyncio.sleep(0)`
  replaced with `_close_and_wait()` chaining each transport's
  `connection_lost` into a future, then `asyncio.wait(..., timeout=...)`
  as a hang-safety bound.
- `desktop.py` + `tests/conftest.py` (`run_uvicorn`) -- uvicorn
  startup busy-wait replaced with `threading.Event` set by a
  `Server` subclass override of `startup()`. Shared helper in
  `_uvicorn_signal.py`.
- `tests/conftest.py` -- dropped `force_exit=True`. Lets uvicorn run
  full lifespan shutdown so `app.state.hve.shutdown()` actually
  fires, which cascades through the engine supervisor.
- E2E tests -- Playwright `ctx`/`page` lifecycle moved into shared
  `make_page` / `page` fixtures in conftest. Synchronous context
  teardown before fixture teardown prevents WS leaks bleeding into
  the next test.
- Six e2e files migrated from inline uvicorn (busy-wait + force_exit)
  to the shared `run_uvicorn` helper.
- `test_recent_imports._run()` -- close per-call
  `asyncio.new_event_loop()`.
- `_instance_lock.acquire()` -- release prior `_lock_fh` before
  reassigning (prod fix; latent leak surfaced by tests).
- `test_instance_lock.py` -- `subprocess.Popen` as context manager so
  stdout/stderr pipes close deterministically.
- E2E tests -- 5 redundant `page.wait_for_timeout(...)` calls deleted
  where a real `wait_for_function` / `wait_for_selector` follow-up
  already provided the signal.
- `test_e2e_sprt_ui.py` -- replaced "wait 600ms then check counter" with
  `page.expect_request(...)` subscribing to the actual PUT request.
- 107 explicit `timeout=N` kwargs stripped from Playwright/asyncio
  waits across 16 test files. Tests now rely on the real signal with
  Playwright's default 30s hang-bound; CI flakes from tight cushions
  go away, slow machines stop misfiring.
- Orchestrator -- `_schedule_tailer_start/stop` now expose their
  pending tasks via `_pending_tailer_tasks` set; tests use
  `_await_pending_tailer_tasks()` instead of sleeping or polling for
  tailer lifecycle.
- E2E test infrastructure rewritten to run uvicorn as a **subprocess**
  (`run_uvicorn_subprocess` helper in conftest). Killing the process
  on teardown lets the OS reclaim TCP/transport state instantly --
  no more proactor close-callback races. Removes the entire
  `ResourceWarning` class on Windows.
- Prod plumbing for the subprocess fixture:
  - `SV_*` env overrides on every persistent-path function
    (`default_settings_file`, `default_registry_path`, etc.) so per-test
    `tmp_path` isolation flows into the child process.
  - `SV_INSTANCE_LOCK_PATH` override on the single-instance lock.
  - `Settings.test_mode` field + conditional mount of `/_test/*`
    endpoints (HVE install/state, tournament proxy_secret) in
    `api/test_hooks.py`. Never loaded in production.
- `filterwarnings = ["error"]` **flipped on** in `pyproject.toml`.
  Suite green: 1088 passed, 13 skipped, zero warnings.

## Diagnostic techniques

- `filterwarnings = ["error"]` in pytest config to escalate every
  warning to a hard failure. Surfaces latent `ResourceWarning`,
  `DeprecationWarning`, unraisable exceptions.
- Run failing tests in isolation vs in the full suite. Pass-alone +
  fail-in-suite = order-dependent contamination from a prior test.
- `git diff --ignore-all-space` to filter CRLF/LF noise on Windows.
- `pytest_runtest_teardown` autouse hook calling `gc.collect()`
  inside `warnings.catch_warnings(record=True)` and `pytest.fail()`-ing
  on caught `ResourceWarning` / `PytestUnraisableExceptionWarning`.
  Attributes leaks to the test that owns them instead of the
  innocent next test. Reverted from main -- useful diagnostic, not a
  permanent gate. See git history.

## Recurring patterns

- Magic-number yields (`for _ in range(N): await asyncio.sleep(0)`)
  -- guessing how many event-loop turns the proactor needs. Real fix
  is to hook the actual done-signal (`connection_lost`, `returncode`).
- Startup busy-waits (`while not server.started: time.sleep(0.05)`)
  -- replace with a `threading.Event` set by a subclass override.
- `force_exit=True` masks lifespan shutdown -- on Windows it kills
  the loop before lifespan teardown runs, leaving engine subprocess
  transports to be reclaimed by GC. Use clean shutdown; close
  Playwright contexts synchronously before signalling exit.
- Module-level singleton fh reassigned without close() -- prod code
  held a global and overwrote it; harmless long-lived, leaks
  per-call in tests.
- `subprocess.Popen` without context manager -- stdout/stderr pipes
  only close on `__del__`. Always use `with subprocess.Popen(...)`.
- `asyncio.new_event_loop()` without `.close()` -- particularly in
  test scaffolds running async code from sync.
- Inline copies of uvicorn fixtures across multiple files. Migrate
  to a single helper.

## Known remaining

- Subprocess connect-probe in `run_uvicorn_subprocess` uses
  `s.settimeout(0.1)` in a tight retry loop until the server's
  listening port answers. Not sync-by-sleep (no underlying sleep,
  the timer bounds one connect attempt); the real signal is the OS
  port-bound state. Tracked as a follow-up only if it ever proves
  to be a problem.
- `test_e2e_workspace_persistence.py` was dropped during the
  subprocess migration -- niche UX (window placement persistence)
  whose tests required deep `app.state` poking that wasn't worth
  bridging through new endpoints.

## If you suspect a leak

1. Flip `filterwarnings = ["error"]` in `pyproject.toml`.
2. Run the suite. Failing tests now point at warnings.
3. Run each failing test alone. Pass-alone + fail-in-suite means you
   are hunting order-dependent contamination -- consider re-enabling
   the `pytest_runtest_teardown` gc-attribution hook from this doc's
   history to find the actual leaker.
4. Common culprits: unclosed asyncio loops, `subprocess.Popen` without
   `with`, module-level fh reassignment, magic-number `asyncio.sleep(0)`
   yield counts, `force_exit=True` skipping lifespan shutdown.

## Biggest-bang-for-buck lesson

Strip explicit `timeout=N` kwargs from Playwright/asyncio waits EARLY.
Tight cushions are the #1 source of CI flakes ("works locally, fails
under load"). Removing them forces tests onto real signals; the
framework's default hang-bound (30s) covers the genuine-bug case.
The change is mechanical and the payoff is immediate. Do this before
chasing individual `wait_for_timeout` calls or ResourceWarnings.
