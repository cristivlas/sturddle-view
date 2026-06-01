# sturddle-view

Browser-based chess GUI for human vs engine play and live observation of headless
engine tournaments. See [docs/spec.md](docs/spec.md) for the full design.

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
sturddle-view
```

The server binds to `127.0.0.1` by default and prints an `/auth?token=...`
URL at startup. Open it once; the server sets an `HttpOnly` cookie and
redirects to `/ui/`. To expose the server on the LAN/tailnet, pass
`--host 0.0.0.0` (token still required, or add `--no-auth` if you trust
the network). To serve over TLS, supply `--cert PATH --key PATH`. See
[docs/spec.md#security](docs/spec.md#security) for the full model.

### Submodules

```bash
git submodule update --init --recursive
```

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

## Environment variables

All knobs use the `SV_` prefix; any CLI flag above has an `SV_*` equivalent
(e.g. `SV_HOST`, `SV_PORT`, `SV_AUTH_DISABLED`). AI, tournament, and debug
tunables are documented in full in [docs/env-vars.md](docs/env-vars.md).

## Features

- Human-vs-engine play with engine eval/PV display and adjustable time control.
- Engine roster: register UCI engines, edit per-engine options, set defaults (Hash, Threads, SyzygyPath, opening book).
- Tournaments: round-robin or gauntlet via [fastchess](https://github.com/Disservin/fastchess); per-row Info, Start/Pause/Resume, sortable list, live game observation in floating windows.
- Tournament settings (engine defaults, opening book) are snapshotted into the tournament's `state.json` at create time so Stop/Resume can't drift.
- AI analysis & commentary: prose over the engine's eval/PV via Anthropic, Google Gemini, or local Ollama. Off by default; the engine stays the source of truth. See [docs/ai-analysis-spec.md](docs/ai-analysis-spec.md).

See [docs/spec.md](docs/spec.md) and [docs/tournament-spec.md](docs/tournament-spec.md) for design details.

> Also ships as a self-contained standalone desktop app.