"""Runtime environment detection: dev checkout vs frozen bundle.

Single source of truth for two concerns that differ between the two modes:
  1. Where is the application root directory (web assets, data files)?
  2. How is the stdio proxy subprocess invoked?

All other modules import from here -- nothing else touches sys.frozen or
sys._MEIPASS directly.
"""
from __future__ import annotations

import sys
from pathlib import Path


def app_root() -> Path:
    """Absolute path to the application root directory.

    Frozen bundle: the PyInstaller extraction directory, which contains
    the bundled web/ tree and data files alongside the package code.
    Dev checkout: the repository root (two levels above this file's
    package directory: server/sturddle_view/ -> server/ -> repo root).
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parents[2]


def proxy_argv_prefix() -> list[str]:
    """Argv prefix for spawning the stdio proxy as a subprocess.

    Dev:    [sys.executable, "-m", "sturddle_view.tournament.proxy"]
    Frozen: [sys.executable, "proxy"]

    In the frozen case the exe dispatches the "proxy" subcommand to
    sturddle_view.tournament.proxy.main() -- see __main__.py.
    The caller appends the remaining proxy flags after this prefix.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "proxy"]
    return [sys.executable, "-m", "sturddle_view.tournament.proxy"]
