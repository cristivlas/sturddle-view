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

## Status

Skeleton only. Most endpoints return 501. See [docs/spec.md](docs/spec.md) for the
target shape.
