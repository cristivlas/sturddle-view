"""Membership pin for the client's FORCED_TERMINATIONS set.

The client mirrors the server's start_analysis guard with a hand-listed
set of termination strings; nothing at runtime ties it to python-chess.
This compares the set parsed out of play.js source verbatim to the
enum-derived names, so a python-chess rename or a client-side drop,
typo, or stray addition fails fast in the default (non-e2e) suite --
including for endings no short PGN can reach (seventyfive_moves,
fivefold_repetition, insufficient_material).
"""
from __future__ import annotations

from .forced_terminations import FORCED_PY, client_forced_set


def test_client_forced_set_matches_python_chess() -> None:
    assert client_forced_set() == FORCED_PY
