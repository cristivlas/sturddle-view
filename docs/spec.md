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
