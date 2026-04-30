# SturddleView — Design Specification

## Overview

A browser-based chess GUI supporting human vs engine play and real-time observation
of headless engine tournaments. Designed as a personal dev tool, lightweight, with
minimal install footprint. Portable across machines via browser; optionally wrapped
in a native window via PyWebView.

---

## Goals

- Human vs engine play
- Run and manage engine tournaments (via fastchess/cutechess-cli)
- "Peek" into already-running headless tournaments across machines
- Native-feeling desktop experience without Electron/Node
- Extensible: agent/AI plugin interface designed in from the start
- **Cross-platform**: Linux, macOS, Windows 11 — no Linux-only or POSIX-only code paths in the server, client, orchestrator, or proxy. Where stdlib doesn't cover a need portably, prefer a small cross-platform dependency (e.g. `psutil`) over `/proc` parsing or platform `ioctl`s.

---

## Architecture

```
┌─────────────────────────────────────────────┐
│           Tournament Orchestrator            │
│  - fastchess/cutechess facade                │
│  - injects stdio proxies per engine          │
│  - normalizes output upstream                │
└─────────────┬───────────────────────────────┘
              │
┌─────────────┴───────────────────────────────┐
│              Stdio Proxy (per engine)        │
│  - generic, protocol-agnostic                │
│  - forwards stdin/stdout transparently       │
│  - broadcasts copy of all traffic            │
└─────────────┬───────────────────────────────┘
              │
┌─────────────┴───────────────────────────────┐
│              GUI Backend Server (Python)     │
│  - WebSocket: live events broadcast          │
│  - REST: settings, control, history          │
│  - Human vs engine: python-chess UCI         │
│  - Agent interface: consume + produce events │
└─────────────┬───────────────────────────────┘
              │ WebSocket + REST
┌─────────────┴───────────────────────────────┐
│              Browser Client                  │
│  - cm-chessboard (git submodule)             │
│  - Tournament observer / peek view           │
│  - Human vs engine mode                      │
│  - Agent/analysis panels                     │
└─────────────────────────────────────────────┘
```

---

## Components

### 1. Tournament Orchestrator

- Presents a unified facade over fastchess, cutechess-cli, and compatible tournament managers
- Rewrites engine paths in tournament config to point at stdio proxies
- Launches tournament manager as subprocess, or attaches to an already-running one
- On attach: reconstructs current tournament state from existing PGN/log output ("catch-up"), then switches to live proxy stream
- Exposes a launcher wrapper: `launch [--gui]` — optionally opens GUI immediately or allows attaching later
- Tournament manager lifecycle (crashes, restarts) is detected and reported upstream

### 2. Stdio Proxy

- Generic stdio forwarder — no chess or UCI knowledge
- Sits between tournament manager and each engine binary
- Forwards: `tournament_manager stdin → engine stdin`, `engine stdout → tournament_manager stdout`
- Broadcasts a copy of all traffic to the GUI backend server
- One proxy process per engine instance (lightweight, I/O bound, Python is sufficient)
- Proxy crash: wrapper detects and attempts restart without disturbing the game
- Implemented in Python; C++ extension only if profiling reveals a bottleneck (unlikely)

### 3. Human vs Engine

- Managed directly via `python-chess` (`chess.engine` async UCI interface)
- No tournament manager or proxy involved
- Handles: time controls, engine info streaming (depth, score, PV, NPS), move validation
- Feeds the same GUI backend event bus as the tournament path
- Supports: new game, resign, take-back (linear history to start; variation tree left open)

TODO: surface "engine answered from book" in the engine info panel.
When an engine plays from its opening book it returns `bestmove` with no
preceding `info` lines — the panel currently shows stale/empty values
with no explanation. Naive detection ("no info_emitted before bestmove")
is unreliable: very fast searches at low depth could in principle do the
same. Need to investigate how fastchess / cutechess-cli identify book
moves (likely: external book the GUI manages itself, or a UCI extension)
before implementing. Postponed until we know the right way.

### 4. GUI Backend Server

Python-based server, three API areas:

#### `/settings` — REST CRUD
Server-side, persisted to a JSON file under the OS user-config directory
(`platformdirs.user_config_dir("sturddle-view")/settings.json`). Apply-on-change
semantics: every PUT writes to disk immediately and broadcasts a
`sturddle:settings-changed` event to clients.

Settings that can't be applied to an in-progress game (currently:
`human_side`, `tc_initial_seconds`, `tc_increment_seconds`) are accepted
and persisted, and the Play perspective surfaces a "will apply on next
game" toast on drift. The current game is unaffected.

TODO (when tournaments land): decide policy for settings that conflict
with a running tournament — block the change, queue it, or freeze the
relevant subset of settings while the tournament is in progress. The
current "accept and notify" policy is fine for the single-game case but
may not suffice once a tournament owns the engine for hours.

Persisted fields:
- `pgn_autosave` (toggle), `pgn_dir` (path) — save games as PGN
- `tc_initial_seconds`, `tc_increment_seconds` — default time control
- `human_side` — `"white" | "black" | "random"`
- `allow_takeback`
- `engine_path` — fallback engine when no engine selected from registry

Excluded from persistence: `token`, `host`, `port`, `auth_disabled`, `web_dir`.
These come from CLI flags / env vars.

Future fields (per spec, not yet implemented): opening book paths, tournament
config defaults.

Client-side (localStorage, server-agnostic):
- Active perspective, per-perspective layout (window positions, sizes)
- Theme (deferred), piece set, board colors (deferred)
- Sound on/off (deferred)

#### `/engines` — REST CRUD
- List, add, update, remove registered engines
- `POST /engines/{id}/select` — set the active engine
- Engine path validation on add (must exist, must be a regular file, must be
  executable on POSIX)
- Persisted to `engines.json` in the same user-config directory; selection
  survives restart
- Each registry entry stores:
  - `id`, `name`, `path` (existing fields)
  - `options` — flat `{name: value}` map of user-overridden UCI options
    (only keys whose value differs from the engine's advertised default
    are stored; lets us round-trip "use defaults")
  - `option_schema` — cached UCI option list captured at engine
    registration time, indexed by option name. Each entry: `{type, default,
    min?, max?, vars?}` where `type` ∈ `spin | combo | check | string | button`.
    Used to render the per-engine dialog without re-spawning the engine
    every time.
- `POST /engines/{id}/refresh-schema` — re-spawn the engine briefly to
  re-capture its UCI option list (e.g. after the user upgrades the binary).

#### `/fs` — REST
- Directory listing for the file picker dialog (engine binary, PGN dir, etc.)
- Cross-platform (Windows drives, POSIX paths) via stdlib `pathlib`
- Token-gated; permissions are whatever the server process has — no allowlist

#### `/game` — REST
- Submit human move (snap-back on illegal: server publishes current state so
  client UI re-syncs)
- New game, resign, take-back
- Tournament control: start, stop, pause, attach (Phase 2)

#### `/ws` — WebSocket
- All live events: engine info (depth, score, PV, nodes, NPS), board state,
  clock ticks, tournament standings, game results, opening identification,
  tablebase results, agent annotations, system errors.
- Events are **structured and typed** (not ad-hoc strings) — required for agent consumption
- On client reconnect: server rebroadcasts current state
- Client implements exponential backoff reconnect

### 5. Browser Client

- Vendored libraries (offline, pinned, no CDN at runtime):
  - `cm-chessboard` — chess board rendering and move input
  - Web Awesome — UI components (dialogs, inputs, tabs, switches, icons)
  - WinBox — draggable/resizable windows (used by Observe sub-view)
  - Font Awesome Free SVGs — icon library used by Web Awesome
  - `chess-openings` (Lichess) — opening identification dataset
- **Reusable `GameView` component**: composes board + clocks + move list +
  engine info + opening line + tablebase result. Used by Play perspective and
  (Phase 2) inside Observe windows. Each aspect can be hidden/shown
  independently.
- Two perspectives in Phase 1: Play, Engines. (See "Perspectives" below.)
- Agent/analysis panels: designated UI areas agents can populate (Phase 2).
- Per-perspective layout state persisted in localStorage.

---

## Authentication

Single shared secret token, generated at server startup or via config:

```
ws://hostname:port/ws?token=<secret>
http://hostname:port/ui?token=<secret>
```

- No user accounts or session management
- Local same-machine PyWebView mode: token auto-filled, effectively transparent
- Share URL+token to allow remote peek access
- `--no-auth` CLI flag disables token check entirely (trusted-LAN dev convenience).
  Banner makes this explicit at startup. **Never use on untrusted networks.**

When the WebSocket disconnects, the client visibly disables the Settings gear
and the Engines perspective nav button. If the user is on Engines when the
connection drops, they are auto-routed back to Play. Play remains usable for
last-known state inspection.

---

## Logging

- Centralized logger config; rotating file handler under
  `platformdirs.user_log_dir("sturddle-view")/sturddle-view.log` (5 × 2 MB).
- `--debug` CLI flag flips stderr level to DEBUG; file always at DEBUG.
- Server modules use `logging.getLogger(__name__)`. Clients use `console`
  for in-browser inspection.
- TODO: surface the rotating server log in the GUI (e.g. a Settings panel
  that fetches the tail of `sturddle-view.log` on demand). Replaces the
  earlier client-side "Debug event log" in the Play perspective, which was
  removed for being low-value.

---

## PGN Storage

- Each game is saved as its own file when `pgn_autosave` is enabled.
- Filename: `YYYYMMDD-HHMMSS-{game_id}.pgn` under `pgn_dir`.
- One game per file (not appended) — easier to delete/share individually.
  Bulk import to other tools is `cat *.pgn > all.pgn` away.
- Headers: Event (Sturddle View — Human vs Engine), Site, Date, White, Black,
  Result, Termination, TimeControl. White/Black are "Human" and the engine's
  UCI-advertised name (falling back to its binary filename), side-correct.
- Resign produces a result + `Termination=resignation`; flag fall produces
  `Termination=timeout`.
- Empty games (no moves) are not saved.

---

## Native Desktop Mode

- PyWebView wraps the browser client in a native OS window (no Electron, no Node)
- Uses OS-native webview: WebView2 (Win11, pre-installed), WebKit (macOS), WebKitGTK (Linux)
- Python server and PyWebView share the same process
- Headless mode: skip PyWebView, point any browser at the server URL
- Same server and client code for both modes

### Install (Windows 11)

```
pip install pywebview python-chess <package-name>
```

No additional runtime required on Windows 11 (WebView2 ships with OS).

---

## Agent / Plugin Interface

Agents are first-class participants, not bolts-on:

- Any agent subscribes to the WebSocket event stream like any other client
- Dedicated agent API endpoint: agents push annotations, suggestions, commentary back to server
- Server broadcasts agent output to all clients
- Client UI has designated panels for agent content (analysis, commentary, teacher mode)
- Use cases envisioned: game analysis, move suggestions, teaching/explanation, post-game review
- Voice control / speech interface: deferred to later phase, designed to plug into the same event bus

---

## UI / Frontend

Aesthetics and UX are first-class concerns; the GUI is not just a thin debug surface for the backend. Visual polish, consistent theming, and predictable interactions matter as much as functional correctness.

TODO: color-palette tightening pass. Several follow-ups deferred from
the current "minimum-customization" baseline:
1. Resign-button hover regression — `--wa-color-danger-fill-loud` is
   muted, but hover/active/focus states pull from non-overridden tokens
   and flash back to bright orange. Either override the hover/active
   tokens or accept the flash.
2. File-picker dialog Up button (`web/app/dialogs.js:195`) is the lone
   `appearance="outlined"` button left after the filled-by-default
   sweep. Either drop the override for parity, or keep and document why.
3. Brand button (`variant="brand"`) is too loud at default WA blue.
   Consider a paler blue or teal swap (likely via `--wa-color-brand-*`
   overrides analogous to the danger ones).
4. Top nav (Play / Engines tabs) uses a teal outline + glow that reads
   inconsistent with the rest of the now-quiet palette. Investigate
   what's setting the active-tab style and quiet it down — the project
   tenet is minimal use of color.

### Hard constraints

- **No build step.** No TypeScript files served, no rollup/vite/webpack/tsc in the dev or run loop. Browser-loadable ES modules and CSS only.
- **No CDN at runtime.** Every asset (JS, CSS, fonts, icons, sprites) must resolve from the local server. Vendored libraries ship as files in `web/vendor/`.
- **Same code in browser and PyWebView.** No mode-specific DOM forks.
- **Cross-platform aesthetics.** Layout and typography must look correct on Linux/macOS/Windows 11. No platform-specific font assumptions.
- **Responsive.** Desktop-first, but small-window and tablet-portrait usable. No fixed-pixel layouts that break under resize.
- **Offline-first.** No assumption of internet reachability — the GUI is operational on an air-gapped LAN.

### Component vocabulary

The UI composes from a fixed set of primitives. Adding a new ad-hoc widget should be rare; if a screen needs something not in this list, the list itself is what gets extended.

- **Toast / notification** — transient, non-blocking. Used for success confirmations, soft warnings, agent-emitted info that doesn't demand attention.
- **Message box (alert)** — modal, single dismiss button. Errors and acknowledgements that block until the user reads them.
- **Confirmation dialog** — modal, two or more buttons. "Are you sure?" gates before destructive actions.
- **Form dialog** — modal, contains inputs and validation. Used for "edit engine," "configure tournament," etc.
- **Floating panel (window)** — non-modal, draggable, resizable, optionally minimizable. Multiple panels can coexist on screen. Used for live engine info per game, agent commentary, tournament observer per-game views.
- **Docked panel** — non-modal, attached to a screen edge or a region of the layout. Used for primary navigation, the active board, persistent status.
- **File browser dialog** — modal, lets the user pick a path on the **server's** filesystem (engine binary, PGN save dir, opening book, tablebase root). Backed by a server endpoint that enumerates directories — the browser cannot read the server's filesystem directly.
- **Inline form controls** — buttons, inputs, selects, switches, checkboxes, tabs, accordions, tooltips. Themed consistently.

### Interaction model

- **Action chaining via Promises.** All dialog primitives return Promises that resolve to the user's choice (or `null` for cancel). Application code chains naturally: `await confirm(...)` → `await api(...)` → `toast(...)`. This is the "monadic" pattern — Promises are the substrate, no custom monad is needed.
- **One blocking modal at a time.** Floating panels are unrestricted; modal dialogs are stacked one-deep. New modal requested while one is open: queue or replace, never overlap.
- **Keyboard-first wherever practical.** Esc closes modals. Enter confirms default action. Tab cycles within a modal's focus trap. Drag handles are mouse/touch only — keyboard users get an alternate "open in dialog" view.
- **Persisted layout.** Floating-panel positions, sizes, and open/closed state survive reload (localStorage, scoped per perspective).
- **No silent failures.** Every API error surfaces as either a toast (recoverable) or a message box (blocking). The event log is a debug aid, not a substitute for explicit feedback.

### Perspectives

The app is organized into **perspectives** — distinct top-level layouts tuned to different activities. Top-bar nav switches between them. A perspective owns its own root layout container; switching perspectives swaps the root content but leaves dialogs, toasts, and global state untouched.

Two perspectives ship in Phase 1:

#### Play perspective (focused, fixed layout)

For human vs engine play. Calm, distraction-free.

- Centered board, large but bounded (max ~85vh).
- Fixed side rail (right on wide screens, below on narrow): clock, move list, engine info during search.
- Top of side rail: compact game-control bar — New Game, Take-back, Pause, Resign.
  - Pause is enabled only on the human's turn (engine is idle then); it
    stops the clock and rejects moves until resumed.
- No floating windows. The board is the focus; nothing should float over it during play.
- One docked panel toggle: an optional "agent" tab in the side rail (Phase 2).

#### Engines perspective (workspace + management)

A single perspective subsumes everything engine-related — registry management, tournament configuration, and live observation of running games. They share the same primary nouns (engines, games, tournaments) and benefit from the same screen real estate.

Internal layout uses an inner nav (tabs or rail) within the perspective:

- **Roster** — manage registered engines: list (search/filter, supports many), add, remove, configure per-engine UCI options (see "Per-engine UCI options" below). A persistent default UCI options section applies across engines.
- **Tournaments** — create, save, edit, and start tournament configurations (engine pairings, time controls, rounds, concurrency, SPRT parameters). Browse historical tournaments and drill into past games.
- **Observe** — workspace canvas for live tournaments and headless-peek attachment. WinBox-driven: each running game is a window; standings, SPRT progress, schedule, event log are their own windows. Layouts persist per tournament. Read-only with respect to game play — no input goes back to engines.

Engines management is **not** a settings dialog tab. It is a first-class screen with full width, vertical room, and real master-detail interactions. Settings dialog stays small and is reserved for toggles, time-control defaults, paths, and similar form-shaped concerns.

##### Per-engine UCI options

A modal dialog opened from the Roster's per-engine "options" (sliders) icon.
Title: the engine's display name. Lets the user view and override every UCI
option the engine advertises.

- **Source of truth.** The dialog renders from the cached `option_schema`
  on the registry entry (captured at engine registration). No engine spawn
  on dialog open — fast, works offline, no race with live games. A small
  "refresh schema" affordance is available for the case of an upgraded
  binary (`POST /engines/{id}/refresh-schema`).
- **Show all advertised options.** No curation. Listing them all minimizes
  the number of dialogs and keeps engine-specific surface (SyzygyPath,
  EvalFile, ContemptFactor, …) reachable in one place.
- **Control mapping by UCI type:**
  - `spin` (int range) → number input (with `min`/`max` honored).
  - `combo` (enum) → select.
  - `check` (bool) → switch.
  - `string` → plain text input by default. If the option's *name* matches
    `*Path | *File | *Dir` (case-insensitive), use the file/directory
    picker dialog instead. Name-pattern based, not value-sniffed: works
    even when the default is empty.
  - `button` → action button (POSTs the option name to the server, which
    sends the UCI `setoption` to the engine on next session — buttons are
    stateless by definition).
- **Persistence.** On Save, the dialog PUTs the changed-from-default
  options to `/engines/{id}` (`{options: {...}}`). The registry stores
  only diffs from the engine's advertised defaults so "Defaults" can be
  meaningful.
- **Apply policy** (mirrors the global settings rule, see below):
  - Idle / no game in progress → applies immediately to the selected
    engine for any next session.
  - Human-vs-engine game in progress → toast "applies on next game"; the
    in-progress game keeps the options it started with.
  - Tournament in progress with this engine → toast "applies on next
    tournament"; the running tournament is not disturbed mid-flight.
- **"Defaults" button** at the bottom of the dialog. Resets every field
  to the engine's advertised UCI default (re-renders from `option_schema`,
  ignoring the persisted overrides). Save still required to commit.
- **Cancel** discards uncommitted edits; the persisted options stay as
  they were.

Settings-during-game/tournament policy (relevant beyond the per-engine
dialog): for any settings change that cannot apply mid-flight (side
choice, time control, per-engine UCI options), the rule is
**accept-and-notify**: persist the change, surface a toast describing
when it takes effect ("applies on next game" / "applies on next
tournament"). Never block the edit. The current game / tournament keeps
the values it was started with. See the "Logging" section for the
related TODO on tournament-running settings policy.

Future perspectives (not Phase 1): Analysis, Library/PGN browser, History.

TODO: Play/Analyze from a FEN. Two pieces:
- A "Load FEN" entry point (likely a small dialog) that validates and sets
  the position before play resumes.
- An Analyze mode (separate from Play): no clock, no enforced sides — engine
  runs continuously and streams PV/eval; user can play moves for either
  side; New Game semantics do not apply. Probably a top-level Play/Analyze
  toggle within the Play perspective, or a sibling perspective.

### Phasing

- **Phase 1**: Play perspective fully working with engine management, settings dialog, file picker dialog, message/confirm/toast primitives. Observe perspective with at least one live tournament displayed in WinBox windows. No agents.
- **Phase 2**: Agent integration (Play side-rail tab + Observe windows), voice control, analysis perspective, eval graphs.

### Library strategy

Vendor two libraries, both MIT, both available as plain pre-built ES modules:

- **Web Awesome** (`@awesome.me/webawesome`, formerly Shoelace): dialogs, form controls, toasts, tabs, dropdowns, popovers, icons (Font Awesome Free). Theming via CSS custom properties.
- **WinBox**: floating draggable/resizable windows. Web Awesome does not cover this; WinBox is a single-purpose dep.

Both are vendored from their published npm `dist/` artifacts, **not** as git submodules of the source repos (those require build steps). Procedure: `npm pack` once, extract, commit `web/vendor/<lib>/`. No npm at runtime, no CDN, no TypeScript served.

A thin app-level wrapper (`web/app/dialogs.js`) exposes `confirm()`, `alert()`, `prompt()`, `pickFile()`, `toast()` as Promise-returning helpers. Application code never touches Web Awesome's event API directly — it goes through this wrapper, which keeps swap-out cost low if we ever change UI libraries.

### What does *not* belong in the UI library

- **Chess board** — already handled by cm-chessboard.
- **Markdown rendering / prose styling for agent output** — if needed later, a small dedicated dep (e.g. `marked`); not Web Awesome's job.
- **Charting / eval graphs** — separate concern, separate dep when the time comes.

---

## Resilience (Implementation Best Practices)

- **Engine search cancel (take-back / new-game / resign mid-think)**: the engine
  subprocess is killed and a fresh one is spawned on the next move. UCI engines
  are stateless across `ucinewgame`, so re-spawn cost is acceptable and avoids
  fighting the protocol's stop-then-bestmove handshake.
- **Engine crash (human vs engine)**: python-chess detects via `EngineTerminatedError`,
  server publishes a `system` event with `error: engine_terminated`; UI surfaces it.
- **Tournament manager crash**: orchestrator detects, cleans up orphaned proxies, GUI shows lost connection
- **Proxy crash**: wrapper restarts proxy transparently; game continues unaffected, brief observability gap
- **Network drop (peek client)**: WebSocket client reconnects with exponential backoff; server rebroadcasts state on reconnect
- **Attach to mid-run tournament**: catch-up from PGN/log to reconstruct state, then switch to live stream
- **Illegal move from human**: server rejects with 400 and immediately re-publishes
  the canonical board state so the client UI snaps back. (Phase 2: optional
  "strict" mode auto-resigns on illegal moves.)
- All failure states are surfaced explicitly in the UI — no silent stalls

---

## Tablebase Management

- Configure local tablebase paths (Syzygy, Gaviota)
- Server-side lookup exposed to both human vs engine and tournament observer views
- Display DTZ/DTM, WDL result, best move in tablebase positions
- python-chess has built-in tablebase support — use it directly

---

## Opening Identification

- Implemented for human-vs-engine. Tournament observer reuses the same
  lookup once Observe ships.
- Lichess [chess-openings](https://github.com/lichess-org/chess-openings)
  dataset, vendored as a git submodule under `web/vendor/chess-openings/`.
- Server loads all `*.tsv` files at startup, parses PGN move sequences into
  UCI tuples, builds a dict for prefix lookup. Longest matching prefix wins.
- Result is published in the `board_update` event payload as
  `opening: { eco, name }` and rendered by GameView under the board.
- Fully offline, no external API dependency.

---

## Tournament & SPRT Management

- Create, save, and load tournament configurations (engine pairings, time controls, rounds, concurrency)
- SPRT configuration: H0/H1 elo, alpha/beta, auto-stop on conclusion
- View running tournament state: standings, game results, SPRT status and bounds progression
- Historical tournament results: browse past tournaments, drill into individual games
- PGN export per tournament or per game

---

## Open / Deferred

- PGN viewer: variation tree vs linear history — left open for implementation phase
- Voice control and speech interface — later phase, prior implementation to be leveraged
- Eval graph over full game history
- Agent implementations (analysis, teacher, etc.)
- Linux WebKitGTK consistency across distros
- Theme integration for board annotations: when themes (dark/light) are
  wired up, the engine "considered move" arrow color must derive from the
  active theme rather than the cm-chessboard default green.

### Server-side persistence — to be revisited

Current state (Phase 1, as implemented): only `settings.json` and `engines.json` are
persisted (under the OS user-config dir via `platformdirs`). Games and logs are
**in-memory only** — they evaporate on server restart. The `pgn_autosave` /
`pgn_dir` settings are wired through the API but have no writer behind them yet.

Decisions deferred:

- **Game persistence**: where PGNs land (per-game file vs append to a session
  archive), retention, and how history is exposed in the UI (Library/History
  perspective). Tournament PGNs likely follow a different path (per-tournament
  directory) than human-vs-engine PGNs.
- **Log management**: today logs go to stdout/stderr only. Once the server runs
  detached or under PyWebView for long sessions, we will need rotating file
  logs (size- or time-based), a configurable log dir, retention policy, and a
  way to surface recent server logs in the GUI for debugging. Engine stdio
  traffic captured by the proxy is a separate, higher-volume stream — it should
  not share the application log file. Cross-platform: file paths via
  `platformdirs.user_log_dir()`, no syslog/journald assumptions.
- **Tournament history storage**: PGN-on-disk is enough for browsing, but
  standings, SPRT state, and schedule reconstruction may want a small index
  (sqlite) — flagged for the tournament-history milestone, not Phase 1.

---

## Tech Stack Summary

| Layer | Technology |
|---|---|
| Backend server | Python |
| UCI / human vs engine | python-chess |
| Native window | PyWebView |
| Browser client | HTML / CSS / JS |
| Chess board | cm-chessboard (git submodule) |
| Performance extensions | C++ (only if profiling demands) |
| Tournament managers | fastchess, cutechess-cli (pluggable) |
