"""Playbook rendering: chess-theory plans keyed on Situation tags, rendered
as the one `Plan:` line the narrator's `plan` tool serves
(docs/ai-playbook-spec.md). The text lives in the playbook catalog
(play/playbook_catalog.py); the combination rules live here. Fragments
name plans, never board tactics; the verifier never sees the line.
"""
from __future__ import annotations

import chess

from ..chess.results import SIDE_BLACK, SIDE_WHITE
from ..env_utils import env_int
from ..play.playbook import Situation
from ..play.playbook_catalog import (
    NOTE_CRUSHING,
    NOTE_LOST,
    NOTE_REPEAT_AHEAD,
    NOTE_REPEAT_BEHIND,
    Catalog,
    default_catalog,
)
from ..play.playbook_tags import (
    AHEAD_MARGINS,
    AXIS_MARGIN,
    AXIS_PHASE,
    AXIS_STRUCTURE,
    BASE_MARGIN,
    BEHIND_MARGINS,
    MARGIN_BETTER,
    MARGIN_CRUSHING,
    MARGIN_EVEN,
    MARGIN_LOSING,
    MARGIN_LOST,
    MARGIN_WINNING,
    MARGIN_WORSE,
    PHASE_ENDGAME,
    PHASE_OPENING,
    SOURCE_MATERIAL,
)
from .prompts import COACH_MODE, PLAYBOOK_LEAD, PromptMode


# Upper bound on fragments per turn; the crushing / lost note wins a slot.
_DEFAULT_MAX_FRAGMENTS = 3
MAX_FRAGMENTS = env_int("SV_AI_PLAYBOOK_MAX_FRAGMENTS", _DEFAULT_MAX_FRAGMENTS)

_MARGIN_WORDS = {
    MARGIN_LOST: "lost",
    MARGIN_LOSING: "clearly worse",
    MARGIN_WORSE: "slightly worse",
    MARGIN_EVEN: "level",
    MARGIN_BETTER: "slightly better",
    MARGIN_WINNING: "clearly better",
    MARGIN_CRUSHING: "winning outright",
}
_BY_MATERIAL = " by material"
_YOU = "you"

_MOVES_JOIN = ", "
_NOTE_SEP = " "
_SIDE_WORDS = {chess.WHITE: SIDE_WHITE.capitalize(), chess.BLACK: SIDE_BLACK.capitalize()}


def _standing(situation: Situation, subject: str) -> str:
    """'you are slightly better by material' / 'White is level'."""
    verb = "are" if subject == _YOU else "is"
    source = _BY_MATERIAL if situation.margin_source == SOURCE_MATERIAL else ""
    return f"{subject} {verb} {_MARGIN_WORDS[situation.margin]}{source}"


def _specific(situation: Situation, catalog: Catalog) -> list[str]:
    """Specific-tag fragments in the catalog's composed registry order."""
    tags = ((axis, getattr(situation, axis)) for axis in catalog.specific_axes)
    return [catalog.fragment(axis, tag) for axis, tag in tags if tag is not None]


def _axis_fragments(situation: Situation, catalog: Catalog) -> list[str]:
    base = BASE_MARGIN.get(situation.margin, situation.margin)
    combo = catalog.combo(base, AXIS_PHASE, situation.phase)
    combo_on_phase = combo is not None
    if combo is None and situation.structure is not None:
        combo = catalog.combo(base, AXIS_STRUCTURE, situation.structure)
    phase = catalog.fragment(AXIS_PHASE, situation.phase)
    structure = (
        [] if situation.structure is None
        else [catalog.fragment(AXIS_STRUCTURE, situation.structure)]
    )
    specific = _specific(situation, catalog)
    # Outside the endgame, phase is the generic fallback: specific tags replace it.
    phase_slot = specific or [phase]
    if combo_on_phase:
        return [combo, *specific, *structure]
    if combo is not None:
        return [combo, *phase_slot]
    margin = catalog.fragment(AXIS_MARGIN, base)
    margins = [margin] if margin else []
    # Phase leads in the endgame (promotion first), margin otherwise.
    if situation.phase == PHASE_ENDGAME:
        return [phase, *margins, *specific, *structure]
    return [*margins, *phase_slot, *structure]


def _notes(situation: Situation, mode: PromptMode, catalog: Catalog) -> list[str]:
    if situation.margin == MARGIN_CRUSHING:
        return [catalog.note(NOTE_CRUSHING)]
    if situation.margin == MARGIN_LOST and mode == COACH_MODE and situation.phase != PHASE_OPENING:
        return [catalog.note(NOTE_LOST)]
    return []


def _fragments(situation: Situation, mode: PromptMode, catalog: Catalog) -> list[str]:
    # The note counts against the cap and wins: resign / convert advice
    # never loses its slot to an axis fragment.
    notes = _notes(situation, mode, catalog)[:MAX_FRAGMENTS]
    return _axis_fragments(situation, catalog)[:MAX_FRAGMENTS - len(notes)] + notes


def _repeat_note(situation: Situation, catalog: Catalog) -> str:
    # Rides outside the fragment cap: concrete moves, and the margin decides
    # the verdict (draw is the goal behind, the enemy ahead).
    if not situation.repeats:
        return ""
    moves = _MOVES_JOIN.join(situation.repeats)
    if situation.margin in BEHIND_MARGINS:
        return catalog.note(NOTE_REPEAT_BEHIND, moves=moves)
    if situation.margin in AHEAD_MARGINS:
        return catalog.note(NOTE_REPEAT_AHEAD, moves=moves)
    return ""


def render_playbook(
    situation: Situation,
    mode: PromptMode,
    side_to_move: chess.Color,
    catalog: Catalog | None = None,
) -> str:
    """The plan line. Coach speaks to the player ('you'); the commentator
    gets neutral third person for the side to move. `catalog` defaults to
    the process catalog."""
    if catalog is None:
        catalog = default_catalog()
    subject = _YOU if mode == COACH_MODE else _SIDE_WORDS[side_to_move]
    body = _NOTE_SEP.join(_fragments(situation, mode, catalog))
    note = _repeat_note(situation, catalog)
    line = f"{PLAYBOOK_LEAD}{_standing(situation, subject)}; {body}"
    return f"{line}{_NOTE_SEP}{note}" if note else line
