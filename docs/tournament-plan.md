# Tournament Subsystem — Implementation Plan

Status: agreed; ready to start. Companion to `docs/tournament-spec.md`
(design). This file tracks **how** we build it; the spec describes
**what** we are building. Update slice-by-slice as work lands.

The aim is small, individually-shippable slices so each merge leaves
the system working. Within each slice, order is server → API → UI so
the UI is built on top of an already-tested server.

---

## Slice 0 — Decompose the existing stub (XS)

No new behavior. Just the file split agreed in the spec.

- Replace `server/sturddle_view/tournament/orchestrator.py` (44 lines,
  all `NotImplementedError`) with the new module layout:
  - empty `store.py`
  - `runner.py` (`Runner` Protocol)
  - `fastchess.py`
  - slimmer `orchestrator.py`
  - empty `pgn_stats.py`

  All methods raise `NotImplementedError`.
- Confirm no callers import `Manager` / `TournamentConfig` (current
  grep: none) before deleting them.
- Smoke test: package imports cleanly.

Why first: locks in the file layout the rest of the slices write into.
Trivial PR, easy review.

---

## Slice 1 — Store + on-disk layout (S)

- `TournamentStore`: `create / get / list / remove / update_status /
  active`. No subprocess concept.
- `state.json` schema as specified, atomic writes via existing
  `_atomic.atomic_write_json`.
- `<tournaments-root>` resolution via `platformdirs` with
  auto-create-on-first-use.
- Tests: create → list → get → remove round-trip; corrupted
  `state.json` raises a typed error; concurrent writes don't corrupt
  (existing helper guarantees this; the test pins it).

Ships nothing user-visible. Foundation for everything else.

---

## Slice 2 — PGN stats (M)

- `pgn_stats.py`: `compute_standings(pgn_path) -> Standings`,
  `compute_sprt(pgn_path, params) -> SprtResult`.
- Pure functions, parse with `python-chess`, no I/O beyond reading the
  PGN.
- Tests against fixture PGNs (small hand-crafted files committed to
  the repo): round-robin, gauntlet, SPRT-decided H1, SPRT-decided H0,
  SPRT-inconclusive, illegal/aborted games skipped.

Ships nothing user-visible. Most algorithmic part — solid tests up
front.

---

## Slice 3 — FastchessRunner (L)

- `runner.py`: `Runner` Protocol — `start(tournament, on_event) /
  stop() / is_running()`.
- `fastchess.py`: `FastchessRunner` implements it.
  - Builds the fastchess CLI from a frozen template (engines, TC,
    hash, threads, ponder, book, TB, SPRT, adjudication,
    `-output format=fastchess`,
    `-pgnout file=games.pgn append=true`).
  - Spawns with `start_new_session=True` (Unix) /
    `CREATE_NEW_PROCESS_GROUP` (Windows).
  - Drains stdout/stderr to `logs/fastchess.log` via background tasks.
  - `stop()` = `proc.kill()` + `proc.wait()`; idempotent.
  - Detects clean exit vs killed; emits `done` or `stopped` event
    accordingly.
- Tests: spawn `python -c '...'` as a fake fastchess that prints,
  sleeps, exits — verify pipe drain, exit-code propagation, kill
  semantics, no zombie. Real fastchess exercised in Slice 5
  integration tests.

---

## Slice 4 — Orchestrator (S)

- Composes `Store` + `Runner`.
- `start(id)`: rejects if `store.active()` is non-None or
  `runner.is_running()`. Updates `state.json` to `running`, calls
  `runner.start`.
- `stop(id)`: calls `runner.stop`, updates `state.json` to `stopped`.
- `reconcile_on_startup()`: scans store, marks any `running` row as
  `stopped` (Phase 1: no Resume).
- Wires runner events back to store (`done` → status update, etc.)
  and to a broadcast callback for the WS layer.

End of Slice 4: tournament subsystem functionally complete on the
server, callable from a Python REPL or a future CLI. No web surface
yet.

---

## Slice 5 — REST + WS surface (M)

Server-side wiring only. Endpoints under `/api/tournaments` to match
the existing `api/` convention:

- `GET  /api/tournaments` → list
- `POST /api/tournaments` → create from template
- `GET  /api/tournaments/{id}` → state.json + computed stats
- `DELETE /api/tournaments/{id}` → remove (rejects if running)
- `POST /api/tournaments/{id}/start`
- `POST /api/tournaments/{id}/stop`
- `GET  /api/tournament-settings` / `PUT` → fastchess.path,
  tournaments.root, default template
- `WS   /ws/tournaments/{id}` → live events: `game_started`,
  `game_finished`, `engine_info`, `standings_update`, `status_change`,
  `engine_crash`, `runner_crash`

Integration test: real fastchess + two real engines (sturddle binary
+ stockfish-or-equivalent if available; skip on CI if not). Create →
start → stop → assert games.pgn has ≥1 game, standings non-empty.

End of Slice 5: server is shippable for command-line / curl-based use.
UI can be built independently from here.

---

## Slice 6 — Tournaments perspective UI: list view + settings sub-tab (M)

- Remove the **Observe** sub-tab from
  `web/app/perspectives/engines.js`.
- Replace the Tournaments placeholder with:
  - settings sub-area (fastchess.path picker, tournaments.root picker,
    template form)
  - master list with Start / Stop / Remove / Open workspace verbs per
    row
  - `+ New Tournament` action
- Empty states: "fastchess not found" when path unset/invalid, "no
  tournaments yet" when list empty.
- Wires REST + WS for live status badges on the row.

Ships a usable v0: user can configure paths, create / start / stop /
remove tournaments, but Open workspace is a stub.

---

## Slice 7 — Reusable template form component (S)

- Single component used in 3 contexts (Settings tab, New Tournament
  dialog, read-only inspect). Mounting context decides editable vs
  read-only.
- The Settings tab and New Tournament dialog from Slice 6 swap their
  inline forms for this component.

Why after Slice 6 and not before: the form's exact field set firms up
while wiring Slice 6; building the reusable abstraction with the
second use case in front of you is cheaper than guessing.

---

## Slice 8 — Workspace: Standings + Schedule + Event log (M)

- WinBox already vendored. Three windows, default layout from spec.
- Subscribes to the WS for the open tournament; populates Standings
  (from `standings_update`), Schedule (from
  `game_started`/`game_finished`), Event log (from all events).
- Layout persistence at
  `platformdirs.user_config_dir(...) / "workspace-layout.json"`.
- Works for both running and stopped/done tournaments (live-only
  windows simply absent).

---

## Slice 9 — Live observation pipeline

After Slice 8 user-testing the original "Slice 9 = live games window"
was split into three independent slices. The design is in the
"Live observation pipeline" section of `docs/tournament-spec.md`.

### Slice 9a — Forward fastchess stdout to event bus (XS)

The Event log window is currently sparse (lifecycle events only).
fastchess prints "Started game N (A vs B): ..." and "Finished game
N: result" to its own stdout, which we already drain to
`logs/fastchess.log`. This slice **also** forwards each captured line
to the event bus so the Event log window populates in real time.

No proxy involved. No correlation logic. ~30 minutes of work,
mostly a tap added to the existing drain task in `fastchess.py` plus
a new `tournament_runner_log` event kind.

This is shippable on its own; it makes the Event log immediately
useful and doesn't block 9b/9c.

### Slice 9b — Wire the proxy broadcast tap (M)

Replace the TODO in `tournament/proxy.py:48-50` with HTTP POSTs to a
new `/internal/proxy` server endpoint. Add per-proxy WS subscription
on the server: a Live game window opens by subscribing to a
`proxy_id`, and the server forwards that proxy's UCI lines to the
subscriber.

Server-side game pairing: per the spec's "Game pairing" section, the
server maintains a `pair_index` keyed on `(move_list, ply)` and
infers which two proxies are playing each other. Schedule window's
"in-progress" rows come from this. ~50 lines server-side.

Orchestrator change: when building the fastchess command, replace
each engine's `cmd=` with a wrapper that runs the proxy script
(`<sys.executable> -m sturddle_view.tournament.proxy ...`).

Volume mitigations baked in from the start (per spec):

- Proxy batches lines (50ms / 32 lines) before POSTing.
- Server only fully parses streams that have at least one subscriber;
  others contribute only their latest `position` line to the
  `pair_index` and are otherwise discarded.

End of 9b: proxy traffic flows; Schedule shows in-progress games;
no live boards yet.

### Slice 9c — Live game window (M)

The Live game window subscribes to one proxy's stream and renders:

- Board reconstructed from the latest `position startpos moves ...`
  via python-chess (one line of code).
- Both clocks from `go wtime ... btime ...`.
- This engine's eval / depth / PV from `info` lines.
- Bestmove highlight on each `bestmove`.

User opens the window by clicking a Schedule row.
Reuses the existing `web/app/board.js`.

End of 9c: feature-complete per the spec.

### Notes on Phase 1 vs later

- The "attach to engine, not to game" model means the user sees one
  engine's POV per window; opening a second window for the opponent
  shows the other side's eval/PV.
- Eval graphs are still Phase 2.
- C++ proxy rewrite is documented as an escape hatch in the spec;
  not in scope.

---

## Slice 10 (optional, post-Phase-1) — Polish and follow-ups

- Engine-options dialog label/input alignment fix.
- `python -m sturddle_view.tournament.cli` thin wrapper (Phase 1.5).
- Tweak default workspace layout based on actual use.

---

## Dependencies and risks

- **Cross-platform process control** (Slices 3, 9b): cribbed from
  `~/Projects/sturddle-2/tools/tuneup/spsa/worker.py`. Lowest-risk
  part — working reference exists.
- **Proxy broadcast transport** (Slice 9b): HTTP POST with batching
  (50ms / 32 lines) decided in the spec. Localhost Unix socket is the
  fallback if profiling demands it; not a blocker.
- **SPRT formula correctness** (Slice 2): standard but easy to subtly
  mis-implement. Fixture-driven tests are the mitigation.
- **WS event volume / DOM throttling** (Slices 9b–9c): handled by
  server-side selective parsing (only subscribed proxies fully parsed)
  + DOM render throttling on the Schedule window (≤4 Hz). See spec
  "Volume & high-concurrency considerations".
- **Real fastchess testing** (Slices 9b–9c): unlike Slices 0–8, the
  proxy slices need a real fastchess + real engine to validate the
  end-to-end argv path. Local fastchess at `~/Projects/fastchess/`
  per the project memory.

---

## Sizing

T-shirt sizes (XS / S / M / L) reflect relative effort and review
surface, not calendar time. Each slice is sized to land in one sitting
with a focused PR.

---

## Status tracker

Update as slices land.

| Slice | Status | Landed | Notes |
|-------|--------|--------|-------|
| 0 — Decompose stub | done | 608b87c | smoke tests in test_tournament_imports.py |
| 1 — Store | done | c3f253a | 23 tests; microsecond timestamps for deterministic sort |
| 2 — PGN stats | done | 80fa026 | 21 tests; pentanomial SPRT, normalized model only |
| 3 — FastchessRunner | done | 524731f | 18 tests; fake-fastchess via inline Python; cross-platform process group |
| 4 — Orchestrator | done | 1e3fab2 | 17 tests; 2 real-runner integration tests; rollback on start failure |
| 5 — REST + WS | done | 74be9bb | 21 tests; events flow via existing EventBus → /ws |
| 6 — Tournaments UI v0 | done | 4c4a654, 3eef665 | Observe sub-tab removed; list+settings+new dialog; 2 Playwright e2e tests |
| 7 — Reusable template form | done | 6f13b52 | core fields + Advanced JSON; mounted in 3 contexts; e2e tested |
| 8 — Workspace (3 windows) | done | 2d9f14b | Standings/Schedule/Event log; layout persisted in localStorage; 1 e2e test |
| 9a — Forward fastchess stdout to Event log | done | d48d88f | runner_log event; rendered as actual line in Event log window |
| 9b — Proxy broadcast tap + pairing | not started | — | M; needs real fastchess |
| 9c — Live game window | not started | — | M; depends on 9b |
| 10 — Polish | optional | — | post-Phase-1 |
