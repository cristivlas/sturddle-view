# Tournament Subsystem -- Design Specification

Status: shipped (Phase 1).

This document specifies the tournament-management subsystem of SturddleView.
It supersedes the brief tournament references in `docs/spec.md` and is the
source of truth for implementation.

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

- Queue / scheduling of tournaments (Phase 2).
- Attaching to a tournament started outside the GUI ("peek" / headless
  attach). The orchestrator is factored so this can be added later.
- Connecting to remote machines running tournaments.
- Multiple tournaments running concurrently on the same server (enforced
  single-active; see "Concurrency policy" below).

## Terminology: UI labels vs. code states

The UI surfaces tournament lifecycle as **Start / Stop / Restart**;
the code's state machine uses **`idle / running / stopped / done`**
(no `paused` state). Stop transitions `running -> stopped`; Start on a
`stopped` row wipes the tournament directory and runs from scratch
(there is no resume -- see **No resume: Stop wipes** below). This split
is deliberate -- the UI verbs read naturally to users, the code keeps a
minimal state set -- and the rest of this spec uses the code names.

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
  ("fastchess not configured -- open Settings -> Tournament to set the
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

### Why single-active (not just an arbitrary limit)

Two reasons, in priority order:

1. **Hardware sanity.** rescheck (above) is per-tournament. Running
   N tournaments at once would either oversubscribe the box (each
   tournament's rescheck passes in isolation; sum doesn't) or
   require a global resource budget split across them -- both worse
   UX than "one at a time, finishes faster." On a beefy box you'd
   still rather run one tournament fast than several slow.
2. **UX.** The workspace, Live windows, Schedule, and standings are
   all keyed to "the active tournament." Multi-tournament needs a
   selector and disambiguation throughout. Significant surface for
   marginal value.

If we ever want to lift this:

- The orchestrator's per-tournament state (`_proxy_subscribers`,
  `_pair_ids`, `_pair_moves`, `_reconcile_queue`, `_pgn_tailer`)
  would need to be keyed by tournament id rather than held flat.
- Proxies would need a tournament-scoped tag in their broadcast
  payload (today the per-tournament secret already disambiguates
  active vs stale; it could carry the id too) so
  ``ingest_proxy_lines`` routes to the right orchestrator instance.
- rescheck would need a global host-load view, not per-template.

Documented for future-revisit; no work to do until someone has a
concrete need.

---

## Resource sanity checks (rescheck)

Block clearly-unsafe configurations before they reach fastchess. Lives
in `server/sturddle_view/tournament/rescheck.py` -- pure (no orchestrator
state), called by both the create-time endpoint and the start-time
recheck.

**Architecture.** Client owns *resolution* (it has the engine registry's
`option_schema`); server owns *comparison* (it knows the host CPU/RAM):

- Client (in `tournament-template-form.js`): for the picked engines,
  resolves `max_threads` and `max_hash_mb`. Global `engine_default_*`
  overrides win; otherwise the max across each engine's
  `options.<key>` then `option_schema.<key>.default` then a fallback
  (1 thread / 16 MB). Calls `POST /api/tournaments/rescheck` with the
  resolved values; on 400, blocks Create.
- Resolved values are folded into the template at create time, so
  `Orchestrator.start()` can re-run the same check against the stored
  template without consulting the registry. This catches direct-API
  misuse and stale tournaments started after host specs changed.

**Formula.**

```
cpu_load    = parallel * (2 if ponder else 1) * max_threads
ram_load_mb = parallel * 2 * (max_hash_mb + ENGINE_OVERHEAD_MB)
```

`ENGINE_OVERHEAD_MB` = 256 (symbolic; covers binary + NNUE weights + PV
stacks). Revisit when telemetry justifies it.

**Block conditions.**

- `cpu_load > logical_cores` -> `oversubscribed`
- `ram_load_mb > 0.75 * total_ram` -> `insufficient_ram`
- `pin_affinity AND cpu_load > physical_cores` ->
  `affinity_exceeds_physical`

`SV_ALLOW_OVERSUBSCRIBE=1` downgrades `oversubscribed` and
`insufficient_ram` to warnings (toast at create time, no block). The
affinity check is a correctness gate (hyperthreading siblings can't
satisfy CPU pinning) and is **never** silenced by the flag.

A start-time rescheck failure surfaces the same way as a runner crash:
status flips to `failed`, `last_error.stderr_tail` carries the message,
and `last_error.rescheck` carries the structured detail
(`{reason, cpu_load, ram_load_mb, ...}`).

---

## Lifecycle and state machine

States: `idle` -> `running` -> (`stopped` | `done`).

- **Start**: validates the frozen config, creates the on-disk tournament
  directory, spawns fastchess, transitions `idle -> running`.
- **Stop**: hard-kills the fastchess subprocess (`proc.kill()` +
  `proc.wait()`), transitions `running -> stopped`. The in-flight
  game(s) are lost; previously completed games are preserved in
  `games.pgn` because fastchess writes them as they finish (with
  `append=true`).
- **Done**: fastchess exits cleanly (all rounds completed, or SPRT
  decided), transitions `running -> done`.

The state machine has **no `paused` state**: the UI's Stop button
maps to `running -> stopped`, and Start on a `stopped` row wipes the
tournament directory and runs from scratch -- there is no resume
(see **No resume: Stop wipes** below).

### Cross-platform process control

Implementation must use the following tactics:

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

- A `Stop` mid-tournament still has correct standings -- every game
  fastchess flushed to PGN counts; only the in-flight game is lost.
- PGN is ground truth: since Start always wipes and runs from scratch,
  the parsed PGN is the single source of game counts (no merge with
  fastchess's `config.json`, which we only sanity-check against).
- Swapping to a different runner later (cutechess, custom) does not
  affect the math.

PGN parsing is a header-only line scan (regex over `[White ...]`,
`[Black ...]`, `[Result ...]`, `[Round ...]`) -- `chess.pgn.read_game`'s
full move-tree parse is ~50x slower on multi-MB PGNs and we never
consume the moves for stats. Results are cached per-path keyed by
`(mtime, size)`; PGN is append-only so this is sound. Standard result
tags (`1-0`, `0-1`, `1/2-1/2`) drive game tallies. SPRT requires
pentanomial scoring (paired games); the formula is standard but must be
implemented in the wrapper, not delegated.

### Elo: two numbers per engine

Each engine in the standings has **two** Elo values, both populated
when at least two engines are present:

- **`elo` (logistic Elo).** Computed by `elo_from_score(score_pct)`.
  The per-engine head-to-head Elo against the rest of the pool: for a
  2-engine tour with A scoring 52%, A.elo = +14 and B.elo = -14. This
  matches `ordo -a 0 -A B` (anchored: rating gap = 28). For gauntlets,
  per-challenger Elo is head-to-head vs the leader only.

- **`elo_ordo` (ordo-style joint fit).** A mean-centered rating from a
  joint iterative fit over the full game graph, replicating the
  algorithm in Ballicora's ordo (https://github.com/michiguel/Ordo,
  function `adjust_rating` in `rating.c`). Matches the output of
  `ordo -a 0 -M -D` on the same PGN to within rounding for ratings;
  the 95% margin is a Wald CI from the binomial Fisher information,
  which runs ~1.5x wider than ordo's bootstrap CI. For a 2-engine
  52%/48% split, `elo_ordo` is +/- 7 (each side), giving a 14 Elo gap
  consistent with `elo`.

Both numbers are emitted in the API and rendered in the standings
table; `elo_ordo` appears as a smaller, dimmer inline number next to
`elo` ("+/- X.X ordo"). The dual display lets users cross-check
against ordo without leaving the app while keeping the logistic Elo
(which matches fastchess stdout, cutechess, and published CCRL/CEGT
ratings) as the primary number.

Edge cases for `elo_ordo`:

- All-wins / all-losses engines are purged from the joint fit and
  surface `(None, None)`. ordo behaves the same with `-G`.
- Disconnected match graphs are fit per-component, each mean-centered
  independently. Cross-component comparison is undefined.

---

## On-disk persistence layout

```
<tournaments-root>/
  <id>/
    state.json     <- wrapper-owned: id, name, status, timestamps, frozen template
    config.json    <- fastchess-owned: its run config (we sanity-check, never resume from)
    games.pgn      <- fastchess-owned: -pgnout file=games.pgn append=true
    logs/
      wrapper.log    <- orchestrator output
      fastchess.log  <- fastchess stdout/stderr capture
```

- **One directory per tournament**, named by `<id>` (UUID).
- **`state.json`** is updated atomically using the existing
  `_atomic.atomic_write_json` helper.
- **`<tournaments-root>` default**: `platformdirs.user_data_dir(
  "sturddle-view") / "tournaments"`. User can override under
  Settings -> Tournament (see "Settings surface" below).
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

Tournaments are created from a **template** -- a set of run-time
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
  run in parallel -- independent from `Threads`). Default 1. The label
  is "games in parallel" in the UI; the code/CLI flag retains the
  fastchess name.
- **Rounds** count.
- **Tournament type**: round-robin | gauntlet (with **Seeds** field
  shown only when type = gauntlet).
- **Ponder** on/off (think on opponent's time).
- **CPU Affinity** on/off (`pin_affinity`). Emits fastchess `-affinity`
  so each game-slot is bound to a fixed pair of cores. Recommended for
  SPRT / rating-list runs.
- **Restart engines** on/off (`restart_engines`). Passes fastchess
  `-each restart=on` so each engine process is restarted between games,
  clearing hash/internal state for a clean start (slower than reusing
  processes).
- **Oversubscribe** -- no UI toggle. Set `SV_ALLOW_OVERSUBSCRIBE=1`
  (off by default) to permit CPU/RAM use beyond host capacity: rescheck
  blockers (see "Resource sanity checks" below) downgrade to warnings,
  and fastchess gets `-force-concurrency` so it doesn't reject
  `-concurrency > nproc`. Don't use for SPRT.
- **Auto-folded** at create time by the client and stored in the
  template: `max_threads` and `max_hash_mb` (worst-case across the
  selected engines, or the global override if set). Persisted so the
  server can re-run the rescheck at start time without consulting the
  engine registry.
- **Adjudication -- Resign** with on/off switch. Inputs prefilled with
  the customary fastchess values (3 moves at score 700 cp); the switch
  controls whether the values are emitted on save.
- **Adjudication -- Draw** with on/off switch. Inputs prefilled with
  the customary fastchess values (from move 40, for 8 moves, with
  `|score| <= 10` cp); the switch controls whether the values are
  emitted on save.

Spec'd but **not** in the v0 form (added in their own slices later):

- **Opening book** path (fastchess `-openings file=...`; tournament-level
  starting positions for both engines).
- **Tablebase** path (per-engine `SyzygyPath` UCI option).
- **Games per round** (>2 does not improve statistics; we currently
  rely on fastchess's default of 2).
- **SPRT parameters** (`elo0`, `elo1`, `alpha`, `beta`, `model`) --
  shipped: global defaults in Settings > SPRT, a template-form SPRT
  toggle, and a workspace status line. See
  [sprt-ux-spec.md](sprt-ux-spec.md).

### Override semantics

- The template applies to **standard, runner-controlled UCI options**
  (`Threads`, `Hash`, `Ponder`, `SyzygyPath`). For these, the template
  value wins over whatever the registered engine has stored; when the
  tournament leaves one unset, the engine's own stored value applies.
- The template does **not** override engine-specific options (eval
  weights, history pruning, search-internal knobs); those come from the
  engine's per-engine UCI options registry entry. Like everything else,
  they are snapshotted into `state.json` at create/edit time — later
  registry edits do not affect existing tournaments.
- The opening book is tournament-level (picks start positions for both
  sides), not an engine UCI option.

### Reusable form component

The same form component (`mountTournamentTemplateForm` in
`web/app/tournament-template-form.js`) renders in three contexts:

1. **Global Settings dialog -> Tournament tab**: editable; auto-saves
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

- `fastchess_path` -- binary location. Empty by default. Empty value
  triggers the "fastchess not configured -- open Settings -> Tournament"
  empty state on the Tournaments perspective and disables the
  **+ New Tournament** button.
- `tournaments_root` -- storage location for `<id>/` dirs. Defaults to
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

- **Start / Restart** -- only enabled when no tournament is currently
  running. On an `idle` row it starts fresh; on a `stopped` / `failed`
  row it is **Restart**, which wipes the tournament directory and runs
  from scratch behind a confirmation dialog citing the recorded game
  count (there is no resume).
- **Stop** -- only enabled when this row is the running tournament.
  Stops fastchess; the next Start wipes and restarts from scratch.
- **Open workspace** -- opens the workspace view (WinBox-driven). Valid
  in any state: live windows when running, frozen view when stopped /
  done.
- **Info** -- opens a human-readable dialog summarizing the tournament
  (engines list, time control, rounds, parallel games, games played
  vs total, ponder/resign/draw, opening book, created/started/stopped
  timestamps). Opening book displays as basename; full path on hover.
- **Remove** -- delete the saved tournament directory. Disabled while
  the tournament is running.

The list itself has a **Sort** menu (Name / Status / Created /
Started); the chosen sort persists across reloads via localStorage.

Plus a top-level action: **+ New Tournament**, which opens a dialog
containing:

- A **Name** field.
- An **engine-list builder**: two panes (Available <-> In tournament)
  with Add / Remove arrow buttons, plus Up / Down reorder buttons on
  the In-tournament pane (order matters for gauntlet seeding).
- The shared template form (see "Reusable form component" above),
  pre-filled from the saved defaults; per-tournament overrides are
  applied on top.

The dialog's primary action (**Create**) is enabled only when the
name is non-empty and at least two engines are picked. There is no
Cancel button -- the dialog's X handles dismissal -- and no
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
  Elo +/- 95% margin, plus the ordo-style joint-fit rating
  (`elo_ordo`) which covers N>=3 tournaments; anchored absolute
  ratings when reference engines carry a registry rating (see
  [pgn-elo-ordo.md](pgn-elo-ordo.md) and
  [engine-ratings-spec.md](engine-ratings-spec.md)). If SPRT is
  configured, a status line showing LLR, bounds, and decision status
  (H0 / H1 / continue). Source: PGN parsed by `pgn_stats`, refreshed
  as games complete.
- **Schedule** (1 window). List of completed games (PGN-derived) plus
  any in-progress games the server is tracking (proxy-derived once the
  pipeline is wired). Clicking a row attaches a Live game window --
  see below.
- **Event log** (1 window). Chronological text feed driven by the
  existing `EventBus`. Phase 1 surface is sparse (`tournament_status`
  and lifecycle: started / done / stopped / runner_crash) plus a
  forward of fastchess's own stdout (`Started game N (A vs B)`,
  `Finished game N: result`). Per-game UCI traffic is **not** sent
  to the event log; it flows through a separate per-engine
  subscription (see "Live observation pipeline" below). Persisted to
  `logs/wrapper.log` and `logs/fastchess.log`.
- **Live game** (0..N windows; opt-in). Each Schedule row in the
  "in-progress" group corresponds to **one engine process** (proxy)
  and exposes a *watch* button. The window subscribes to that one
  engine's UCI stream and renders the position from its POV (board
  oriented to the engine's color, that engine's eval/depth/PV, both
  clocks). See "Live observation pipeline" below for the data flow
  and the "Schedule rows = proxies" subsection for why this is
  single-side rather than per-game.

Static window count = 3 (Standings, Schedule, Event log). Live game
windows are opened on demand; the user attaches as many as they want
to follow.

Explicitly not in Phase 1: standalone PGN browser (Schedule's
row-click suffices), eval graphs (Phase 2).

---

### Live observation pipeline

This section captures the design for live game viewing -- the part of
the workspace that was deferred when Slice 8 shipped. Implementing it
involves three independently-shippable pieces.

#### Two pieces, well-bounded

1. **Wrapper around fastchess** -- the high-level manager
   (`FastchessRunner` + `Orchestrator`). Already shipped.
2. **Stdio proxy** -- a thin pipe that sits between fastchess and each
   engine binary. Forwards stdin/stdout transparently and broadcasts
   a copy of every line to the GUI server. The proxy stays a **dumb
   pipe**: no chess knowledge, no UCI parsing, no game state.

Splitting these responsibilities cleanly is what keeps the design
manageable.

#### Attach-to-engine, not attach-to-game

The Live game window subscribes to **one engine's proxy stream** --
not to a "game" abstraction. The user picks the engine they want to
follow; the window renders the board from that engine's POV.

This trades one feature ("see both engines' eval/PV in one window")
for a major simplification: there's no need to correlate two engine
streams into a "game" before showing anything. Each proxy is its own
unit; the user does the correlation by clicking. To see the other
engine's POV, attach a second window to the other proxy.

Everything needed to render the board is already in the engine's UCI
stream:

- `position startpos moves e2e4 e7e5 ...` -- fastchess sends the full
  move list every turn. Reconstruct the board with python-chess in
  one line; opponent's move comes for free.
- `go wtime ... btime ...` -- both clocks.
- `info depth N score cp ... pv ...` -- this engine's eval, depth, PV.
- `bestmove ...` -- the move this engine just played.

What you give up: the **opponent engine's** internal eval/PV/depth.
That's available from the opponent's proxy if the user attaches a
second window to it.

#### Schedule rows = proxies (single-side)

Shipped model: one Schedule row per active proxy (engine process),
labeled with its engine name, with a "watch" button that opens a
single-engine live window. Move-list-based pair detection runs in the
orchestrator for end-of-game reconciliation (see
`pgn-reconciliation.md`) but is not exposed in the Schedule UI.

To see both sides of a game the user opens two windows. A future
deterministic-pairing scheme (vendoring a fastchess fork to emit an
extended-UCI game-start announcement) could enable dual-PV windows; not
in scope until somebody asks.

#### Volume & high-concurrency considerations

UCI engines emit `info` lines continuously while searching. At
N=8-48 parallel games on a multi-core box, raw line-by-line POSTs
from each proxy to the server would peak in the thousands of
requests per second range.

Mitigations baked into the design:

- **Batch at the proxy.** Each proxy buffers and POSTs every ~50ms
  or every ~32 lines, whichever first. Drops request rate ~50x with
  no perceptible loss in liveness (50ms is below the human flicker
  threshold).
- **Posts run in a background worker thread.** The proxy's asyncio
  loop pumps engine stdio and *must not* block on HTTP. We initially
  called `urllib.request.urlopen` directly from the loop and
  observed multi-second stalls under `-concurrency > 1` because
  blocking the loop also stalled the stdin/stdout pipes between
  fastchess and the engine. Each proxy now drains a queue from a
  daemon worker thread; `add_line` / `flush` are non-blocking.
- **Per-proxy snapshot replay on subscribe.** The orchestrator keeps
  the latest `position` / `go` / `info` line per proxy and replays
  them when a WS subscriber connects. Without this, a window opened
  mid-game would render empty until the engine's next event -- which
  under long time controls (TC=720+8) can be >=10s away. The "info"
  snapshot is cleared on each new "position" so a stale eval doesn't
  paint against a fresh board.
- **Per-process proxy_id.** The proxy script mints its own uuid at
  startup (`p-<pid>-<uuid8>`) rather than receiving it via argv.
  fastchess reuses argv across slot processes when `-concurrency > 1`,
  so an argv-baked id would be shared between slots and silently
  collapse multiple distinct streams into one.
- **Throttle DOM updates.** Schedule re-renders at <=4 Hz even if
  the underlying state ticks faster.

If profiling at very high concurrency (the user's 48-core box) ever
shows the Python proxy is itself the bottleneck, the proxy can be
rewritten in C++ -- it's a self-contained process with a
language-agnostic protocol. Out of scope for now; documented as an
escape hatch.

##### Operational footnotes

- WS subscriber queues are bounded (`maxsize=512`) with drop-oldest
  on `QueueFull`. Sized for observed bursts of ~17-22 `info` lines/s
  per engine; if a slow consumer (e.g. a backgrounded browser tab)
  ever falls badly behind, lines get coalesced silently rather than
  blocking the producer. Acceptable for live observation.
- The snapshot only retains `info` lines that carry `score` or `pv`,
  filtering out `info string ...` debug noise and `info nodes/nps`-only
  lines some engines emit between depth iterations. Sturddle's
  meaningful info lines always carry score+pv, so snapshot accuracy
  is fine for the engines this project targets; engines with sparser
  output may need a more permissive filter.

#### What end-of-game looks like

The proxy doesn't classify. End-of-game is signaled by **pair
dissolution** alone: when one of a confirmed pair's proxies leaves
its FEN bucket -- typically via `ucinewgame` (entering its next
game) or `proxy_session_ended` (engine quit / fastchess closed it)
-- the pair's game is over.

`game_finished` is emitted immediately on dissolution with
`result="*"` / `termination="unknown"` / `game_n=null` -- the proxy
side cannot classify, and fastchess's `Started/Finished game N`
stdout cannot be unambiguously joined to a `pair_id` under
concurrency (same-name engines, no per-slot identifier).

A separate `game_reconciled` event upgrades the row when the
captured UCI move list is matched against fastchess's PGN output;
that event carries the real `result`, `termination` (PGN
[Termination "..."]), and `game_n`. Consumers that don't care about
the upgrade can ignore `game_reconciled`; the Standings window
still derives from the PGN directly. See `docs/pgn-reconciliation.md`
for the matching algorithm and edge cases.

#### Pair lifecycle: confirmation and dissolution

A "pair" is the orchestrator's runtime view of one in-flight game:
two `proxy_id`s playing each other, identified by a `pair_id` UUID.

**Confirmation.** Per-proxy UCI events register each engine into a
FEN bucket. When a bucket has exactly two entries with opposite
colors, the orchestrator confirms a pair, mints a `pair_id`, and
emits `proxy_paired`. Buckets >2 occur transiently because
fastchess feeds the same opening-book line to multiple slots; they
resolve as engines diverge past book.

**Dissolution.** A confirmed pair dissolves when either proxy
becomes orphaned from its FEN bucket -- typically because the proxy
forwarded `ucinewgame` to start its next game, or its session ended
(`proxy_session_ended`). Dissolution emits `proxy_unpaired` +
`game_finished` (with `result="*"`, `termination="unknown"`,
upgraded later by `game_reconciled` -- see "What end-of-game looks
like" above) and closes the per-pair WS subscribers with an `ended`
sentinel.

On terminal runner events (tournament done/stopped/failed), every
remaining open pair is dissolved through the same path so any open
game-WS subscribers receive their `ended` frame.

**Rejected alternatives.**

- *Parsing fastchess `Finished N` for the result.* Investigated and
  abandoned: the `pair_id <-> N` join is unsolvable when concurrency
  > 1 with same-name engines. fastchess's `Started/Finished` lines
  carry only `(white_name, black_name)` plus N; `name=` is shared
  across all parallel slots of one engine, and there is no per-slot
  identifier in the stdout protocol. Eval-based heuristics did not
  produce stable disambiguation.
- *Board-state inference from FEN.* Wrong for resignations
  (`-resign`), adjudicated draws (`-draw`), and time forfeits -- the
  board doesn't reflect the verdict.
- *PGN tail polling matched on `(white, black, N)`.* Same
  `pair_id <-> N` join failure as the stdout case above. The
  reconciliation feature uses PGN tail polling but matches on the
  full UCI move list instead, which disambiguates under concurrency
  even with same-name engines (see `docs/pgn-reconciliation.md`).

**Failure modes.**

- Same-engine-name pair candidate (book-line collision in a
  tournament with two distinct engines whose 4-bucket decays into a
  same-engine 2-bucket): rejected as phantom. See "Self-play
  (deferred)" below for why this rejection is conditional on the
  two-engine constraint.

#### Self-play (deferred)

Self-play tournaments -- engine running against itself, e.g. for
SPRT before/after a self-tune -- are not directly supported today.
Two layers block them:

1. **UI**: the engine-picker dialog (`mountEngineBuilder` in
   `web/app/tournaments.js`) filters already-picked engines out of
   the source pane and short-circuits `doAdd` on duplicate id. A
   user cannot select the same registry entry twice in one
   tournament.
2. **Orchestrator**: pair detection rejects same-engine-name
   candidates as phantoms from book-line collisions (4-bucket
   decays into a same-engine 2-bucket whose two proxies aren't
   actually playing each other in fastchess).

**Workaround that works today**: register the same binary twice in
the registry under distinct names (e.g. `Sturddle 2.5.0 (a)` and
`Sturddle 2.5.0 (b)`). They are distinct registry entries with
distinct ids; the picker accepts both; fastchess sees distinct
names; pair detection's same-name guard passes. Everything
downstream works.

The "real" fix is orchestrator-level disambiguation so the user can
pick one engine and have it play itself in one click. Lifting the
rejection requires the orchestrator to rename engine instances
before passing them to fastchess (e.g. `Engine #1` / `Engine #2`)
so fastchess's PGN headers carry distinct names. Pair detection
then works unchanged once the same-name guard is lifted.

TODO before native self-play UX ships:
- Orchestrator-level engine-name disambiguation (templates with
  duplicate engine cmds get suffixes).
- UI relaxation: allow picking the same engine twice.
- Tests covering self-play pair detection and dissolution.
- Lift the same-engine-name rejection in `_recompute_groups`.

Given the registry-level workaround, this is low priority.

### Stopped / done view

Same workspace. Live game windows are absent (they were live-only).
Standings, Schedule, and Event log remain with frozen content; the
Schedule's row-click PGN preview is the entry point for inspecting any
completed game.

A richer post-mortem (head-to-head matrix for round-robin, etc.) is
not in Phase 1; revisit after the basic workspace is in use.

### Layout persistence

Per-tournament: window positions/sizes/open state and the active
layout mode persist in localStorage under `sturddle:workspace:<id>`,
cleared when the tournament is deleted. Business rules (save points,
`_closed` semantics, restore-vs-default logic) and the test-scenario
matrix live in [tournament-workspace.md](tournament-workspace.md).

### Default layout (first open, no saved preference)

Deterministic placement, non-tiling (windows may overlap as concurrency
grows):

- Standings: top-left, ~40% width x ~50% height.
- Schedule: bottom-left, ~40% width x ~50% height.
- Event log: bottom-right, ~60% width x ~30% height.
- Live game windows: cascade from upper-right, each ~30% x ~45%,
  offset 30px per window.

Numbers are starting values; expect to tune once the workspace is in
front of real users.

---

## Server module shape

Two responsibilities -- **persistent state** and **subprocess
lifecycle** -- are decoupled via composition, not inheritance. Each
component is independently testable and the runner is the swap point
for a future second runner (cutechess); the store and orchestrator
are unaffected by that swap.

```
server/sturddle_view/tournament/
  __init__.py
  store.py           <- TournamentStore: on-disk layout, list/load/create/remove,
                       atomic state.json writes. No subprocess knowledge.
  pgn_stats.py       <- PGN -> Elo / SPRT computation (pure functions)
  runner.py          <- Runner protocol: start / stop / is_running
  fastchess.py       <- FastchessRunner implements Runner. Knows fastchess CLI,
                       process-group isolation, pipe draining. No on-disk
                       schema knowledge beyond paths it is handed.
  orchestrator.py    <- Orchestrator: composes Store + Runner. Thin coordinator;
                       owns the single-active invariant and startup
                       reconciliation. The public API called by REST/WS
                       handlers and the future CLI wrapper.
  proxy.py           <- stdio proxy + HTTP broadcast tap (per-process worker
                       thread for non-blocking POSTs); per-process proxy_id
```

### Single-active invariant lives in the orchestrator

After a server crash, `state.json` on disk may say `running` for a
tournament whose subprocess is gone. The store (a typed view over a
directory tree) cannot tell. The orchestrator can, because it owns the
runner.

On server startup, the orchestrator reconciles: any tournament whose
persisted status is `running` is marked `stopped` (reviving the
subprocess is not attempted). The recorded `games.pgn` is preserved for
inspection, but since there is no resume the next Start wipes it and
runs from scratch -- the same outcome as a user-initiated Stop then
Start.

#### Single-orchestrator-per-store assumption

Reconcile-on-startup is only sound under the assumption that **one
orchestrator instance owns the tournament store at any time**. A second
process pointed at the same `tournament_root` will, on its own startup,
mistake the first instance's live `running` rows for a crash and flip
them to `failed` -- silently corrupting the running tournament's
persisted status while the actual subprocess keeps chugging.

Phase 1 does not support multi-instance deployments and the spec does
not promise this. Single-user desktop deployments naturally satisfy the
invariant. The risk in practice is *accidental* shared roots -- most
notably tests that boot a `create_app` against the developer's real
platformdirs path. The test harness (`server/tests/conftest.py`)
redirects every default-path producer to a per-test tmp dir to keep
this from happening; any new default-path code must be wired into that
fixture.

TODO (post-Phase 1): if multi-instance ever becomes a use case (e.g.
a CLI runner started while the desktop server is up), add a pidfile or
flock-style guard at the store root so a second orchestrator either
refuses to start or skips reconcile. Today's implicit "don't do that"
contract is fine for the desktop product but should not leak into
shared-host configurations.

### Decoupling from the web layer

The orchestrator must be callable without any web/REST coupling -- its
public surface takes a tournament id (or a `TournamentConfig` for
creation) and a broadcast callback. This factoring is what enables the
Phase 1.5 CLI wrapper: it instantiates Store + Runner + Orchestrator
directly, no HTTP server involved.

REST/WS surface and exact method signatures are part of the
implementation plan, not this design doc; they will be added when the
UI/UX section is complete and we move to implementation.

---

## Testing policy

Rules enforced for every test shipped with this subsystem:

1. **No real fastchess or live engine binaries.** Tests must not spawn,
   call, or depend on `fastchess` or any UCI engine. Fake fastchess
   (an inline sleeping Python script) is injected via monkeypatch;
   proxy lines are driven through the HTTP `/internal/proxy` endpoint
   using `httpx.AsyncClient` so they reach the server's uvicorn event
   loop correctly.

2. **No warning suppression.** `pytest.ini` / `pyproject.toml` filters,
   `warnings.filterwarnings("ignore", ...)` calls, and `-W ignore` flags
   are prohibited. All tests must be clean with the default `pytest`
   warning settings.

3. **30-second wall-clock limit per test.** Tests that must wait for
   async state changes (e.g. pairing, WS subscription) do so with tight
   `deadline = time.time() + N` loops (N <= 5 s) or Playwright
   `wait_for_selector`/`wait_for_function` timeouts (<= 5 s).

4. **WebSocket transport: `wsproto`.** All uvicorn test servers must pass
   `ws="wsproto"` to avoid import-order warnings from the `websockets`
   library.

---

## No resume: Stop wipes

There is **no resume**. Stop transitions a tournament `running -> stopped`;
the next Start on a `stopped` (or `failed`) row **wipes the tournament
directory and runs from scratch**, behind a confirmation dialog that cites
the recorded game count. The API enforces this: `/start` on a non-idle row
returns `409 reason=wipe_required` unless called with `confirm_wipe=true`,
which wipes then starts.

Resume was prototyped on fastchess's native `-config` mechanism and then
removed (commit `91545bf`): the contract is too fragile across stop/resume
cycles. For >2 engines fastchess drops stats outright; for ==2 engines the
PGN / `config.json` / `state.json` counts drift across multiple cycles.
Rather than carry rewrite machinery to paper over that, Stop became
destructive across the board. Consequences:

- **PGN is ground truth.** Since every run starts fresh, `games.pgn` is the
  single source of game counts and standings. `config.json` is only
  sanity-checked against it (a warning on disagreement, gated on
  `status != running` so the autosave-lag window does not spam).
- **No rewrite code.** `rewrite_drop_partial_pairs`, `patch_config_json`,
  `state.games_played`, and `state.rewrites` were all deleted; `store.get`
  drops those now-unknown keys silently for forward-compat.

Future work:

- **Engine binary fingerprinting**: SHA-256 the engine binaries at
  tournament-creation time, store in the frozen template, warn (do
  not block) on Start if a binary's current hash differs. Prevents
  silent mixing of two engine versions into one Elo number.

---

## Open items

- **Engine renames don't propagate to live HVE display**: editing an
  engine's display name in the Roster does not update the Play
  perspective's side-panel label until the next page load. Tournaments
  intentionally freeze engine names at creation (by spec) and ignore
  later renames. The HVE side is not by design -- re-resolving from
  the registry on each new game would fix it; left as-is until
  someone cares.
- **Clone-and-edit tournament**: open the New Tournament dialog
  pre-filled from an existing tournament's frozen template + engines,
  with the name field cleared (or `"<name> (copy)"`). Saves the
  re-typing for repeat-style runs (same engines, same TC, different
  rounds/SPRT params). UI: "Clone" entry on the row's context menu
  / ribbon. Server: no new endpoint needed -- client just GETs the
  source tournament and POSTs to `/api/tournaments` with the
  pre-filled body. Engine entries are frozen snapshots so the clone
  inherits the source's engine state, not the registry's current
  state -- matches the freeze-at-create semantics already in the spec.

---

## Cross-proxy bestmove race

Each engine runs as a separate proxy process and POSTs UCI traffic
independently, so between proxies a `position` and the prior turn's
`bestmove` can arrive in either order. The client handles this
gracefully: `/api/chess/apply-move` returns **204** on a stale-FEN
illegal move, the client logs a `console.warn` and skips, and the next
`position` event self-corrects the board. Tradeoff: the move animation
is skipped for the affected half-move. A server-side reorder buffer was
tried and reverted (held clock updates and silently coalesced
mid-turn `info` lines).
