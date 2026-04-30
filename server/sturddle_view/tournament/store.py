"""On-disk persistence for tournaments.

Owns the directory tree under ``<tournaments-root>/<id>/`` and the
``state.json`` schema. No subprocess knowledge.

Layout per tournament (see ``docs/tournament-spec.md``):

    <tournaments-root>/
      <id>/
        state.json     ← wrapper-owned: id, name, status, timestamps, frozen template
        config.json    ← fastchess-owned (resume artifact)
        games.pgn      ← fastchess-owned (append=true)
        logs/
          wrapper.log
          fastchess.log

This module is a stub for Slice 0 of the tournament implementation
plan; concrete implementation lands in Slice 1.
"""
from __future__ import annotations

from pathlib import Path


class TournamentStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def create(self, *args, **kwargs):
        raise NotImplementedError

    def get(self, tournament_id: str):
        raise NotImplementedError

    def list(self):
        raise NotImplementedError

    def remove(self, tournament_id: str) -> None:
        raise NotImplementedError

    def update_status(self, tournament_id: str, status: str, **timestamps) -> None:
        raise NotImplementedError
