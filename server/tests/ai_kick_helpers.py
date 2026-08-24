"""Shared helpers for the _ai_kick user-message and opening-reply tests:
a settings stub with the book fields the probe reads, and sync wrappers
around the async turn-inputs builder."""
from __future__ import annotations

import asyncio

from sturddle_view.api._ai_kick import _build_turn_inputs


class FakeSettings:
    """Only the book fields the opening-reply probe reads; defaults to
    no configured book, override per test via kwargs."""

    def __init__(self, **kw):
        self.hve_use_opening_book = False
        self.engine_default_book_path = None
        self.engine_default_book_plies = None
        self.engine_default_book_order = None
        self.engine_default_book_cursor = 0
        self.__dict__.update(kw)


def build_inputs(hve, settings=None, eco_book=None):
    """Sync wrapper; defaults: no configured book, no ECO dataset."""
    return asyncio.run(_build_turn_inputs(hve, settings or FakeSettings(), eco_book))


def build_message(hve, settings=None, eco_book=None):
    """User message only; None when the turn has no inputs."""
    inputs = build_inputs(hve, settings, eco_book)
    return inputs.user_message if inputs is not None else None
