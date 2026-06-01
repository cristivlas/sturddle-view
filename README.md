# sturddle-view

Browser-based chess GUI for human vs engine play and live observation of headless
engine tournaments. See [docs/spec.md](docs/spec.md) for the full design.

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
playwright install chromium  # one-time, for end-to-end tests
sturddle-view --reload
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

## Features

- Human-vs-engine play with engine eval/PV display and adjustable time control.
- Engine roster: register UCI engines, edit per-engine options, set defaults (Hash, Threads, SyzygyPath, opening book).
- Tournaments: round-robin or gauntlet via [fastchess](https://github.com/Disservin/fastchess); per-row Info, Start/Pause/Resume, sortable list, live game observation in floating windows.
- Tournament settings (engine defaults, opening book) are snapshotted into the tournament's `state.json` at create time so Stop/Resume can't drift.
- AI analysis & commentary: prose over the engine's eval/PV via Anthropic, Google Gemini, or local Ollama. Off by default; the engine stays the source of truth. See [docs/ai-analysis-spec.md](docs/ai-analysis-spec.md).

See [docs/spec.md](docs/spec.md) and [docs/tournament-spec.md](docs/tournament-spec.md) for design details.

> Also ships as a self-contained standalone desktop app.