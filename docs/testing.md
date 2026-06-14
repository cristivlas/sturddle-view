# Testing

## Current state

### Python: pytest + Playwright

All automated tests live under `server/tests/`. Config is in `pyproject.toml`
(`[tool.pytest.ini_options]`) and `server/tests/conftest.py`.

Run everything (activate the venv first, or invoke the interpreter directly:
`.venv/bin/python` on macOS/Linux, `.venv/Scripts/python.exe` on Windows):

```
python -m pytest -q
```

Run a single file or test:

```
python -m pytest server/tests/test_tournament_pgn_stats.py -q
python -m pytest -k frozen_window -q
```

#### Shared fixtures (`conftest.py`)

- `browser` (session scope): one headless Chromium for all e2e tests.
  Yields `None` if Playwright or Chromium is not installed, so each e2e
  test calls `pytest.skip("chromium not installed")` on `None`. Tests must
  create a fresh `browser.new_context()` per case for isolation.
- `_isolate_user_config` (autouse): redirects every default user-config
  path (game store, settings file, engine registry, tournament root) to
  `tmp_path`. Without it a test that constructs the app with defaults
  would touch the developer's real `~/.config` / platformdirs tree and
  could flip in-flight tournaments to `failed`.

#### Custom CLI options

- `--syzygy-path <dir>`: declared in `conftest.py`. Tests that exercise
  tablebase code path (e.g. `test_tablebase.py`) pick it up when the user
  has tablebase files locally; otherwise they skip.

#### Test data fixtures

- `server/tests/fixtures/sample_50.pgn`: first 50 games of a real
  Sturddle vs Sturddle run. Used by `test_tournament_pgn_stats.py` for
  realistic SPRT/standings/Elo calculations against a non-synthetic PGN.

#### Unit-ish tests (pure Python)

Exercise server modules directly, no HTTP, no browser. Fast (single-digit
seconds for the lot). Subject areas currently covered:

- Tournament core: `test_tournament_{orchestrator,pairing,store,fastchess,
  rescheck,game_lifecycle,uci_parse,imports}.py`.
- PGN pipeline: `test_pgn_{tail,reconciliation,reconcile_queue,save,
  eval_parse}.py`, `test_tournament_pgn_stats.py`.
- Engine machinery: `test_engines.py`, `test_hve_engine_defaults.py`.
- Play / persistence: `test_game_persistence.py`, `test_pause.py`,
  `test_takeback.py`, `test_import_position.py`,
  `test_settings_persistence.py`.
- Misc: `test_openings.py`, `test_slot_grid.py`, `test_tablebase.py`
  (needs `--syzygy-path`), `test_instance_lock.py`, `test_view_mode.py`.

#### HTTP API tests

Use FastAPI's `TestClient` against a real app instance with `tmp_path`-backed
stores. Files: `test_tournaments_api.py`, `test_tournament_proxy_api.py`,
`test_engines_api.py`, `test_settings_api.py`, `test_pause_api.py`,
`test_fs_api.py`.

Auth in tests: the server accepts the token via `HttpOnly` cookie (browser
path) or `Authorization: Bearer` header (CLI / tests). The `?token=` query
fallback was removed. Tests that exercise authenticated endpoints either
set `auth_disabled=True` on `Settings` or attach the header on the client:

```python
settings = Settings(token="test-token")
with TestClient(create_app(settings)) as c:
    c.headers["Authorization"] = "Bearer test-token"
    ...
```

Origin enforcement: `_OriginMiddleware` rejects non-GET requests with an
`Origin` header that doesn't match `Host`. Missing `Origin` is allowed
(Python `TestClient`, curl, and other non-browser clients don't send one;
browsers always do on cross-origin requests). Tests therefore need no
special handling.

#### End-to-end (Playwright)

Files named `test_e2e_*.py`. Spawn a real uvicorn server on a random port,
drive a headless Chromium via Playwright, assert on DOM state and (when
needed) on `localStorage` / page-evaluate hooks.

Current e2e coverage (representative; see `test_e2e_*.py` for the full
set of ~two dozen):

- `test_e2e_workspace_characterization.py`: tournament workspace layout
  geometry (tile/tidy/snap) invariants -- no overlap, in-bounds, perfect
  tiling + idempotency.
- `test_e2e_tournaments_ui.py` / `test_e2e_sprt_ui.py`: tournament list /
  ribbon / template form, and the SPRT row + status rendering.
- `test_e2e_studio_h2h.py`: Studio head-to-head board flow.
- `test_e2e_tournament_live_game.py`: live game window WS attach + render.
- `test_e2e_perspective_sync.py`: cross-perspective state sync.
- `test_e2e_play_from_here.py` / `test_e2e_xgame_toasts.py`: fork-a-game
  flow and its toasts.
- `test_e2e_ai_done_ribbon.py` / `test_e2e_ai_thinking_replay.py`: AI
  analysis done-ribbon + thinking replay.

Skip behavior: every file calls `pytest.importorskip("playwright.async_api")`
at module load, so a checkout without Playwright skips the whole e2e suite
at import. The files also carry `pytestmark = pytest.mark.e2e`, so
`-m e2e` / `-m "not e2e"` selects or excludes them.

Patterns to reuse from existing files:

- Spawning the server: `run_uvicorn_subprocess(...)` in `conftest.py` runs
  uvicorn as a real subprocess on a free port; the OS owns its lifecycle
  (kill on teardown, no proactor-cleanup races). Per-test isolation flows
  through `SV_*` env overrides (`SV_PGN_DIR`, `SV_TOURNAMENT_ROOT`,
  `SV_IMPORTS_DIR`, `SV_SETTINGS_FILE`, ...).
- Seeding state into that subprocess: test-only `/_test/...` HTTP hooks
  (e.g. `/_test/ai/publish_event`, `/_test/ai/seed_replay`,
  `/_test/hve/install`, `/_test/recents/seed_fork`) -- the test can't reach
  in-process `app.state`.
- Faking fastchess: point `SV_TOURNAMENT_FASTCHESS_PATH` at a stub binary
  whose `detect_binary` probe echoes back, so no real fastchess is spawned.
- A few older tests still drive the app in-process and reach `app.state`
  directly (e.g. `test_e2e_tournament_live_game.py`); prefer the subprocess
  pattern for new tests.
- Driving the browser without depending on hover state: prefer
  `page.evaluate("() => button.click()")` over `page.click(selector)` for
  elements gated behind CSS hover.

### JavaScript: no dedicated unit-test framework

There is no `package.json` at the repo root and no JS test runner wired up.
JavaScript code is currently exercised only via the Playwright e2e suite,
which covers user-visible behavior end-to-end but is poor for testing
pure-data helpers, internal closures, or bookkeeping invariants.

The web/vendor directory contains third-party packages with their own
`package.json` files, but those are not part of our test infrastructure.

### Security: manual smoke tests

The auth, bind-policy, and TLS code paths are not covered by automated
tests. Re-run these manually whenever any of the following change:
`auth.py`, `app.py` (middlewares / `/auth` route), `__main__.py` flag
validation, `desktop.py` startup, or the cookie/Bearer code in the
frontend.

1. Default loopback bind. `python -m sturddle_view`. Banner prints
   `bound on 127.0.0.1:8765` and `open: http://127.0.0.1:8765/auth?token=...`.
   Browser lands on `/ui/` with no token in the URL; status dot turns
   green. From a second machine, the port is unreachable (connection
   refused) -- loopback only.

2. Desktop mode. `python -m sturddle_view --desktop`. PyWebView window
   opens, hits `/auth?token=...`, lands on `/ui/`, WS connects. Token is
   pinned via `SV_TOKEN` so the uvicorn worker's `create_app()`
   sees the same value the window was opened with.

3. Tailscale / trusted-LAN open. `python -m sturddle_view --host 0.0.0.0
   --no-auth`. Startup logs a warning (`AUTH DISABLED on non-loopback
   bind ...`). LAN peers can hit `http://<lan-ip>:8765/` and load
   `/ui/` without a token. `--no-auth` alone (no `--host`) must NOT
   widen the bind -- it stays on `127.0.0.1`.

4. Rejected flag combinations (each exits with code 2, no traceback):
   - `--cert foo.pem` -> `--cert and --key must be provided together.`
   - `--key foo.key` -> same.
   - `--desktop --cert ... --key ...` -> `--cert/--key are not
     supported with --desktop ...`
   - `--cert nonexistent.pem --key nonexistent.key` -> `--cert file
     not found: ...`

5. TLS (BYO cert). Generate a cert+key, e.g.:
   ```
   openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem \
     -days 30 -subj "/CN=localhost" \
     -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
   ```
   Run `python -m sturddle_view --cert cert.pem --key key.pem`. Banner
   shows `https://...`. `curl -vk https://127.0.0.1:8765/healthz` returns
   `{"ok":true}`. Browser accepts the self-signed warning once, then app
   loads; cookie is set with `Secure`.

   The Windows proactor accept-resilience patch (`app.py`,
   `_install_proactor_accept_resilience`) must call `_make_ssl_transport`
   when `sslcontext is not None`. If it falls back to
   `_make_socket_transport` for TLS connections, browsers and curl see
   `ERR_SSL_PROTOCOL_ERROR` while uvicorn logs `Invalid HTTP request
   received` (TLS ClientHello bytes parsed as HTTP).

## Future ideas

### JS unit tests

Triggered conversation: bounded-set bookkeeping in `addLogEntry`
(see `tournament-workspace.js`). The function is small, pure, and has a
non-obvious invariant ("Set size stays in lockstep with array eviction")
that has no UI manifestation. e2e is the wrong tool — too slow, too
indirect, and the bug is invisible from the DOM.

Three options ranked roughly by ergonomics:

1. **Vitest.** Modern, ESM-native, jsdom support out of the box, parallel,
   watch mode. Strong default for new JS test work. Requires adding a
   minimal `package.json` and `vitest.config.js` at repo root; ~10 min to
   scaffold, ~30 min to wire into CI.
2. **`node --test`.** Built into Node 20+. Zero dependencies, no package.json
   strictly required, but assertion DX is sparse (`assert.deepStrictEqual`
   everywhere) and DOM testing needs manual jsdom setup. Best for pure
   data transforms with no DOM.
3. **Page-evaluate hooks in Playwright.** Expose `window.__sturddle_test`
   gated by a test build flag so e2e tests can read closure internals.
   Strong recommendation against: bolts test surface area onto production
   code, and the e2e suite is already too slow for tight feedback loops on
   pure-data invariants.

Strong lean if/when the need recurs: **Vitest**.

Candidate first targets (pure functions, no DOM):

- `addLogEntry` in `tournament-workspace.js` — Set/Array lockstep,
  reconciled-event short-circuit, ordering by seq.
- `snapshotLive` merging logic — resolved-key attachment.
- `slotGrid` math (also currently has a Python test, but the JS module is
  the actual implementation).

### Server-side ideas

- The frozen-window e2e tests already cover the happy paths through
  `read_game_record`. A direct pytest for the helper (asserting all six
  return keys against a fixture PGN) would catch field-shape regressions
  faster than the e2e roundtrip.
- Reconcile-queue stress tests for high-parallelism edge cases (currently
  exercised by `test_pgn_reconcile_queue.py`; expand if reconcile gets
  more responsibilities).

### Cross-cutting

- Memory regressions are invisible in both pytest and Playwright as
  configured. If `seenSeqs`-style unbounded growth becomes a recurring
  category of bug, a targeted Playwright run that pumps N events and
  inspects `performance.memory` (or an explicit test-only `Set.size` hook)
  could be added — but only once we have a second example to justify the
  pattern.
