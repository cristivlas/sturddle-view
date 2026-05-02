# sturddle-view

Browser-based chess GUI for human vs engine play and live observation of headless
engine tournaments. See [docs/spec.md](docs/spec.md) for the full design.

## Layout

```
server/sturddle_view/   Python backend (FastAPI + python-chess)
web/                    Static client (no build step)
web/vendor/             Git submodules (cm-chessboard, chess-openings)
docs/                   Design notes
```

## Dev quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
playwright install chromium  # one-time, for end-to-end tests
sturddle-view --reload
```

The server prints an auth token at startup and redirects `/` to `/ui/?token=...`.

### Native window (PyWebView)

```bash
pip install -e '.[desktop]'
sturddle-view --desktop
```

### Submodules

```bash
scripts/init-submodules.sh
```

## Features

- Human-vs-engine play with a stockfish-style move list, engine eval/PV, and adjustable time control.
- Engine roster: register UCI engines, edit per-engine options, set defaults (Hash, Threads, SyzygyPath, opening book).
- Tournaments: round-robin or gauntlet via [fastchess](https://github.com/Disservin/fastchess); per-row Info, Start/Pause/Resume, sortable list, live game observation in floating windows.
- Tournament settings (engine defaults, opening book) are snapshotted into the tournament's `state.json` at create time so Stop/Resume can't drift.

See [docs/spec.md](docs/spec.md) and [docs/tournament-spec.md](docs/tournament-spec.md) for design details.
