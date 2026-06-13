"""Opening-book tool for the AI analysis agent.

`related_openings` is a pure lookup over the vendored opening dataset --
no engine, no eval. Given a family (or, by default, the opening of the
position under review), it returns that family's sibling variations with
their canonical SAN lines. It grounds a commentator's variation contrast
in real dataset lines instead of the model's memory.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

import chess

from ..env_utils import env_int
from ..llm import ToolSpec
from ..llm.cancel import CancelToken
from ..openings import OpeningBook


log = logging.getLogger(__name__)


BookProvider = Callable[[], OpeningBook | None]
BoardProvider = Callable[[], chess.Board | None]
OpeningsTool = Callable[..., Awaitable[dict]]


RELATED_OPENINGS_TOOL_NAME = "related_openings"

# Cap on siblings returned per call. A broad family (the Sicilian has dozens
# of variations) would otherwise flood the model's context.
_DEFAULT_RELATED_OPENINGS_MAX_N = 8
RELATED_OPENINGS_MAX_N = env_int(
    "SV_AI_RELATED_OPENINGS_MAX_N", _DEFAULT_RELATED_OPENINGS_MAX_N
)


_RELATED_OPENINGS_CARD = (
    "Use this to ground a variation contrast: the returned lines are real "
    "dataset openings, safe to name in prose -- opening moves recalled from "
    "memory are not. One call returns the whole family; read it, then name "
    "at most one contrast that bears on the current plan."
)


RELATED_OPENINGS_TOOL_SPEC = ToolSpec(
    name=RELATED_OPENINGS_TOOL_NAME,
    description=(
        "Opening variations near the position under review -- the named "
        "lines that branch at or just before it, nearest first, each with "
        "its canonical SAN line (eco, name, pgn, ply). Omit `family` for the "
        "neighbors of the current line; pass `family` (the part before the "
        "first ':' selects it) to list a specific family instead. Capped at "
        f"{RELATED_OPENINGS_MAX_N} lines."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "family": {
                "type": "string",
                "description": (
                    "Restrict to this family, e.g. 'Sicilian Defense' or "
                    "'Caro-Kann Defense: Advance Variation' (the part before "
                    "':' selects the family). Omit to get the neighbors of "
                    "the current line."
                ),
            },
        },
    },
    card=_RELATED_OPENINGS_CARD,
)


def make_related_openings_tool(
    book_provider: BookProvider,
    board_provider: BoardProvider,
) -> OpeningsTool:
    """Build the `related_openings` async tool. Ranks openings by shared
    move-prefix with the current line (the move-tree neighbors that branch
    at or near the position under review), nearest first. An optional
    `family` restricts the pool to one named family. Returns a structured
    error when neither a live position nor a family is available, so the
    model recovers or moves on.
    """
    async def related_openings(input_: dict, *, cancel_token: CancelToken) -> dict:
        book = book_provider()
        if book is None or len(book) == 0:
            return {"error": "no_opening_book"}
        raw_family = input_.get("family")
        # family_of strips its argument, so no need to pre-strip here; the
        # guard's strip() only rejects whitespace-only input.
        family = (
            book.family_of(raw_family)
            if isinstance(raw_family, str) and raw_family.strip()
            else None
        )
        board = board_provider()
        if board is None and family is None:
            return {"error": "no_live_position"}
        line = [m.uci() for m in board.move_stack] if board is not None else []
        rows = book.nearest(line, family=family, limit=RELATED_OPENINGS_MAX_N)
        out: dict = {"count": len(rows), "openings": [o.as_dict() for o in rows]}
        if family is not None:
            out["family"] = family
        return out

    return related_openings
