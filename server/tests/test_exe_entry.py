"""Regression tests for frozen-exe entry / --desktop / proxy dispatch.

The frozen exe re-invokes itself as ``<exe> proxy ...`` (fastchess engine
proxy). --desktop must never reach that path: it would land past the
``--`` separator and get passed to the real engine's argv, which rejects
it and never answers uci (the 0.4.6 tournament-in-desktop-mode bug).
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

from sturddle_view import __main__ as main_mod
from sturddle_view._runtime import DESKTOP_FLAG, PROXY_SUBCOMMAND, is_frozen

ENTRY_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "_exe_entry.py"


def _set_frozen(monkeypatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)


def test_frozen_defaults_to_desktop(monkeypatch):
    _set_frozen(monkeypatch)
    assert main_mod._build_parser().parse_args([]).desktop is True


def test_dev_defaults_to_no_desktop():
    assert is_frozen() is False
    assert main_mod._build_parser().parse_args([]).desktop is False


def test_proxy_dispatch_strips_token_and_leaks_no_desktop(monkeypatch):
    _set_frozen(monkeypatch)
    seen: list[str] = []
    monkeypatch.setattr(main_mod, "_proxy_main", lambda: seen.extend(sys.argv))
    proxy_argv = [
        "exe", PROXY_SUBCOMMAND,
        "--broadcast-url", "http://127.0.0.1:8765/internal/proxy",
        "--engine-name", "EngineA",
        "--", "engine.exe",
    ]
    monkeypatch.setattr(sys, "argv", list(proxy_argv))
    main_mod.main()
    assert seen == [proxy_argv[0]] + proxy_argv[2:]
    assert DESKTOP_FLAG not in seen


def test_entry_script_passes_argv_through_unmodified(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(main_mod, "main", lambda: seen.extend(sys.argv[1:]))
    argv = ["exe", PROXY_SUBCOMMAND, "--", "engine.exe"]
    monkeypatch.setattr(sys, "argv", list(argv))
    runpy.run_path(str(ENTRY_SCRIPT))
    assert seen == argv[1:]
