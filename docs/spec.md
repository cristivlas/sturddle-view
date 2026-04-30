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

### 4. GUI Backend Server

Python-based server, three API areas:

#### `/settings` — REST CRUD
Server-side (persisted, backend behavior):
- PGN auto-save toggle and save path
- Time controls
- Opening book paths and usage mode
- Engine paths and UCI options
- Tournament config (rounds, concurrency, SPRT parameters)

Client-side (localStorage, server-agnostic):
- Theme, piece set, board colors
- UI layout preferences
- Sound on/off

#### `/game` — REST
- Submit human move
- New game, resign, take-back
- Tournament control: start, stop, pause, attach

#### `/ws` — WebSocket
- All live events: engine info lines (depth, eval, PV, nodes, NPS), board state updates, clock ticks, tournament standings, game results
- Events are **structured and typed** (not ad-hoc strings) — required for agent consumption
- On client reconnect: server rebroadcasts current state
- Client implements exponential backoff reconnect

### 5. Browser Client

- cm-chessboard vendored as git submodule (offline, pinned, no CDN dependency)
- Two primary views:
  - **Tournament observer**: all running games, standings, SPRT status, eval bar per game, focus a single game for full engine info
  - **Human vs engine**: interactive board, eval bar, PV display, move list, take-back
- Agent/analysis panels: designated UI areas agents can populate
- Client-side settings persisted in localStorage

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
- Top of side rail: compact game-control bar — New Game, Resign, Take-back.
- No floating windows. The board is the focus; nothing should float over it during play.
- One docked panel toggle: an optional "agent" tab in the side rail (Phase 2).

#### Engines perspective (workspace + management)

A single perspective subsumes everything engine-related — registry management, tournament configuration, and live observation of running games. They share the same primary nouns (engines, games, tournaments) and benefit from the same screen real estate.

Internal layout uses an inner nav (tabs or rail) within the perspective:

- **Roster** — manage registered engines: list (search/filter, supports many), add, remove, configure per-engine UCI options. A persistent default UCI options section applies across engines.
- **Tournaments** — create, save, edit, and start tournament configurations (engine pairings, time controls, rounds, concurrency, SPRT parameters). Browse historical tournaments and drill into past games.
- **Observe** — workspace canvas for live tournaments and headless-peek attachment. WinBox-driven: each running game is a window; standings, SPRT progress, schedule, event log are their own windows. Layouts persist per tournament. Read-only with respect to game play — no input goes back to engines.

Engines management is **not** a settings dialog tab. It is a first-class screen with full width, vertical room, and real master-detail interactions. Settings dialog stays small and is reserved for toggles, time-control defaults, paths, and similar form-shaped concerns.

Future perspectives (not Phase 1): Analysis, Library/PGN browser, History.

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

- **Engine crash (human vs engine)**: python-chess detects, GUI shows clear error state
- **Tournament manager crash**: orchestrator detects, cleans up orphaned proxies, GUI shows lost connection
- **Proxy crash**: wrapper restarts proxy transparently; game continues unaffected, brief observability gap
- **Network drop (peek client)**: WebSocket client reconnects with exponential backoff; server rebroadcasts state on reconnect
- **Attach to mid-run tournament**: catch-up from PGN/log to reconstruct state, then switch to live stream
- All failure states are surfaced explicitly in the UI — no silent stalls

---

## Tablebase Management

- Configure local tablebase paths (Syzygy, Gaviota)
- Server-side lookup exposed to both human vs engine and tournament observer views
- Display DTZ/DTM, WDL result, best move in tablebase positions
- python-chess has built-in tablebase support — use it directly

---

## Opening Identification

- Use Niklas Fiekas's [chess-openings](https://github.com/lichess-org/chess-openings) dataset (git submodule)
- Server-side lookup: given a position or move sequence, return ECO code, opening name, variation
- Displayed in board UI as moves are played — both human vs engine and tournament observer
- Fully offline, no external API dependency

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
