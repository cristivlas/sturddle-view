"""LLM judging of regex position-check flags.

The regex checks in `position_check` flag a SAN move or piece claim that
does not hold on the *current* board, but cannot read context: prose about
an earlier position, or a later one in a line under analysis, reads to it as
false on the live board. This module asks the model, per flag, whether the
prose places the item in such another position. A tagged flag is not struck;
the coordinator asks the narrator to name such positions from then on. The
judge can only tag a flag, never add one.

The model is handed the FEN, the prose, and each flagged label with the
board fact that makes it false, and answers a narrow per-label question; it
is not asked to find errors itself.
"""
from __future__ import annotations

import json
import logging
import re

import chess

from .base import LLMProvider, Message

log = logging.getLogger(__name__)


# Name the judge round trip is surfaced under in the panel's tool list.
POSITION_JUDGE_CALL_NAME = "position_judge"

# Sentinel the model wraps its verdict in, so a chatty model that adds prose
# around the JSON still parses.
_VERDICT_TAG = "VERDICT"
# Greedy: if the model emits two objects this spans both and json.loads fails
# -> clears nothing, the safe direction. A single object is the contract.
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


_VERDICT_KEY = "other_position"

JUDGE_SYSTEM_PROMPT = f"""\
You review chess commentary written about one position, given as a FEN.

A literal checker compared the commentary with that position and flagged the \
items listed by the user. Each flagged item is false in that position; the \
board fact next to it says why. So an item the commentary presents as true \
now is an error.

The checker cannot follow context. Commentary also discusses other positions \
of the same game: an earlier one ("before 21.Rxd7, the bishop on d7 guarded \
c6") or a later one reached in a line under analysis ("after 24...Bd7, the \
bishop on d7 eyes the long diagonal"). An item placed in such a position is \
not false, only ambiguous; its writer will be asked to name that position \
explicitly.

For each flagged item, decide which it is:
- CURRENT: the commentary presents it as true in the given position. Present \
tense, possessives ("your bishop on d7"), and plans or threats for the side \
to move all describe the given position.
- OTHER: the surrounding words clearly place it in a different position -- a \
past event, or a move sequence that leads there ("after ...", "if ...", a \
numbered line).

Answer OTHER only when the text makes the other position clear. When in \
doubt, answer CURRENT.

Reply with only this line, listing the OTHER items verbatim as flagged:

{_VERDICT_TAG} {{"{_VERDICT_KEY}": ["<item>", ...]}}

Use an empty list when every item is CURRENT.\
"""


def _build_user_message(board: chess.Board, prose: str, items: list[tuple[str, str]]) -> str:
    """The judge's single user turn: the board, the prose, and each flagged
    label with the board fact that makes it false, one per line."""
    side = "White" if board.turn == chess.WHITE else "Black"
    flagged = "\n".join(f"- {label} (board: {fact})" for label, fact in items)
    return (
        f"Position (FEN): {board.fen()}\n"
        f"Side to move: {side}\n\n"
        f"Commentary:\n{prose}\n\n"
        f"Flagged items:\n{flagged}"
    )


def _parse_other_position_labels(text: str, labels: list[str]) -> set[str]:
    """Labels the model tagged OTHER, intersected with the candidates so a
    hallucinated label can't tag something never flagged. Empty (tag
    nothing) on any parse failure -- the safe direction is to keep the flag."""
    after = text.split(_VERDICT_TAG, 1)[-1]
    m = _JSON_OBJ_RE.search(after)
    if m is None:
        log.warning("position judge: no JSON verdict in %r", text)
        return set()
    try:
        payload = json.loads(m.group(0))
    except (ValueError, TypeError):
        log.warning("position judge: unparseable verdict in %r", text)
        return set()
    tagged = payload.get(_VERDICT_KEY)
    if not isinstance(tagged, list):
        return set()
    candidate = set(labels)
    return {c for c in tagged if isinstance(c, str) and c in candidate}


async def judge_other_position(
    provider: LLMProvider,
    board: chess.Board,
    prose: str,
    items: list[tuple[str, str]],
) -> set[str]:
    """Labels among `items` ((label, board fact) pairs) the model judges the
    prose to place in another position (earlier, or later in a line), rather
    than assert about the live board.

    One `stream()` call, thinking forced off (the reasoning is shallow).
    Returns a subset of the labels; never adds. Any failure (no items,
    provider error, unparseable reply) tags nothing, so the regex verdict
    stands."""
    if not items:
        return set()
    labels = [label for label, _fact in items]
    messages: list[Message] = [
        {"role": "user", "content": _build_user_message(board, prose, items)}
    ]
    parts: list[str] = []
    try:
        async for chunk in provider.stream(
            system=JUDGE_SYSTEM_PROMPT,
            messages=messages,
            tools=None,
            thinking=False,
        ):
            if chunk.kind == "text" and chunk.text:
                parts.append(chunk.text)
    except Exception:
        log.error("position judge call failed; keeping all regex flags", exc_info=True)
        return set()
    tagged = _parse_other_position_labels("".join(parts), labels)
    if tagged:
        log.info("position judge cleared %d/%d flags: %s", len(tagged), len(labels), tagged)
    return tagged
