# SturddleView

Browser-based chess GUI for human vs engine play and live observation of headless
engine tournaments.

## Getting started

Requires Python >= 3.10 and at least one UCI engine binary. The app ships no
engine -- download any UCI-compatible engine and register it from the Engines
tab once the app is running.

```bash
git clone https://github.com/cristivlas/sturddle-view.git
cd sturddle-view
git submodule update --init --recursive   # board renderer + opening data (required)
```

The submodules ([cm-chessboard](https://github.com/shaack/cm-chessboard),
[chess-openings](https://github.com/lichess-org/chess-openings)) provide the
board UI and opening identification; the app will not render correctly without
them, so init them before the first run.

## Quickstart

Create a virtualenv, install the package (editable, with dev extras), and run.
The only OS difference is how the virtualenv is activated.

**Linux / macOS**

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
sturddle-view
```

**Windows (PowerShell)**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
sturddle-view
```

**Windows (cmd)**

```bat
python -m venv .venv
.venv\Scripts\activate.bat
pip install -e ".[dev]"
sturddle-view
```

The server binds to `127.0.0.1:8765` by default and prints the full
startup URL (e.g. `http://127.0.0.1:8765/auth?token=...`). Open it once;
the server sets an `HttpOnly` cookie and redirects to `/ui/`. To expose
the server on the LAN/tailnet, pass `--host 0.0.0.0` (token still
required, or add `--no-auth` if you trust the network). To serve over
TLS, supply `--cert PATH --key PATH`.

## Command-line flags

| Flag | Effect |
|---|---|
| `--host HOST` | Bind address. Default `127.0.0.1`. |
| `--port PORT` | Bind port. Default `8765`. |
| `--engine PATH` | Fallback UCI engine when the registry has no selection. |
| `--desktop` | Open in a native window instead of a browser tab. |
| `--width N` / `--height N` | Desktop window size. Default `1280` x `1000`. |
| `--no-auth` | Disable token auth. Loopback only unless you accept the risk. |
| `--cert PATH` / `--key PATH` | Serve over TLS (PEM). Must be given together; rejected with `--desktop`. |
| `--instance TAG` | Isolate config/data dirs so multiple instances run side by side. |
| `--reload` | Dev: auto-reload on source changes. |
| `--debug` | Verbose (DEBUG) logging for the app. |
| `--server-debug` | Verbose (DEBUG) logging for uvicorn. |

### Flag reference

Every flag has an `SV_*` environment-variable equivalent (see
[Environment variables](#environment-variables)); the flag wins when both are
set.

**Networking.** `--host` sets the bind address and `--port` the port (defaults
`127.0.0.1:8765`). The loopback default keeps the server private; set
`--host 0.0.0.0` to reach it from other machines on your LAN or tailnet. Token
auth still applies on a non-loopback bind unless you also pass `--no-auth` --
do that only on a network you trust, since it drops the only access control.

**TLS.** `--cert PATH` and `--key PATH` serve HTTPS from a PEM certificate and
key. They must be supplied together, and are rejected together with `--desktop`
(the native window talks to the loopback server directly, so TLS adds nothing).

**Desktop.** `--desktop` opens the app in a native window instead of a browser
tab; `--width N` / `--height N` set its initial size (default `1280` x `1000`).
Without `--desktop` the size flags are ignored.

**Engine.** `--engine PATH` names a UCI binary to fall back on when the engine
registry has no active selection. Normally you register engines from the
Engines tab instead; this is a convenience for a one-off run.

**Multiple instances.** `--instance TAG` namespaces the config and data
directories so two servers (e.g. a stable and a dev build) can run at once
without clobbering each other's settings, engine registry, or tournaments.

**Dev / debug.** `--reload` auto-restarts the server on source changes (dev
only). `--debug` raises the app's own logging to DEBUG; `--server-debug` does
the same for uvicorn (request/transport noise). They are independent -- combine
them for the full firehose.

## Environment variables

All knobs use the `SV_` prefix; any CLI flag above has an `SV_*` equivalent
(e.g. `SV_HOST`, `SV_PORT`, `SV_AUTH_DISABLED`). AI, tournament, and debug
tunables are documented in full in [docs/env-vars.md](docs/env-vars.md).

## Screenshots

| | |
|---|---|
| [![Play/analysis with engine lines and AI panel](screenshots/arena-analysis-1.webp)](screenshots/arena-analysis-1.webp) | [![Play/analysis, alternate position](screenshots/arena-analysis-2.webp)](screenshots/arena-analysis-2.webp) |
| [![Play/analysis, alternate position](screenshots/arena-analysis-3.webp)](screenshots/arena-analysis-3.webp) | [![Play/analysis, alternate position](screenshots/arena-analysis-4.webp)](screenshots/arena-analysis-4.webp) |
| [![Review: import by PGN, FEN, or opening](screenshots/import-pgn.webp)](screenshots/import-pgn.webp) | [![Review: game with eval bar](screenshots/studio-game-analysis.webp)](screenshots/studio-game-analysis.webp) |
| [![Studio: live tournament boards](screenshots/studio-watch-live-games.webp)](screenshots/studio-watch-live-games.webp) | [![Studio: dashboard, standings, head-to-head](screenshots/studio-tournament-overview.webp)](screenshots/studio-tournament-overview.webp) |
| [![Arena: dockable board panels](screenshots/arena-panels-menu.webp)](screenshots/arena-panels-menu.webp) | [![Studio: discard in-progress game](screenshots/studio-discard-prompt.webp)](screenshots/studio-discard-prompt.webp) |
| [![AI commentary over the engine's PV](screenshots/ai-commentary.webp)](screenshots/ai-commentary.webp) | [![AI: live reasoning trace](screenshots/ai-thinking.webp)](screenshots/ai-thinking.webp) |
| [![AI: completed analysis](screenshots/ai-analysis-done.webp)](screenshots/ai-analysis-done.webp) | [![Settings: tournament / fastchess](screenshots/settings-tournament.webp)](screenshots/settings-tournament.webp) |
| [![Settings: AI provider](screenshots/settings-ai-provider.webp)](screenshots/settings-ai-provider.webp) | [![Settings: Ollama model](screenshots/settings-ai-model.webp)](screenshots/settings-ai-model.webp) |

## Features

- **Play** -- human vs UCI engine with live eval/PV display, adjustable time control, and play from any position.
- **Review** -- step through any game (import by PGN, FEN, or opening name); engine eval plus opening identification. The Play tab becomes View while reviewing.
- **Edit & comment** -- set up arbitrary positions in the FEN/position editor; add move annotations and comments that save to PGN.
- **Tournaments** -- round-robin or gauntlet via [fastchess](https://github.com/Disservin/fastchess), with engine defaults and opening book snapshotted per run for reproducibility, plus live game observation in floating windows. Two UX perspectives: **Arena** (classic sortable list with per-row Info / Start / Stop / Restart) and **Studio** (dashboard with standings and head-to-head).
- **Engine roster** -- register UCI engines, edit per-engine options, set defaults (Hash, Threads, SyzygyPath, opening book).
- **AI Analysis (experimental)** -- natural-language commentary over the engine's eval/PV via Anthropic, Google Gemini, or local Ollama. Off by default; the engine stays the source of truth. Hosted providers need an API key (Settings -> Analysis, stored in the OS keyring); Ollama runs locally with no key. See [docs/ai-analysis-spec.md](docs/ai-analysis-spec.md).
- **Native desktop window** -- run with `--desktop` to open in a PyWebView window instead of a browser tab (install the `desktop` extra: `pip install -e '.[dev,desktop]'`).

See [docs/tournament-spec.md](docs/tournament-spec.md) for design details.

## Standalone desktop build

The app can be packaged as a single self-contained executable (PyInstaller
`--onefile --windowed`) that bundles the web assets and runs without a Python
install. Build from the repository root:

```bash
python scripts/build_exe.py
```

This provisions an isolated `build_venv/`, installs the package with its
`desktop` extra plus PyInstaller, and writes `dist/sturddle-view-<version>`
(`.exe` on Windows) alongside a `.sha256` checksum. Useful flags:

- `--reuse-venv` -- skip venv creation / dependency install when `build_venv/`
  already exists (reinstalls only the local package to pick up code changes).
- `--console` -- keep a console window for debugging (omit for release builds).
