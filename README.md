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

### Submodules

```bash
git submodule update --init --recursive
```

## Features

- Human-vs-engine play with engine eval/PV display and adjustable time control.
- Engine roster: register UCI engines, edit per-engine options, set defaults (Hash, Threads, SyzygyPath, opening book).
- Tournaments: round-robin or gauntlet via [fastchess](https://github.com/Disservin/fastchess); per-row Info, Start/Pause/Resume, sortable list, live game observation in floating windows.
- Tournament settings (engine defaults, opening book) are snapshotted into the tournament's `state.json` at create time so Stop/Resume can't drift.

See [docs/spec.md](docs/spec.md) and [docs/tournament-spec.md](docs/tournament-spec.md) for design details.

## Native desktop window (optional)

`--desktop` opens the app in a native window via [PyWebView](https://pywebview.app) instead of a browser tab. The normal browser URL still works alongside it.

### Windows

WebView2 (Edge) is built-in — no extra dependencies.

```bat
.venv\Scripts\activate
pip install -e ".[desktop]"
sturddle-view --desktop
```

### Linux

PyWebView requires GTK + WebKit2 and a real display (it will not work headless or reliably over SSH X forwarding). The `gi` bindings are system packages and cannot be pip-installed, so the venv must be created with `--system-site-packages`.

> **Note:** this replaces any existing `.venv` — your dependencies are reinstalled by the `pip install` step below.

```bash
# install system dependencies (once; package name may vary by distro)
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1

# recreate the venv to expose them
deactivate
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -e '.[dev,desktop]'

sturddle-view --desktop
```