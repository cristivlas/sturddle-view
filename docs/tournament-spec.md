# Tournament Subsystem — Design Specification

Status: design agreed; UI/UX details pending. Phase 1.

This document specifies the tournament-management subsystem of SturddleView.
It supersedes the brief tournament references in `docs/spec.md` and is the
source of truth for implementation. UI/UX details (specifically the workspace
window layout) are deferred and will be appended in a follow-up section.

---

## Goals (Phase 1)

- Run engine vs engine tournaments locally, started **from the GUI only**.
- Support SPRT and round-robin / gauntlet via a single underlying tournament
  runner (`fastchess`).
- Preserve cross-platform portability (Linux / macOS / Windows 11). No
  platform-locked code paths in either the wrapper or its UI surface.
- Keep the implementation factored such that a thin CLI wrapper can later
  drive the same orchestrator without UI changes (Phase 1.5).
- Keep the implementation factored such that an additional runner
  (`cutechess-cli`) can be added later behind the same abstraction without
  rewriting callers.

Out of scope for Phase 1:

- Pause / Resume of a running tournament.
- Queue / scheduling of tournaments (Phase 2).
- Attaching to a tournament started outside the GUI ("peek" / headless
  attach). The orchestrator is factored so this can be added later.
- Connecting to remote machines running tournaments.
- Multiple tournaments running concurrently on the same server (enforced
  single-active; see "Concurrency policy" below).

---

## Runner strategy

- **One runner shipped in Phase 1**: `fastchess`. No git submodule, no
  bundling. Runtime-detected via a configured binary path.
- The runner abstraction (`Orchestrator` interface in
  `server/sturddle_view/tournament/orchestrator.py`) is **kept**. Only one
  concrete implementation (`FastchessOrchestrator`) is shipped. The
  abstraction exists so a `CutechessOrchestrator` can be added later
  without rewriting callers; it does **not** exist as a generalized
  facade speculating on capabilities of unknown future runners.
- Per-runner detection: if the configured binary is missing or invalid,
  the Tournaments perspective surfaces a single empty-state message
  ("fastchess not found — set the path in Settings"). No silently
  disabled controls.
- The runner kind is **inferred from the configured path/binary name**.
  No separate `kind` setting is exposed today; revisit only if/when a
  second runner is added.

---

## Concurrency policy

- **Single active tournament**, enforced by the server. Attempts to
  start a second tournament while one is running fail with a clear
  error.
- This applies per server instance. It is unrelated to the **games in
  parallel** setting (fastchess `-concurrency`), which controls how
  many *games* fastchess plays in parallel within a single tournament.
- When remote / multi-host support is added later, this constraint may
  be relaxed per host. Not in Phase 1.

---

## Lifecycle and state machine

States: `idle` → `running` → (`stopped` | `done`).

- **Start**: validates the frozen config, creates the on-disk tournament
  directory, spawns fastchess, transitions `idle → running`.
- **Stop**: hard-kills the fastchess subprocess (`proc.kill()` +
  `proc.wait()`), transitions `running → stopped`. The in-flight
  game(s) are lost; previously completed games are preserved in
  `games.pgn` because fastchess writes them as they finish (with
  `append=true`).
- **Done**: fastchess exits cleanly (all rounds completed, or SPRT
  decided), transitions `running → done`.

There is **no Pause/Resume verb** in Phase 1. Rationale: simpler state
machine, identical behavior on all platforms, no chunked-loop runner
complexity, no signal-handling asymmetry between POSIX (SIGTERM) and
Windows (TerminateProcess is hard-kill anyway). If a Resume verb is
later requested, fastchess's `-config file=…` mechanism makes it
trivially addable without changing the existing state machine — Resume
becomes "Start with `-config` pointing at the prior tournament's
artifacts."

### Cross-platform process control

Implementation must use the same tactics validated in
`~/Projects/sturddle-2/tools/tuneup/spsa/worker.py`:

- **Process-group isolation** when spawning fastchess so the orchestrator
  can target it without affecting the parent server:
  - Windows: `creationflags=CREATE_NEW_PROCESS_GROUP`.
  - Unix: `start_new_session=True`.
- **Pipe drain threads/tasks** for fastchess stdout/stderr so a full
  pipe never deadlocks the wrapper while it polls or waits.
- Hard-kill path uses `proc.kill()` + `proc.wait()`; works identically
  on all platforms.

---

## Standings, Elo, and SPRT

The runner's stdout summary is **not** the source of truth. Standings,
Elo, and SPRT statistics are computed by parsing `games.pgn` ourselves.

Why:

- A `Stop` mid-tournament still has correct standings — every game
  fastchess flushed to PGN counts; only the in-flight game is lost.
- A future Resume that appends to the same PGN yields correct
  cumulative numbers without special handling.
- Swapping to a different runner later (cutechess, custom) does not
  affect the math.

PGN parsing uses `python-chess` (already a project dep). Standard
result tags (`1-0`, `0-1`, `1/2-1/2`) drive game tallies. SPRT requires
pentanomial scoring (paired games); the formula is standard but must be
implemented in the wrapper, not delegated. Approximate effort: ~40
lines, plus tests against known fixtures.

---

## On-disk persistence layout

```
<tournaments-root>/
  <id>/
    state.json     ← wrapper-owned: id, name, status, timestamps, frozen template
    config.json    ← fastchess-owned: resume artifact (its filename, we don't pick)
    games.pgn      ← fastchess-owned: -pgnout file=games.pgn append=true
    logs/
      wrapper.log    ← orchestrator output
      fastchess.log  ← fastchess stdout/stderr capture
```

- **One directory per tournament**, named by `<id>` (UUID).
- **`state.json`** is updated atomically using the existing
  `_atomic.atomic_write_json` helper.
- **`<tournaments-root>` default**: `platformdirs.user_data_dir(
  "sturddle-view") / "tournaments"`. User can override in the
  Tournaments tab settings (see "Settings surface" below).
- **Auto-create on first use**: if the configured root does not exist,
  it is created the first time the user creates a tournament. A toast
  confirms creation; no silent surprises.
- The `logs/` subdirectory is reserved now (created on first use) so
  that future log-streaming features have an obvious place to land.

### `state.json` schema (initial)

```json
{
  "id": "uuid-string",
  "name": "human-readable name",
  "status": "idle | running | stopped | done",
  "created_at": "ISO-8601",
  "started_at": "ISO-8601 | null",
  "stopped_at": "ISO-8601 | null",
  "template": { ... frozen template values, see below ... },
  "engines": [ ... frozen engine references, see below ... ]
}
```

Schema may be extended; existing fields are stable contracts.

---

## Tournament template (frozen-on-create)

Tournaments are created from a **template** — a set of run-time
parameters that override per-engine defaults for the duration of the
tournament. The template values are frozen into `state.json` at
creation time and **cannot be modified** after the tournament is
constructed; they can be inspected (read-only).

### Template fields

- **Time control** (single TC for the whole tournament).
- **Ponder** on/off (think on opponent's time).
- **Hash** size in MB (per engine).
- **Threads** (per engine; the UCI `Threads` option).
- **Games in parallel** (fastchess `-concurrency`; how many *games*
  run in parallel — independent from `Threads`). The label is "games
  in parallel" in the UI; the code/CLI flag retains the fastchess
  name.
- **Opening book** path (fastchess `-openings file=…`; tournament-level
  starting positions for both engines, **not** a per-engine UCI option).
- **Tablebase** path (per-engine `SyzygyPath` UCI option; engines that
  don't advertise it ignore it silently).
- **Tournament type**: round-robin | gauntlet (and `-seeds N` for
  gauntlet).
- **Rounds** count.
- **Games per round** (default 2; values >2 do not improve statistics).
- **SPRT parameters** (optional): `elo0`, `elo1`, `alpha`, `beta`,
  `model`.
- **Adjudication** (draw / resign thresholds).

### Override semantics

- The template applies to **standard, runner-controlled UCI options**
  (`Threads`, `Hash`, `Ponder`, `SyzygyPath`). For these, the template
  value wins over whatever the registered engine has stored.
- The template does **not** override engine-specific options (eval
  weights, history pruning, search-internal knobs); those continue to
  come from the engine's per-engine UCI options registry entry.
- The opening book is tournament-level (picks start positions for both
  sides), not an engine UCI option.

### Reusable form component

The same form component renders in three contexts:

1. **Settings → Tournament defaults**: editable; persists as the
   default template for new tournaments.
2. **New Tournament dialog**: editable; pre-filled from the saved
   defaults; per-tournament overrides allowed.
3. **Inspect existing tournament**: read-only; renders the frozen
   template values from `state.json`.

This mirrors the per-engine UCI options dialog pattern already
established in the codebase.

---

## Settings surface

Tournament-related settings live in the **Tournaments perspective's
settings sub-area** (one inner tab on the Tournaments perspective),
**not** in the global Settings dialog. Rationale: these settings only
apply in the tournament context; co-locating them with the feature
they configure is more discoverable.

The settings sub-area contains:

- `fastchess.path` — binary location. Empty by default. Empty value
  triggers the "fastchess not found" empty state on the Tournaments
  list.
- `tournaments.root` — storage location for `<id>/` dirs. Defaults to
  `platformdirs.user_data_dir("sturddle-view") / "tournaments"`.
- The **tournament defaults template** (all fields listed under
  "Template fields" above), serving as the pre-fill for new
  tournaments.

Settings changes never affect a running tournament; templates are
frozen at creation time. This is consistent with the existing
"settings during game/tournament" policy in `docs/spec.md`.

---

## UI surface (high-level only; details deferred)

The Engines perspective's existing **Observe** sub-tab is **removed**.
"Observation" is no longer a separate surface; it is one verb on a
tournament row.

The Engines perspective's **Tournaments** sub-tab becomes a master list
of saved tournaments. Per-row verbs:

- **Start** — only enabled when no tournament is currently running.
- **Stop** — only enabled when this row is the running tournament.
- **Remove** — delete the saved tournament directory. Disabled while
  the tournament is running.
- **Open workspace** — opens the workspace view (WinBox-driven). Valid
  in any state: live windows when running, frozen view when stopped /
  done.

Plus a top-level action: **+ New Tournament** (opens the form
component).

The detailed window inventory for "Open workspace" is specified in
"Workspace" below.

---

## Workspace

"Open workspace" launches a WinBox-driven canvas containing a small,
fixed inventory of window kinds. The same canvas is used for both
running and stopped/done tournaments; only the live-only kinds are
absent in the latter case.

### Window inventory

- **Standings** (1 window). Table: engine, games played, W/L/D, score%,
  Elo ± error. If SPRT is configured, a row at the top showing LLR,
  bounds, and decision status (H0 / H1 / inconclusive). Source: PGN
  parsed by `pgn_stats`, refreshed as games complete.
- **Live game** (N windows; live-only). One per parallel game (the
  template's "games in parallel" value, N). Contains: small board, both engine names, current
  eval / depth / PV from the proxy stream, move list, clock. Closes
  when its game ends; a new window opens for the next pairing in that
  slot.
- **Schedule / pairings** (1 window). List of all pairings; status icon
  per row (done / running / pending); result for completed games.
  Clicking a completed row previews the game PGN.
- **Event log** (1 window). Chronological text feed: game start/finish,
  engine crashes, runner restarts, etc. Backed by the server-side
  ring buffer and persisted to `logs/wrapper.log`.

Total live window count: `N + 3`.

Explicitly not in Phase 1: per-engine info as separate windows (lives
inside each Live game window), a standalone PGN browser (Schedule's
row-click suffices), eval graphs (Phase 2).

### Stopped / done view

Same workspace. Live game windows are absent (they were live-only).
Standings, Schedule, and Event log remain with frozen content; the
Schedule's row-click PGN preview is the entry point for inspecting any
completed game.

A richer post-mortem (head-to-head matrix for round-robin, etc.) is
not in Phase 1; revisit after the basic workspace is in use.

### Layout persistence

A single user-level preferred workspace layout, persisted at
`platformdirs.user_config_dir("sturddle-view") / "workspace-layout.json"`.
Reopening any tournament restores this layout; rearranging in any
workspace updates it.

Per-tournament-instance layouts are explicitly not in Phase 1; revisit
if needed once the single-layout model is in use.

### Default layout (first open, no saved preference)

Deterministic placement, non-tiling (windows may overlap as concurrency
grows):

- Standings: top-left, ~40% width × ~50% height.
- Schedule: bottom-left, ~40% width × ~50% height.
- Event log: bottom-right, ~60% width × ~30% height.
- Live game windows: cascade from upper-right, each ~30% × ~45%,
  offset 30px per window.

Numbers are starting values; expect to tune once the workspace is in
front of real users.

---

## Server module shape

Two responsibilities — **persistent state** and **subprocess
lifecycle** — are decoupled via composition, not inheritance. Each
component is independently testable and the runner is the swap point
for a future second runner (cutechess); the store and orchestrator
are unaffected by that swap.

```
server/sturddle_view/tournament/
  __init__.py
  store.py           ← TournamentStore: on-disk layout, list/load/create/remove,
                       atomic state.json writes. No subprocess knowledge.
  pgn_stats.py       ← PGN → Elo / SPRT computation (pure functions)
  runner.py          ← Runner protocol: start / stop / is_running
  fastchess.py       ← FastchessRunner implements Runner. Knows fastchess CLI,
                       process-group isolation, pipe draining. No on-disk
                       schema knowledge beyond paths it is handed.
  orchestrator.py    ← Orchestrator: composes Store + Runner. Thin coordinator;
                       owns the single-active invariant and startup
                       reconciliation. The public API called by REST/WS
                       handlers and the future CLI wrapper.
  proxy.py           ← (existing) stdio proxy, broadcast tap to be wired
```

### Single-active invariant lives in the orchestrator

After a server crash, `state.json` on disk may say `running` for a
tournament whose subprocess is gone. The store (a typed view over a
directory tree) cannot tell. The orchestrator can, because it owns the
runner.

On server startup, the orchestrator reconciles: any tournament whose
persisted status is `running` is marked `stopped` (Phase 1 does not
support Resume; reviving the subprocess is not attempted). This
preserves prior games already in `games.pgn`; only the in-flight game
at the moment of the crash is lost — the same loss profile as a
user-initiated Stop.

### Decoupling from the web layer

The orchestrator must be callable without any web/REST coupling — its
public surface takes a tournament id (or a `TournamentConfig` for
creation) and a broadcast callback. This factoring is what enables the
Phase 1.5 CLI wrapper: it instantiates Store + Runner + Orchestrator
directly, no HTTP server involved.

REST/WS surface and exact method signatures are part of the
implementation plan, not this design doc; they will be added when the
UI/UX section is complete and we move to implementation.

---

## Open items

- **UI/UX**: workspace window inventory and default layout (live
  standings, per-game live boards, SPRT progress, event log). Pending
  follow-up discussion.
- **Implementation plan**: server modules' public APIs, REST/WS event
  shapes, phased landing order. To be drafted after UI/UX is settled.
- **Chunk-size knob for future Resume**: not relevant in Phase 1
  (single fastchess invocation, no chunking). If Resume is added later,
  decide whether to expose a chunk-size knob then.
