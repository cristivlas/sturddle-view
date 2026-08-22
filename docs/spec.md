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
- Entry point: `sturddle-view [--desktop]` — `--desktop` opens a PyWebView native window immediately; omit to run headless and point any browser at the server URL
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

Gameplay:
- `pgn_autosave` (toggle), `pgn_dir` (path) — save games as PGN
- `tc_initial_seconds`, `tc_increment_seconds` — default time control
- `human_side` — `"white" | "black" | "random"`
- `allow_takeback`
- `auto_claim_draws` — auto-claim 3-fold / 50-move draws
- `inherit_pgn_clocks` — new game inherits live clock from PGN cursor when true; resets to TC initial when false
- `play_eval_pov` — eval display POV: `"white" | "engine" | "human"`
- `board_style` — board color scheme

Engine defaults (applied across all engines unless overridden per-engine):
- `engine_path` — fallback engine when no engine selected from registry
- `engine_default_threads`, `engine_default_analysis_threads`
- `engine_default_hash_mb`
- `engine_default_syzygy_path`
- `engine_default_book_path`, `engine_default_book_plies`, `engine_default_book_order`

Tournament:
- `tournament_fastchess_path` — path to fastchess binary
- `tournament_root` — root directory for tournament files
- `tournament_default_template` — default tournament config template

Excluded from persistence: `token`, `host`, `port`, `auth_disabled`, `web_dir`.
These come from CLI flags / env vars (`SV_*` prefix or `.env` at repo root).

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
- Tournament control: start (wipes + runs fresh), stop; attach to an
  externally-started run is deferred

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
  - WinBox — draggable/resizable windows (tournament workspace live games)
  - Font Awesome Free SVGs — icon library used by Web Awesome
  - `chess-openings` (Lichess) — opening identification dataset
- **Reusable `GameView` component**: composes board + clocks + move list +
  engine info + opening line + tablebase result. Used by the Play
  perspective and inside tournament-workspace live-game windows. Each aspect
  can be hidden/shown independently.
- Two perspectives: Play, Engines. (See "Perspectives" below.)
- Agent/analysis panels: a dockable AI analysis window (commentary + coach);
  see `ai-analysis-spec.md`.
- Per-perspective layout state persisted in localStorage.

---

## Security

### Threat model

Single-user app. Two intended deployment modes:

1. **Local-only** (server default): server bound to `127.0.0.1`. No
   network surface; the only attacker model is local malware running as
   the same user, against which TLS and auth tokens are not defenses.
2. **Trusted LAN / tailnet** (explicit `--host 0.0.0.0`, or desktop's
   About -> Connect from mobile, which opens the LAN port for the
   session): the About dialog shows the entry URL as a QR
   code, served to loopback clients only. The token must keep
   unauthorized peers out; optionally TLS protects against on-wire
   sniffing.

Not in scope: public internet exposure, multi-user isolation, role-based
access, rate limiting, audit logging.

### Bind policy

Default `--host` is `127.0.0.1`. `--no-auth` alone does **not** widen
the bind. Opening the server to the network requires an explicit `--host`
argument, or in desktop mode the user's own Connect-from-mobile action
(for that session only). The unsafe combo of a non-loopback bind
with `--no-auth` is permitted (the tailscale / trusted-LAN case) but logs
a startup warning.

### Authentication

Single shared-secret token, generated at startup via
`secrets.token_urlsafe(24)`. The token is presented two ways:

- **Cookie** (`sv_auth`, `HttpOnly`, `SameSite=Lax`, `Secure` when TLS is
  on). Set by a one-shot `GET /auth?token=<secret>` handshake that
  validates the token and 303s to `/ui/`. The token never appears in the
  rendered page URL, browser history, or `Referer` headers.
- **`Authorization: Bearer <token>`** for non-browser clients (CLI,
  tests). Equivalent security to the cookie.

The `?token=` URL query fallback was removed. Only the `/auth` handshake
accepts the token in the URL, and only to convert it into a cookie.

`SameSite=Lax` was chosen over `Strict` because `Strict` breaks the 303
redirect in some embedded webviews. Lax still strips the cookie from
cross-site POSTs, which is what defeats CSRF.

`--no-auth` disables the token check entirely. The startup banner makes
this explicit. Combined with a non-loopback `--host`, the warning is
escalated.

### Origin enforcement

`_OriginMiddleware` rejects non-`GET`/`HEAD`/`OPTIONS` requests whose
`Origin` header doesn't match `Host`. Missing `Origin` is allowed —
browsers always send it on cross-origin requests, so this defeats CSRF
and DNS-rebinding from a malicious page on the same machine. Non-browser
clients (curl, `TestClient`, Python `requests`) don't send `Origin` and
pass through unchanged.

The same check runs on every WebSocket upgrade.

### Security headers

Applied by `_SecurityHeadersMiddleware`:

- `X-Frame-Options: DENY` — no embedding.
- `Referrer-Policy: no-referrer` — prevent token leakage via outbound
  links (defense-in-depth; the token isn't in URLs anymore).
- `Content-Security-Policy` — `default-src 'self'`; `connect-src` allows
  `data:` and `ws:`/`wss:` (component libraries fetch inline-SVG icon
  payloads); `frame-ancestors 'none'`.

### TLS

User-supplied cert and key via `--cert PATH --key PATH`. Both are
required together; otherwise the server exits 2 with a clear message.
Files must exist and be readable; missing files fail before uvicorn
starts.

Self-signed cert generation is **not supported**. The recommended
workflows are:

- A self-signed cert generated with `openssl` for local dev:

  ```
  openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem \
    -days 30 -subj "/CN=localhost" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
  ```

  Browsers will warn once; click through.
- A real cert (Let's Encrypt, tailscale serve, etc.) for stable hosts.

`--cert/--key` is rejected with `--desktop`. Loopback HTTP is already a
[secure context](https://developer.mozilla.org/en-US/docs/Web/Security/Secure_Contexts)
in modern browsers, and PyWebView/Chromium can't easily be made to
trust a per-app self-signed cert without intrusive system-cert-store
changes.

When TLS is on, the cookie is marked `Secure`; the banner advertises
`https://` and `wss://`; the orchestrator's internal proxy URL is also
served over `https`.

#### Windows proactor caveat

`_install_proactor_accept_resilience` (in `app.py`) overrides
`asyncio.proactor_events.BaseProactorEventLoop._start_serving` to re-arm
`AcceptEx` on transient `WinError` codes. Its accept loop must call
`_make_ssl_transport` when `sslcontext is not None` — falling back to
`_make_socket_transport` produces `ERR_SSL_PROTOCOL_ERROR` on the
client and `Invalid HTTP request received` on the server.

### Disconnect UX

When the WebSocket disconnects, the client visibly disables the Settings
gear and the Engines perspective nav button. If the user is on Engines
when the connection drops, they are auto-routed back to Play. Play
remains usable for last-known state inspection.

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
- One game per file (not appended) -- easier to delete/share individually.
  Bulk import to other tools is `cat *.pgn > all.pgn` away.
- Headers: Event (Sturddle View -- Human vs Engine), Site, Date, White, Black,
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

See README for install steps.

---

## UI / Frontend

Aesthetics and UX are first-class concerns; the GUI is not just a thin debug surface for the backend. Visual polish, consistent theming, and predictable interactions matter as much as functional correctness.

### Palette and component styling

Cool-toned dark theme. Page background `#1d1f24`, panel `#25272d`. Web
Awesome's `--wa-color-danger-*` is overridden with `wa-danger-orange`
plus a muted terracotta `--wa-color-danger-fill-loud: #8a5a3c` (with
cream `--wa-color-danger-on-loud`). Default neutral filled buttons use
`--wa-color-neutral-fill-loud: #3a3d44` so unstyled `<wa-button>`
renders dark instead of WA's near-white default.

Buttons are filled by default (no `appearance="outlined"` per-call
overrides). Only the Add-engine `+` keeps `variant="brand"` so it stands
out as the single prominent affordance per screen; New game, Use, and
similar role buttons render neutral. Destructive verbs use
`variant="danger"`. Icon-only buttons get `aria-label` plus a recognized
font-awesome glyph; we never rely on tooltips for primary meaning.

Top nav and list rows share a token family — `--nav-btn-bg`,
`--nav-btn-bg-hover`, `--nav-btn-bg-active` — so "selected/hovered"
reads consistently across the app. Recessed list panels use a slightly
darker `#15171b` well with `#34373d` border.

Confirm/alert dialogs are headerless (the message is the heading), have
`--width: fit-content` to size to content, and use
`--spacing: var(--wa-space-s)` for tighter outer padding while keeping
breathing room between the message and the footer buttons.

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
- **Inline form controls** — buttons, inputs, selects, switches, checkboxes, tabs, accordions, tooltips. Themed consistently. Tabs inside a dialog default to **left placement** (`wa-tab-group placement="start"`) so multi-section dialogs feel like settings panels and scale to more sections without reflowing the header. Top placement is reserved for short, equally-weighted sibling pairs where a category metaphor would be misleading.

### Interaction model

- **Action chaining via Promises.** All dialog primitives return Promises that resolve to the user's choice (or `null` for cancel). Application code chains naturally: `await confirm(...)` → `await api(...)` → `toast(...)`. This is the "monadic" pattern — Promises are the substrate, no custom monad is needed.
- **One blocking modal at a time.** Floating panels are unrestricted; modal dialogs are stacked one-deep. New modal requested while one is open: queue or replace, never overlap.
- **Keyboard-first wherever practical.** Esc closes modals. Enter confirms default action. Tab cycles within a modal's focus trap. Drag handles are mouse/touch only — keyboard users get an alternate "open in dialog" view.
- **Persisted layout.** Floating-panel positions, sizes, and open/closed state survive reload (localStorage, scoped per perspective).
- **No silent failures.** Every API error surfaces as either a toast (recoverable) or a message box (blocking). The event log is a debug aid, not a substitute for explicit feedback.

### Perspectives

The app is organized into **perspectives** — distinct top-level layouts tuned to different activities. Top-bar nav switches between them. A perspective owns its own root layout container; switching perspectives swaps the root content but leaves dialogs, toasts, and global state untouched.

Two perspectives ship:

#### Play perspective (focused, fixed layout)

For human vs engine play. Calm, distraction-free.

- Centered board, large but bounded (max ~85vh).
- Fixed side rail (right on wide screens, below on narrow): clock, move list, engine info during search.
- Vertical icon ribbon snapped to the viewport's left edge (mobile: horizontal strip below the board) — New, Open, Take-back, Flip, Pause, Resign.
  - Pause is enabled only on the human's turn (engine is idle then); it
    stops the clock and rejects moves until resumed.
- No floating windows. The board is the focus; nothing should float over it during play.
- One docked panel toggle: the AI analysis window (see `ai-analysis-spec.md`).

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

Future perspectives (deferred): Analysis, Library/PGN browser, History.

TODO: Play/Analyze from a FEN. Two pieces:
- A "Load FEN" entry point (likely a small dialog) that validates and sets
  the position before play resumes.
- An Analyze mode (separate from Play): no clock, no enforced sides — engine
  runs continuously and streams PV/eval; user can play moves for either
  side; New Game semantics do not apply. Probably a top-level Play/Analyze
  toggle within the Play perspective, or a sibling perspective.

### Storage conventions

Both `localStorage` keys and `CustomEvent` names use a colon-delimited
namespace: `sturddle:<area>:<key>`. Areas correspond to modules or features
(`engines`, `tournaments`, `pvtable`, `ucilog`, `workspace`, `play`, etc.).
Sub-namespaces use further colons (`sturddle:engines:settings:colPcts3`).

Third-party namespaces (e.g. `fs-picker:last:*` used by the file picker
module) keep their own prefix and are not folded into `sturddle:`.

No migration is performed when keys are renamed; users wipe their local
state with a one-liner in DevTools:

```js
Object.keys(localStorage).filter(k => k.startsWith("sturddle")).forEach(k => localStorage.removeItem(k));
```

### Status

- **Shipped**: Play perspective (engine management, settings, file picker,
  message/confirm/toast primitives); tournament workspace with live WinBox
  windows; AI analysis agents (commentary + coach) with a dockable panel --
  see `ai-analysis-spec.md`.
- **Deferred** (see "Open / Deferred"): voice control, a dedicated analysis
  perspective, eval graphs.

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
  the canonical board state so the client UI snaps back. (Deferred: optional
  "strict" mode auto-resigns on illegal moves.)
- All failure states are surfaced explicitly in the UI — no silent stalls

---

## Tablebase Management

Implemented for human-vs-engine: `engine_default_syzygy_path` is
forwarded to engines, and a server-side Syzygy WDL/DTZ probe
(`play/tablebase.py`, python-chess) publishes results in the
`board_update` payload (`tablebase` field), rendered by GameView.
Not wired into tournament observation.

---

## Opening Identification

- Implemented for human-vs-engine. Tournament observer reuses the same
  lookup once Observe ships.
- Lichess [chess-openings](https://github.com/lichess-org/chess-openings)
  dataset, vendored as a git submodule under `web/vendor/chess-openings/`.
- Server loads all `*.tsv` files at startup, parses PGN move sequences into
  UCI tuples, builds a dict for prefix lookup. Longest matching prefix wins.
- Result is published in the `board_update` event payload as
  `opening: { eco, name }` (embedded in the event, not a separate event type)
  and rendered by GameView under the board.
- Fully offline, no external API dependency.

---

## Tournament & SPRT Management

Engine-vs-engine tournaments (round-robin / gauntlet / SPRT) run locally
via `fastchess`, with a workspace UI for live standings, per-game boards,
and SPRT progress. Shipped; this section is a summary -- the authoritative
specs are:

- **`tournament-spec.md`** -- orchestrator, runner strategy, lifecycle
  (Start / Stop / Restart; no resume -- Stop wipes), persistence,
  standings/Elo, workspace.
- **`sprt-ux-spec.md`** -- SPRT settings, template toggle, workspace panel.
- **`pgn-elo-ordo.md`** -- joint Elo rating fit and margins.
- **`pgn-reconciliation.md`** -- matching live games to fastchess PGN output.

---

## Open / Deferred

- PGN viewer: variation tree vs linear history — left open for implementation phase
- Voice control and speech interface — later phase, prior implementation to be leveraged
- Eval graph over full game history
- Linux WebKitGTK consistency across distros
- Theme integration for board annotations: when themes (dark/light) are
  wired up, the engine "considered move" arrow color must derive from the
  active theme rather than the cm-chessboard default green.
- Multi-select on the engines list (Settings > Engines). Engine testers
  curate large libraries (hundreds of UCI binaries); single-select with
  one-engine-at-a-time Remove makes housekeeping tedious. Wants:
  Shift/Ctrl click and Shift+Up/Down to extend selection, ribbon
  Remove acting on the set, and a confirm dialog summarizing the count.
  Activation (Use) stays single-select.

### Server-side persistence — to be revisited

Implemented: `settings.json`, `engines.json` (OS user-config dir via `platformdirs`),
human-vs-engine PGN autosave (per-game file, atomic write on each move and game end),
tournament PGNs (per-tournament directory under `tournament_root`),
rotating server log (5 x 2 MB, `platformdirs.user_log_dir()`).

Decisions deferred:

- **Game history UI**: Library/History perspective for browsing saved PGNs.
- **Tournament history storage**: PGN-on-disk is enough for browsing, but
  standings, SPRT state, and schedule reconstruction may want a small index
  (sqlite) — flagged for the tournament-history milestone.

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
