"""PGN-based standings, Elo, and SPRT computation.

The runner's stdout summary is **not** the source of truth — these
functions parse ``games.pgn`` directly. This means a ``Stop`` mid-run
preserves prior games' contribution to standings, and (later) a Resume
that appends to the same PGN yields correct cumulative numbers without
special handling.

Pure functions; no I/O beyond reading the PGN.

Stub for Slice 0 of the tournament implementation plan; concrete
implementation lands in Slice 2.
"""
from __future__ import annotations

from pathlib import Path


def compute_standings(pgn_path: Path):
    raise NotImplementedError


def compute_sprt(pgn_path: Path, params: dict):
    raise NotImplementedError
