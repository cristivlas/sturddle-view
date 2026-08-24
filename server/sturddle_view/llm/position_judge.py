"""LLM clearing of regex position-check false positives.

The regex checks in `position_check` flag a SAN move or piece claim that
does not hold on the *current* board, but cannot read context: prose that
names a move legal only in a past, hypothetical, or alternate-line position
reads to it as illegal on the live board. This module asks the model, per
candidate, whether the prose actually asserts the move/claim about the
current board. It can only *clear* a candidate, never add one, so the worst
case is a let-through error -- the same as turning the regex off, guarded by
the same env flag.

The model is handed the FEN, the prose, and the exact flagged labels and
answers a narrow per-label yes/no; it is not asked to find errors itself.
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


JUDGE_SYSTEM_PROMPT = """\
You are a chess fact-checker. A fast pattern matcher has flagged moves or \
piece claims in a chess commentary as not matching the current board. The \
matcher is blind to context: it cannot tell a move described in a past, \
hypothetical, or alternate-line position from one asserted about the live \
board. Your job is to clear its false alarms, nothing more.

You are given the current position (FEN), the commentary prose, and a list \
of flagged items. For each item, decide whether the prose asserts that move \
or piece is valid *on the current board* (KEEP the flag) or is instead \
discussing a different position -- a past move, a hypothetical line, an \
alternative opening, an "after X" continuation, or an illustrative aside \
(CLEAR the flag).

Bias toward KEEP. Clear a flag only when the prose makes the other-context \
reading clear. When in doubt, keep it.

Reply with nothing but a JSON object wrapped in a VERDICT tag:

VERDICT {"clear": ["<label>", ...]}

`clear` lists the labels (copied verbatim from the flagged items) whose flag \
you are clearing. Use [] to clear none.\
"""


def _build_user_message(fen: str, prose: str, labels: list[str]) -> str:
    """The judge's single user turn: the board, the prose, and the flagged
    labels it must rule on, one per line."""
    flagged = "\n".join(f"- {label}" for label in labels)
    return (
        f"Current position (FEN): {fen}\n\n"
        f"Commentary:\n{prose}\n\n"
        f"Flagged items:\n{flagged}"
    )


def _parse_clear_labels(text: str, labels: list[str]) -> set[str]:
    """Labels the model voted to clear, intersected with the candidates so a
    hallucinated label can't clear something never flagged. Empty (clear
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
    cleared = payload.get("clear")
    if not isinstance(cleared, list):
        return set()
    candidate = set(labels)
    return {c for c in cleared if isinstance(c, str) and c in candidate}


async def clear_false_positives(
    provider: LLMProvider,
    board: chess.Board,
    prose: str,
    labels: list[str],
) -> set[str]:
    """Labels among `labels` the model judges to be other-context references
    (past/hypothetical/alternate line) rather than live-board assertions, so
    their regex flag is a false positive and should be dropped.

    One `stream()` call, thinking forced off (the reasoning is shallow).
    Returns a subset of `labels`; never adds. Any failure (empty labels,
    provider error, unparseable reply) clears nothing, so the regex verdict
    stands."""
    if not labels:
        return set()
    messages: list[Message] = [
        {"role": "user", "content": _build_user_message(board.fen(), prose, labels)}
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
    cleared = _parse_clear_labels("".join(parts), labels)
    if cleared:
        log.info("position judge cleared %d/%d flags: %s", len(cleared), len(labels), cleared)
    return cleared
