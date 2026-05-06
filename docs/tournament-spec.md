# Tournament Subsystem — Design Specification

Status: shipped (Phase 1).

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

- Queue / scheduling of tournaments (Phase 2).
- Attaching to a tournament started outside the GUI ("peek" / headless
  attach). The orchestrator is factored so this can be added later.

Pause/Resume is implemented: stopping a running tournament transitions
it to ``stopped``; starting it again resumes from where it left off
(per-tournament state survives across runs).
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

## Resource sanity checks (rescheck)

Block clearly-unsafe configurations before they reach fastchess. Lives
in `server/sturddle_view/tournament/rescheck.py` — pure (no orchestrator
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

- `cpu_load > logical_cores` → `oversubscribed`
- `ram_load_mb > 0.75 * total_ram` → `insufficient_ram`
- `pin_affinity AND cpu_load > physical_cores` →
  `affinity_exceeds_physical`

`allow_oversubscribe = true` downgrades `oversubscribed` and
`insufficient_ram` to warnings (toast at create time, no block). The
affinity check is a correctness gate (hyperthreading siblings can't
satisfy CPU pinning) and is **never** silenced by the flag.

A start-time rescheck failure surfaces the same way as a runner crash:
status flips to `failed`, `last_error.stderr_tail` carries the message,
and `last_error.rescheck` carries the structured detail
(`{reason, cpu_load, ram_load_mb, ...}`).

See [docs/tournament-concurrency-plan.md](tournament-concurrency-plan.md)
for the working notes (deferred slices, empirical findings).

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
artifacts." See **Resume after Stop** below for the concrete plan.

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

- A `Stop` mid-tournament still has correct standings — every game
  fastchess flushed to PGN counts; only the in-flight game is lost.
- A future Resume that appends to the same PGN yields correct
  cumulative numbers without special handling.
- Swapping to a different runner later (cutechess, custom) does not
  affect the math.

PGN parsing is a header-only line scan (regex over `[White ...]`,
`[Black ...]`, `[Result ...]`, `[Round ...]`) — `chess.pgn.read_game`'s
full move-tree parse is ~50× slower on multi-MB PGNs and we never
consume the moves for stats. Results are cached per-path keyed by
`(mtime, size)`; PGN is append-only so this is sound. Standard result
tags (`1-0`, `0-1`, `1/2-1/2`) drive game tallies. SPRT requires
pentanomial scoring (paired games); the formula is standard but must be
implemented in the wrapper, not delegated.

### Open: Elo display convention

Current behavior: `compute_standings` stores `elo_from_score(score_pct)`
on **each** engine independently. For a 2-engine tournament with
A scoring 52%, that produces `A.elo = +14, B.elo = -14`. The leader's
number matches `ordo -a 0 -A B`'s anchored output (which is the
*rating gap*). Storing the mirror on the trailer gives the impression
of a 28-Elo spread when the actual head-to-head gap is 14.

This is a presentation question, not a math bug. Options:

1. Show Elo only on the leader (trailer's `elo` = `None` or 0). Mirrors
   ordo exactly.
2. Halve the per-engine value so the displayed pair sums to the gap.
3. Keep current and disambiguate in the UI.

Deferred — touching this changes user-visible numbers and existing
tournaments' archived standings.

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
- **CPU Affinity** on/off (`pin_affinity`). Emits fastchess `-affinity`
  so each game-slot is bound to a fixed pair of cores. Recommended for
  SPRT / rating-list runs.
- **Oversubscribe** on/off (`allow_oversubscribe`). Permits CPU/RAM use
  to exceed host capacity; rescheck blockers (see "Resource sanity
  checks" below) downgrade to warnings. Also passes
  `-force-concurrency` to fastchess so it doesn't reject `-concurrency
  > nproc`. Don't use for SPRT.
- **Auto-folded** at create time by the client and stored in the
  template: `max_threads` and `max_hash_mb` (worst-case across the
  selected engines, or the global override if set). Persisted so the
  server can re-run the rescheck at start time without consulting the
  engine registry.
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

- **Start / Resume** — only enabled when no tournament is currently
  running. The icon switches between *play* (idle) and *forward-step*
  (resume from a previously stopped tournament).
- **Pause** — only enabled when this row is the running tournament.
  Stops fastchess; the next Start resumes from the same state.
- **Open workspace** — opens the workspace view (WinBox-driven). Valid
  in any state: live windows when running, frozen view when stopped /
  done.
- **Info** — opens a human-readable dialog summarizing the tournament
  (engines list, time control, rounds, parallel games, games played
  vs total, ponder/resign/draw, opening book, created/started/stopped
  timestamps). Opening book displays as basename; full path on hover.
- **Remove** — delete the saved tournament directory. Disabled while
  the tournament is running.

The list itself has a **Sort** menu (Name / Status / Created /
Started); the chosen sort persists across reloads via localStorage.

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
  Elo ± 95% margin. Elo and margin are emitted only for head-to-head
  (N=2) tournaments; with N≥3 the score% column is "vs field" (mixed
  strengths) and the Elo column shows "—" until a multi-engine rating
  estimator lands (see Future work). If SPRT is configured, a row at
  the top showing LLR, bounds, and decision status (H0 / H1 /
  inconclusive). Source: PGN parsed by `pgn_stats`, refreshed as games
  complete.
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

#### Schedule rows = proxies (single-side; pairing deferred)

Originally specified as automatic pair detection on the server (a
`pair_index` matching proxies by shared move list). **Tried and
removed.** What we shipped: one Schedule row per active proxy
(engine process), labeled with its engine name, with a "watch"
button that opens a single-engine live window.

Why pairing turned out untenable in Phase 1:

- **Same-opening overlap under concurrency.** With `-concurrency > 1`
  fastchess runs the same opening line in parallel game-slots
  (book-driven, intentionally; SPRT pairs play each opening twice
  with colors swapped). All four proxies briefly hold the same
  `(move_list, ply)` state. Strict same-key matching produces
  ambiguity; prefix-relaxed matching produces phantom cross-pairs
  (e.g. two processes of the *same engine* from different slots
  paired with each other).
- **No end-of-game UCI signal.** Engine processes are reused across
  rounds (UCI has no `endgame`; just `ucinewgame` for the next).
  A locked pair stays locked even after fastchess re-pairs the
  engines for the next round; the index silently keeps stale
  partnerships.
- **Strict ply-difference checks flap.** Forcing `|ply_a - ply_b| ≤ 1`
  to guarantee opposite side-to-move yields constant
  observe / dissolve flapping under normal batching, because one
  side often races ahead by several plies before the other catches up.

We tried strict pairing, prefix pairing, and uniqueness-disambiguated
pairing. All three failed in different ways. The path forward
(documented; not in scope for Phase 1) is **deterministic** pairing:
vendor a fastchess fork, emit an `extended UCI` announcement at
game-start (`sturddle game-start slot=N white=X black=Y`), have the
proxy intercept and forward to the orchestrator. With authoritative
pairings, the dual-PV window described in the original "Attach to
engine, not to game" trade-off becomes trivial.

Until then we ship the simple model: one row per proxy, one window
per click. To see both sides of a game the user opens two windows.

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
  mid-game would render empty until the engine's next event — which
  under long time controls (TC=720+8) can be ≥10s away. The "info"
  snapshot is cleared on each new "position" so a stale eval doesn't
  paint against a fresh board.
- **Per-process proxy_id.** The proxy script mints its own uuid at
  startup (`p-<pid>-<uuid8>`) rather than receiving it via argv.
  fastchess reuses argv across slot processes when `-concurrency > 1`,
  so an argv-baked id would be shared between slots and silently
  collapse multiple distinct streams into one.
- **Throttle DOM updates.** Schedule re-renders at ≤4 Hz even if
  the underlying state ticks faster.

If profiling at very high concurrency (the user's 48-core box) ever
shows the Python proxy is itself the bottleneck, the proxy can be
rewritten in C++ — it's a self-contained process with a
language-agnostic protocol. Out of scope for now; documented as an
escape hatch.

##### Operational footnotes

- WS subscriber queues are bounded (`maxsize=512`) with drop-oldest
  on `QueueFull`. Sized for observed bursts of ~17–22 `info` lines/s
  per engine; if a slow consumer (e.g. a backgrounded browser tab)
  ever falls badly behind, lines get coalesced silently rather than
  blocking the producer. Acceptable for live observation.
- The snapshot only retains `info` lines that carry `score` or `pv`,
  filtering out `info string …` debug noise and `info nodes/nps`-only
  lines some engines emit between depth iterations. Sturddle's
  meaningful info lines always carry score+pv, so snapshot accuracy
  is fine for the engines this project targets; engines with sparser
  output may need a more permissive filter.

#### What end-of-game looks like

The proxy doesn't classify. The orchestrator combines two signals:

- **UCI stream** (per-proxy): `ucinewgame` marks a game boundary.
  Proxy disconnect (engine quit / fastchess closed it) marks a hard
  end.
- **fastchess stdout** (tournament-wide, authoritative): the
  `Started game N (A vs B)` and `Finished game N (A vs B): result`
  lines from `-output format=fastchess` carry the real result and
  termination reason.

The result label (`1-0` / `0-1` / `1/2-1/2`) comes from the
`Finished` line — not from PGN polling and not from board-state
inference. PGN remains the source for the Standings window.

#### Pair lifecycle: confirmation and dissolution

A "pair" is the orchestrator's runtime view of one in-flight game:
two `proxy_id`s playing each other, identified by a `pair_id` UUID.
Pairs need both *confirmation* (so live windows can attach) and
*dissolution* (so windows close with the right result).

**Confirmation.** Per-proxy UCI events register each engine into a
FEN bucket. When a bucket has exactly two entries with opposite
colors, the orchestrator confirms a pair, mints a `pair_id`, and
emits `proxy_paired`. Buckets >2 occur transiently because
fastchess feeds the same opening-book line to multiple slots; they
resolve as engines diverge past book.

**Game-N stamping.** fastchess's stdout assigns each game a
monotonic integer N. On `Started game N (A vs B)` the orchestrator
queues `(N, white=A, black=B)`. The next pair confirmation whose
sides match `(A, B)` is stamped with N (FIFO). This makes the
`pair_id ↔ N` mapping deterministic at any concurrency.

**Dissolution (Option B — deferred).** `Finished game N` is the
sole authoritative trigger for ending a pair: it carries the real
result and termination, so it drives `proxy_unpaired` +
`game_finished` and closes the per-pair WS subscribers with the
result in the `ended` sentinel.

UCI-side end-of-game (a `ucinewgame` from a confirmed proxy, or a
`proxy_session_ended`) does *not* emit termination events on its
own. It only marks the pair *pending dissolution*; the `Finished`
line completes the cleanup. If `Finished` never arrives (proxy
crashed before fastchess flushed, runner shutdown), a forced-
dissolve path drains pending pairs with `result=unknown` so windows
don't hang.

**Why deferred.** The alternative is "first signal wins, emit a
corrective second event if the real result arrives later." Under
high concurrency that produces interleaved `unpaired` /
`game_finished` events for already-dissolved pairs, pushing race
reconciliation into the client. Deferral keeps the contract simple:
exactly one terminal event per pair, carrying the real result, in
order. The state cost is bounded by `concurrency` (≤ a few dozen
pending entries).

**Rejected alternatives.**

- *Board-state inference from FEN.* Wrong for resignations
  (`-resign`), adjudicated draws (`-draw`), and time forfeits — the
  board doesn't reflect the verdict.
- *PGN tail polling for results.* Adds a poller and doesn't solve
  pair-id mapping under concurrency (PGN flush order is per-game,
  not per-slot).
- *Match results to pairs by `(white, black)` alone, without N.*
  Under concurrency multiple in-flight pairs share `(white, black)`
  in either direction; pair-id assignment becomes ambiguous.

**Failure modes.**

- Malformed `Started`/`Finished` line (fastchess version skew):
  log a warning and fall through. Forced-dissolve on shutdown
  cleans up.
- `Finished N` for unknown N (pair never confirmed — book-line
  collision held bucket >2 for the whole game): log + drop. No UI
  window existed to update.
- `ucinewgame` arrives before `Started N` is queued (theoretical
  reorder): pair confirmation can't stamp N; falls through to
  forced-dissolve.
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
so fastchess's `Started N (... vs ...)` and PGN headers carry
distinct names. Pair detection then works unchanged, and the FIFO
match in `_stamp_pair_game_n` keys correctly off the disambiguated
names.

TODO before native self-play UX ships:
- Orchestrator-level engine-name disambiguation (templates with
  duplicate engine cmds get suffixes).
- UI relaxation: allow picking the same engine twice.
- Tests covering self-play pair detection, dissolution, and
  Started/Finished FIFO matching.
- Lift the same-engine-name rejection in `_recompute_groups`.

Given the registry-level workaround, this is low priority.

#### fastchess output format dependency

Pair lifecycle (game-N stamping, dissolution, result/termination)
parses fastchess's stdout format produced by
`-output format=fastchess`. The relied-upon lines:

- `Started game <N> [of <M>] (<white> vs <black>)` -- queues N for
  the next confirmation matching `(white, black)`.
- `Finished game <N> [of <M>] (<white> vs <black>): <result>
  {<termination>}` -- dissolves the pair stamped with N and emits
  the authoritative `game_finished` event.

Regexes live in `server/sturddle_view/tournament/orchestrator.py`
(`_FASTCHESS_STARTED_RE`, `_FASTCHESS_FINISHED_RE`). They tolerate
the optional ` of M` segment but are otherwise strict on the
literal " vs " separator and the result tokens
(`1-0` / `0-1` / `1/2-1/2` / `*`).

This is a load-bearing dependency on an external tool's output
format. fastchess targets cutechess-cli compatibility, which
suggests format stability, but the contract is not declared. A
silent format change in a fastchess update would cause `Finished N`
lines to fail parsing -- pairs would never dissolve via the normal
path and would only clear at tournament terminal via
`_force_dissolve_pending` with `result="*"`. The failure is visible
(WARNING-level "had no confirmed pair" log lines fire on every
unrecognized Finished) but silent at the UI level until the user
notices windows accumulating.

Mitigation if it happens: update the regexes; add a unit test
pinned to fixture stdout from the new fastchess version.

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
  proxy.py           ← stdio proxy + HTTP broadcast tap (per-process worker
                       thread for non-blocking POSTs); per-process proxy_id
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
   `deadline = time.time() + N` loops (N ≤ 5 s) or Playwright
   `wait_for_selector`/`wait_for_function` timeouts (≤ 5 s).

4. **WebSocket transport: `wsproto`.** All uvicorn test servers must pass
   `ws="wsproto"` to avoid import-order warnings from the `websockets`
   library.

---

## Resume after Stop (shipped)

Clicking **Start** on a `stopped` tournament resumes from where it
left off using fastchess's native `-config` mechanism. Background:

The fix uses fastchess's native resume mechanism (`-config`). Research
findings (verified against the fastchess source tree):

- Fastchess maintains state in a JSON file (default `config.json`,
  written by `BaseTournament::saveJson()`). The file contains the
  tournament config, engine list, and a `stats` map (W/L/D + penta per
  engine pair).
- **Save cadence is governed by `-autosaveinterval N`**, which defaults
  to **20 games**. With the default, killing fastchess after fewer than
  20 completed games loses *all* progress for resume purposes (the
  cfg.json was never written). Pass `-autosaveinterval 1` so every
  game is durable.
- On startup with `-config file=<path>`, fastchess loads that file via
  `loadJson()` (`app/src/cli/cli.cpp:433`), seeds the scoreboard via
  `setResults()` (`tournament.cpp:331`), and computes
  `initial_matchcount_` as the sum of W+L+D across pairs
  (`tournament.cpp:33`).
- The opening book auto-rotates by that count
  (`opening_book.cpp:23`: `offset_ = start - 1 + initial_matchcount /
  games`), so fastchess fast-forwards through the schedule to the
  exact next pair to play.
- The CLI splits read and write paths: `-config file=<path>` is the
  *read* (load on resume), `-config outname=<path>` is the *write*
  (where fastchess saves snapshots). Initial run passes only
  `outname=`; resume runs pass both `file=` and `outname=` (same
  path, so the snapshot is overwritten in place). Passing `file=` to a
  non-existent file is a fatal error, so the orchestrator must check
  before adding the flag.
- **Fastchess does not read `games.pgn` on resume.** Stats come from
  `config.json` only. A truncated or `*`-result trailing game in the
  PGN is invisible to fastchess; it will simply replay any pair that
  hadn't yet been recorded in `config.json`.
- **Resume produces at most one duplicate game.** Even with
  `-autosaveinterval 1`, the save fires after the *post-game*
  bookkeeping inside fastchess; if SIGKILL lands between the PGN
  append for game N and the cfg.json write for game N, the resumed
  run replays the pair that produced game N, leaving two PGN entries
  for the same `(round, white, black)`. Verified empirically: see the
  smoke test in `/tmp/fc-smoke/` (a planned 8-game match interrupted
  after 3 PGN games but only 2 saved produced 9 total PGN entries on
  completion).
- **Resume is statistically valid but NOT byte-deterministic.**
  `-srand` only seeds fastchess's pairing/opening-shuffle PRNG; it
  does not seed the engines. With ultra-fast TC the same pair on the
  same opening produces a different game across runs because engine
  search depends on wall-clock timing. SPRT/Elo correctness is
  unaffected (pairs remain independent samples), but anyone expecting
  bit-identical replay will be disappointed.

Implementation (shipped):

1. **Pin a seed at tournament-creation time.** Add a `seed` field
   (uint64) to the frozen template; generate at create-time with
   `secrets.randbits(64)`. Pass `-srand <seed>` on every Start. This
   does NOT make games reproducible (engines are not seeded); it only
   makes the *opening order* stable across runs when the book is
   shuffled. Schema change is acceptable (early dev, no production
   data).
2. **Pass `-autosaveinterval 1`** on every Start. Default is 20,
   which would lose up to 19 games of resume progress on Stop.
3. **On Start, conditionally pass `-config`:**
   - First run (`<state.json>` does not exist):
     `-config outname=<state.json>`.
   - Resume run (`<state.json>` exists):
     `-config file=<state.json> outname=<state.json>`.
   The orchestrator checks file existence before composing the flag.
   `<state.json>` lives in the tournament dir alongside `games.pgn`.
4. **Do not delete `<state.json>` on stop.** Just leave the working
   dir intact.
5. **Dedup PGN games on parse.** In `compute_standings()` and
   `compute_sprt()` (see
   `server/sturddle_view/tournament/pgn_stats.py`), key games by
   `(Round, White, Black)` and keep only the last occurrence per
   key. This handles the at-most-one duplicate game produced by
   resume after a kill that lands between PGN-append and
   cfg.json-write. Same pass should also drop games without a
   definitive `[Result]` (handles the rare `*`-tail case from a
   killed in-flight game). Independent of resume — ship anytime.
6. **Disable Start when `status === "done"`** — already in place
   (web/app/tournaments.js:142 gates on
   `running` and `done`). Listed for completeness only; no change
   needed.
7. **Graceful stop**: send SIGTERM, wait ~2 s for fastchess's
   `~BaseTournament` to flush state (and ideally fire a final
   `saveJson()`) and join the engine pool, fall back to SIGKILL only
   on timeout. Reduces — but does not eliminate — duplicate-game
   risk on resume, since SIGTERM lets fastchess finish any
   in-flight save. PGN truncation risk (an unterminated tail game)
   also drops to near-zero. Step 5's dedup/filter is the
   correctness backstop; this step is a quality-of-life
   improvement.

Considered and rejected:

- **Counting completed games ourselves** (parse PGN, pass
  `-openings start=N+1` and reduced `-rounds`): more code, opaque
  semantics for gauntlet/random-order, and we re-derive what
  fastchess already tracks. Use `-config` instead.
- **Defensive `try/except` around python-chess parsing** of the last
  PGN game to handle SIGKILL truncation: low value given the `[Result]`
  filter already handles the "no result" case, and graceful stop
  handles the truncation case. Not worth the complexity or per-parse
  cost.

Future work (not part of the resume effort):

- **Engine binary fingerprinting**: SHA-256 the engine binaries at
  tournament-creation time, store in the frozen template, warn (do
  not block) on Start if a binary's current hash differs. Prevents
  silent mixing of two engine versions into one Elo number.
- **Multi-engine ratings (N≥3)**: replace the current "no Elo for
  N≥3" placeholder with a proper rating estimator (Bradley-Terry /
  Ordo-style iterative MLE) that yields per-engine ratings *and*
  per-engine 95% margins from the pairwise W/L/D matrix. Until then
  the Standings table renders "—" in the Elo column for N≥3.

---

## Open items

- **UI/UX**: workspace window inventory and default layout (live
  standings, per-game live boards, SPRT progress, event log). Pending
  follow-up discussion.
- **Implementation plan**: server modules' public APIs, REST/WS event
  shapes, phased landing order. To be drafted after UI/UX is settled.
- **Engine renames don't propagate to live HVE display**: editing an
  engine's display name in the Roster does not update the Play
  perspective's side-panel label until the next page load. Tournaments
  intentionally freeze engine names at creation (by spec) and ignore
  later renames. The HVE side is not by design — re-resolving from
  the registry on each new game would fix it; left as-is until
  someone cares.
- **Clone-and-edit tournament**: open the New Tournament dialog
  pre-filled from an existing tournament's frozen template + engines,
  with the name field cleared (or `"<name> (copy)"`). Saves the
  re-typing for repeat-style runs (same engines, same TC, different
  rounds/SPRT params). UI: "Clone" entry on the row's context menu
  / ribbon. Server: no new endpoint needed — client just GETs the
  source tournament and POSTs to `/api/tournaments` with the
  pre-filled body. Engine entries are frozen snapshots so the clone
  inherits the source's engine state, not the registry's current
  state — matches the freeze-at-create semantics already in the spec.
