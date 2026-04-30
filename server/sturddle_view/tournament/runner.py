"""Runner protocol — abstract surface for tournament-manager backends.

The Phase 1 implementation is ``FastchessRunner`` in ``fastchess.py``. The
abstraction exists so a ``CutechessRunner`` can be added later without
rewriting callers; it does not exist as a generalized facade speculating
on capabilities of unknown future runners.

Stub for Slice 0 of the tournament implementation plan.
"""
from __future__ import annotations

from typing import Protocol


class Runner(Protocol):
    async def start(self, tournament, on_event) -> None: ...
    async def stop(self) -> None: ...
    def is_running(self) -> bool: ...
