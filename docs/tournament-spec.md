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
  ("fastchess not configured — open Settings → Tournament to set the
  binary path") and disables the **+ New Tournament** button. No
  silently disabled controls.
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
  "sturddle-view") / "tournaments"`. User can override under
  Settings → Tournament (see "Settings surface" below).
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

Fields the form renders today (Phase 1):

- **Time control** (single TC for the whole tournament).
- **Hash** size in MB (per engine).
- **Threads** (per engine; the UCI `Threads` option). Default 1.
- **Games in parallel** (fastchess `-concurrency`; how many *games*
  run in parallel — independent from `Threads`). Default 1. The label
  is "games in parallel" in the UI; the code/CLI flag retains the
  fastchess name.
- **Rounds** count.
- **Tournament type**: round-robin | gauntlet (with **Seeds** field
  shown only when type = gauntlet).
- **Ponder** on/off (think on opponent's time).
- **Adjudication — Resign** with on/off switch. Inputs prefilled with
  the customary fastchess values (3 moves at score 700 cp); the switch
  controls whether the values are emitted on save.
- **Adjudication — Draw** with on/off switch. Inputs prefilled with
  the customary fastchess values (from move 40, for 8 moves, with
  `|score| ≤ 10` cp); the switch controls whether the values are
  emitted on save.

Spec'd but **not** in the v0 form (added in their own slices later):

- **Opening book** path (fastchess `-openings file=…`; tournament-level
  starting positions for both engines).
- **Tablebase** path (per-engine `SyzygyPath` UCI option).
- **Games per round** (>2 does not improve statistics; we currently
  rely on fastchess's default of 2).
- **SPRT parameters** (`elo0`, `elo1`, `alpha`, `beta`, `model`) —
  deferred to **Phase 2**. The server-side computation is implemented
  (`pgn_stats.compute_sprt`) and the API consumes a `template.sprt`
  sub-object if present, but the UI does not currently expose it.

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

The same form component (`mountTournamentTemplateForm` in
`web/app/tournament-template-form.js`) renders in three contexts:

1. **Global Settings dialog → Tournament tab**: editable; auto-saves
   on input (debounced) and persists as the default template for new
   tournaments.
2. **New Tournament dialog**: editable; pre-filled from the saved
   defaults; per-tournament overrides allowed.
3. **Inspect existing tournament** (click a tournament row in the
   Tournaments perspective): read-only; renders the frozen template
   values from `state.json`.

This mirrors the per-engine UCI options dialog pattern already
established in the codebase.

---

## Settings surface

Tournament-related settings live in a dedicated **"Tournament" tab in
the global Settings dialog** (alongside the existing General and Play
tabs). Rationale: these settings only apply when a tournament is being
configured or run; the Tournaments perspective stays focused on the
list of saved tournaments and the **+ New Tournament** verb. Putting
the path/defaults configuration in the global Settings dialog keeps
the perspective uncluttered and gives the user a single, predictable
place to find install-time configuration.

The Tournament settings tab contains:

- `fastchess_path` — binary location. Empty by default. Empty value
  triggers the "fastchess not configured — open Settings → Tournament"
  empty state on the Tournaments perspective and disables the
  **+ New Tournament** button.
- `tournaments_root` — storage location for `<id>/` dirs. Defaults to
  `platformdirs.user_data_dir("sturddle-view") / "tournaments"`.
- The **tournament defaults template** (all fields listed under
  "Template fields" above), serving as the pre-fill for new
  tournaments. Edited inline using the same reusable form component
  that the New Tournament dialog mounts; auto-saved on input (matching
  the rest of the Settings dialog's apply-on-change semantics).

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

Plus a top-level action: **+ New Tournament**, which opens a dialog
containing:

- A **Name** field.
- An **engine-list builder**: two panes (Available ↔ In tournament)
  with Add / Remove arrow buttons, plus Up / Down reorder buttons on
  the In-tournament pane (order matters for gauntlet seeding).
- The shared template form (see "Reusable form component" above),
  pre-filled from the saved defaults; per-tournament overrides are
  applied on top.

The dialog's primary action (**Create**) is enabled only when the
name is non-empty and at least two engines are picked. There is no
Cancel button — the dialog's X handles dismissal — and no
"required *" decoration on fields (see the project memory note on
modern app-style dialogs).

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
- **Schedule** (1 window). List of completed games (PGN-derived) plus
  any in-progress games the server is tracking (proxy-derived once the
  pipeline is wired). Clicking a row attaches a Live game window —
  see below.
- **Event log** (1 window). Chronological text feed driven by the
  existing `EventBus`. Phase 1 surface is sparse (`tournament_status`
  and lifecycle: started / done / stopped / runner_crash) plus a
  forward of fastchess's own stdout (`Started game N (A vs B)`,
  `Finished game N: result`). Per-game UCI traffic is **not** sent
  to the event log; it flows through a separate per-engine
  subscription (see "Live observation pipeline" below). Persisted to
  `logs/wrapper.log` and `logs/fastchess.log`.
- **Live game** (0..N windows; opt-in). User attaches by clicking a
  row in Schedule. The window subscribes to **one engine's** proxy
  stream and renders the position from the engine's POV. See "Live
  observation pipeline" below for what's shown and where each piece
  comes from. Closes when its game ends or the user dismisses it.

Static window count = 3 (Standings, Schedule, Event log). Live game
windows are opened on demand; the user attaches as many as they want
to follow.

Explicitly not in Phase 1: standalone PGN browser (Schedule's
row-click suffices), eval graphs (Phase 2).

---

### Live observation pipeline

This section captures the design for live game viewing — the part of
the workspace that was deferred when Slice 8 shipped. Implementing it
involves three independently-shippable pieces (see
`docs/tournament-plan.md` for the slice breakdown).

#### Two pieces, well-bounded

1. **Wrapper around fastchess** — the high-level manager
   (`FastchessRunner` + `Orchestrator`). Already shipped.
2. **Stdio proxy** — a thin pipe that sits between fastchess and each
   engine binary. Forwards stdin/stdout transparently and broadcasts
   a copy of every line to the GUI server. The proxy stays a **dumb
   pipe**: no chess knowledge, no UCI parsing, no game state.

Splitting these responsibilities cleanly is what keeps the design
manageable.

#### Attach-to-engine, not attach-to-game

The Live game window subscribes to **one engine's proxy stream** —
not to a "game" abstraction. The user picks the engine they want to
follow; the window renders the board from that engine's POV.

This trades one feature ("see both engines' eval/PV in one window")
for a major simplification: there's no need to correlate two engine
streams into a "game" before showing anything. Each proxy is its own
unit; the user does the correlation by clicking. To see the other
engine's POV, attach a second window to the other proxy.

Everything needed to render the board is already in the engine's UCI
stream:

- `position startpos moves e2e4 e7e5 ...` — fastchess sends the full
  move list every turn. Reconstruct the board with python-chess in
  one line; opponent's move comes for free.
- `go wtime ... btime ...` — both clocks.
- `info depth N score cp ... pv ...` — this engine's eval, depth, PV.
- `bestmove ...` — the move this engine just played.

What you give up: the **opponent engine's** internal eval/PV/depth.
That's available from the opponent's proxy if the user attaches a
second window to it.

#### Game pairing (server-side, optional)

For the Schedule window's "in-progress games" rows, we want to know
which two proxies are playing each other. Done server-side by hashing
the move list:

- Per-game key: `(move_list, half_move_number)`.
- The two engines in a paired game see **the same move list** at
  alternating ply numbers. After every full move both engines have
  observed the same `position startpos moves ...`.
- `pair_index: dict[move_list, list[(proxy_id, ply)]]`. On each
  `position` line from any proxy, update the entry. When two entries
  share the same move list at adjacent ply (one is at ply N, the
  other at N+1 or N-1), they're paired.
- O(1) per-line update. No linear search. Bounded memory:
  `concurrency × 2` proxies max.

Color and ply guard against false matches when two games happen to
share an opening sequence: as soon as the move lists diverge (move 1
in the worst case), the index self-corrects. Pairing may take
~1 second to lock in for a fresh game, which is acceptable — the
Schedule row simply appears a moment after the first move.

If pairing fails (unlikely but possible at very high concurrency),
the only consequence is a slightly wonky Schedule listing. The Live
game window itself still works because it subscribes per-proxy, not
per-pair.

#### Volume & high-concurrency considerations

UCI engines emit `info` lines continuously while searching. At
N=8–48 parallel games on a multi-core box, raw line-by-line POSTs
from each proxy to the server would peak in the thousands of
requests per second range.

Mitigations baked into the design:

- **Batch at the proxy.** Each proxy buffers and POSTs every ~50ms
  or every ~32 lines, whichever first. Drops request rate ~50× with
  no perceptible loss in liveness (50ms is below the human flicker
  threshold).
- **Selective parsing on the server.** Only fully parse the streams
  that have at least one subscriber (= at least one Live game window
  attached). Other proxies' data is kept only as the latest
  `position` line (for the pairing index) and discarded.
- **Throttle DOM updates.** Schedule re-renders at ≤4 Hz even if
  the underlying state ticks faster.

If profiling at very high concurrency (the user's 48-core box) ever
shows the Python proxy is itself the bottleneck, the proxy can be
rewritten in C++ — it's a self-contained process with a
language-agnostic protocol. Out of scope for now; documented as an
escape hatch.

#### What end-of-game looks like

The proxy doesn't classify; the consumer infers from the stream:

- **Start of a new game** on this engine: `ucinewgame`.
- **End of a game** on this engine: the next `ucinewgame` (the next
  game starts) or proxy disconnect (engine quit / fastchess closed
  the connection).
- **Result label** (`1-0` / `0-1` / `1/2-1/2`): comes from PGN, not
  from the proxy. fastchess writes the result on adjudication. The
  Schedule window already polls PGN; results overlay on a slight
  delay (≤5s today; instant once fastchess's stdout `Finished game N`
  line is forwarded into the event bus).

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
- **Workspace on mobile**: WinBox's floating-window model is unusable
  on narrow viewports. Acceptable for Phase 1 since tournament
  observation is a desktop-class use case. If mobile matters later,
  options: (1) at `max-width: 600px`, replace "Open workspace" with a
  single full-screen Standings-only view, or (2) force WinBox into a
  tabs/accordion layout on narrow viewports.
- **Engine renames don't propagate to live HVE display**: editing an
  engine's display name in the Roster does not update the Play
  perspective's side-panel label until the next page load. Tournaments
  intentionally freeze engine names at creation (by spec) and ignore
  later renames. The HVE side is not by design — re-resolving from
  the registry on each new game would fix it; left as-is until
  someone cares.
