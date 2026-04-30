# Tournament Subsystem — Implementation Plan

Status: agreed; ready to start. Companion to `docs/tournament-spec.md`
(design). This file tracks **how** we build it; the spec describes
**what** we are building. Update slice-by-slice as work lands.

The aim is small, individually-shippable slices so each merge leaves
the system working. Within each slice, order is server → API → UI so
the UI is built on top of an already-tested server.

---

## Slice 0 — Decompose the existing stub (½ day)

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

## Slice 1 — Store + on-disk layout (1 day)

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

## Slice 2 — PGN stats (1 day)

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

## Slice 3 — FastchessRunner (1–2 days)

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

## Slice 4 — Orchestrator (½ day)

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

## Slice 5 — REST + WS surface (1 day)

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

## Slice 6 — Tournaments perspective UI: list view + settings sub-tab (1 day)

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

## Slice 7 — Reusable template form component (½ day)

- Single component used in 3 contexts (Settings tab, New Tournament
  dialog, read-only inspect). Mounting context decides editable vs
  read-only.
- The Settings tab and New Tournament dialog from Slice 6 swap their
  inline forms for this component.

Why after Slice 6 and not before: the form's exact field set firms up
while wiring Slice 6; building the reusable abstraction with the
second use case in front of you is cheaper than guessing.

---

## Slice 8 — Workspace: Standings + Schedule + Event log (1 day)

- WinBox already vendored. Three windows, default layout from spec.
- Subscribes to the WS for the open tournament; populates Standings
  (from `standings_update`), Schedule (from
  `game_started`/`game_finished`), Event log (from all events).
- Layout persistence at
  `platformdirs.user_config_dir(...) / "workspace-layout.json"`.
- Works for both running and stopped/done tournaments (live-only
  windows simply absent).

---

## Slice 9 — Workspace: Live game windows (1–2 days)

- Subscribes to per-game UCI info from the proxy stream.
- N windows, opened on `game_started`, closed on `game_finished`.
- Reuses the existing `web/app/board.js` for the small live board.
- Depends on the proxy broadcast tap actually working — currently a
  TODO in `server/sturddle_view/tournament/proxy.py:48-50`. Wiring
  that tap is part of this slice.

End of Slice 9: feature-complete per the spec.

---

## Slice 10 (optional, post-Phase-1) — Polish and follow-ups

- Engine-options dialog label/input alignment fix.
- `python -m sturddle_view.tournament.cli` thin wrapper (Phase 1.5).
- Tweak default workspace layout based on actual use.

---

## Dependencies and risks

- **Cross-platform process control** (Slices 3, 9): cribbed from
  `~/Projects/sturddle-2/tools/tuneup/spsa/worker.py`. Lowest-risk
  part — working reference exists.
- **Proxy broadcast tap** (Slice 9): currently a TODO. Decision
  needed: tap-via-HTTP-POST (worker.py-style; simple, slightly chatty)
  vs tap-via-localhost-socket (faster, more code). Lean HTTP-POST
  initially, optimize only if profiling demands. Not a blocker for
  Slices 0–8.
- **SPRT formula correctness** (Slice 2): standard but easy to subtly
  mis-implement. Fixture-driven tests are the mitigation.
- **WS event volume** (Slice 9): with N=8 parallel games each emitting
  `info` lines at 1+ kHz, naive WS forwarding could overwhelm the
  browser. Mitigation: server-side throttle to ~10 Hz per game
  (standard practice). Plan to do this from the start in Slice 9.

---

## Total estimate

~9–11 days of focused work. Each slice small enough to land in one
sitting and be reviewed without a marathon PR.

---

## Status tracker

Update as slices land.

| Slice | Status | Landed | Notes |
|-------|--------|--------|-------|
| 0 — Decompose stub | not started | — | |
| 1 — Store | not started | — | |
| 2 — PGN stats | not started | — | |
| 3 — FastchessRunner | not started | — | |
| 4 — Orchestrator | not started | — | |
| 5 — REST + WS | not started | — | |
| 6 — Tournaments UI v0 | not started | — | |
| 7 — Reusable template form | not started | — | |
| 8 — Workspace (3 windows) | not started | — | |
| 9 — Workspace (live games) | not started | — | |
| 10 — Polish | optional | — | post-Phase-1 |
