"""Playbook fragments: chess-theory advice keyed on Situation tags, rendered
as one narrator-only steer line on the initial user message
(docs/ai-playbook-spec.md). Fragments name plans, never board tactics.
"""
from __future__ import annotations

import chess

from ..chess.results import SIDE_BLACK, SIDE_WHITE
from ..env_utils import env_int
from ..play.playbook import (
    MARGIN_BETTER,
    MARGIN_CRUSHING,
    MARGIN_EVEN,
    MARGIN_LOSING,
    MARGIN_LOST,
    MARGIN_WINNING,
    MARGIN_WORSE,
    PHASE_ENDGAME,
    PHASE_LATE,
    PHASE_MIDDLEGAME,
    PHASE_OPENING,
    SOURCE_MATERIAL,
    STRUCTURE_CLOSED,
    STRUCTURE_OPEN,
    Situation,
)
from .prompts import COACH_MODE, PLAYBOOK_LEAD, PromptMode


# Upper bound on fragments per turn (combo + third axis + margin note).
_DEFAULT_MAX_FRAGMENTS = 3
MAX_FRAGMENTS = env_int("SV_AI_PLAYBOOK_MAX_FRAGMENTS", _DEFAULT_MAX_FRAGMENTS)

# Per-axis fragments. Imperative plan language works under both leads.
_MARGIN_FRAGMENTS = {
    MARGIN_WINNING: (
        "convert cleanly: simplify, trade pieces not pawns, keep the king "
        "safe, avoid needless complications."
    ),
    MARGIN_BETTER: (
        "keep pieces on, improve the worst-placed piece, no rush; do not "
        "cash the edge in for a simplified drawn position."
    ),
    MARGIN_WORSE: (
        "solidify first; trade off the most active enemy piece; trade "
        "queens if under attack."
    ),
    MARGIN_LOSING: (
        "complicate, seek counterplay and practical chances; passive "
        "defense loses slowly."
    ),
}
_MARGIN_FRAGMENTS[MARGIN_CRUSHING] = _MARGIN_FRAGMENTS[MARGIN_WINNING]
_MARGIN_FRAGMENTS[MARGIN_LOST] = _MARGIN_FRAGMENTS[MARGIN_LOSING]

# Appended after the margin/combo text for the extreme buckets.
_CRUSHING_NOTE = "The position is decisively won; no creativity is needed."
_LOST_NOTE = (
    "The position is objectively lost: name resignation as an option once, "
    "without insisting, and still give the most stubborn try."
)

_PHASE_FRAGMENTS = {
    PHASE_OPENING: (
        "development, the center and king safety before adventures; no "
        "early queen sorties."
    ),
    PHASE_MIDDLEGAME: (
        "pawn breaks, piece activity, weak squares; a plan before tactics."
    ),
    PHASE_LATE: (
        "trade toward a favorable ending when ahead, keep tension when "
        "behind; watch the transition."
    ),
    PHASE_ENDGAME: (
        "king activity, create and push passed pawns, promotion is the "
        "goal; opposition and zugzwang in pawn endings."
    ),
}

_STRUCTURE_FRAGMENTS = {
    STRUCTURE_CLOSED: (
        "closed structure: maneuver, prepare pawn breaks, knights over "
        "bishops, play on the wing with more space."
    ),
    STRUCTURE_OPEN: (
        "open structure: bishops over knights, files and diagonals, "
        "initiative and tactics; tempo matters."
    ),
}

# Combo overrides replace both axes' fragments; keyed on the base margin
# (lost -> losing, crushing -> winning) and a phase or structure tag.
_COMBO_FRAGMENTS = {
    (MARGIN_LOSING, STRUCTURE_CLOSED): (
        "behind in a closed position: keep it closed, avoid opening lines "
        "for the stronger side; wait for a mistake, prepare one break."
    ),
    (MARGIN_LOSING, STRUCTURE_OPEN): (
        "behind in an open position: complicate now, activity and "
        "initiative over material; open lines toward the enemy king."
    ),
    (MARGIN_WINNING, STRUCTURE_CLOSED): (
        "ahead in a closed position: do not force it; prepare the pawn "
        "break on the side with more space, then open at the right moment."
    ),
    (MARGIN_WINNING, STRUCTURE_OPEN): (
        "ahead in an open position: trade pieces, keep pawns, hold the "
        "open files; simplification is the win."
    ),
    (MARGIN_WINNING, PHASE_ENDGAME): (
        "ahead in the endgame: king to the center, create the passed pawn, "
        "promote; trade pieces not pawns."
    ),
    (MARGIN_LOSING, PHASE_ENDGAME): (
        "behind in the endgame: king activity and counterplay; seek the "
        "drawing fortress or opposite-colored bishops before it is too late."
    ),
    (MARGIN_WORSE, PHASE_OPENING): (
        "slightly worse in the opening: finish development first, no pawn "
        "grabs; castle and consolidate."
    ),
    (MARGIN_BETTER, PHASE_OPENING): (
        "slightly better in the opening: keep developing, do not cash in "
        "early; punish the lag later."
    ),
}

_BASE_MARGIN = {MARGIN_LOST: MARGIN_LOSING, MARGIN_CRUSHING: MARGIN_WINNING}

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
_SIDE_WORDS = {chess.WHITE: SIDE_WHITE.capitalize(), chess.BLACK: SIDE_BLACK.capitalize()}


def _standing(situation: Situation, subject: str) -> str:
    """'you are slightly better by material' / 'White is level'."""
    verb = "are" if subject == _YOU else "is"
    source = _BY_MATERIAL if situation.margin_source == SOURCE_MATERIAL else ""
    return f"{subject} {verb} {_MARGIN_WORDS[situation.margin]}{source}"


def _fragments(situation: Situation, mode: PromptMode) -> list[str]:
    base = _BASE_MARGIN.get(situation.margin, situation.margin)
    combo = _COMBO_FRAGMENTS.get((base, situation.phase))
    combo_axis = situation.phase
    if combo is None and situation.structure is not None:
        combo = _COMBO_FRAGMENTS.get((base, situation.structure))
        combo_axis = situation.structure
    out: list[str] = []
    if combo is not None:
        out.append(combo)
        if combo_axis == situation.phase and situation.structure is not None:
            out.append(_STRUCTURE_FRAGMENTS[situation.structure])
        elif combo_axis != situation.phase:
            out.append(_PHASE_FRAGMENTS[situation.phase])
    else:
        margin = _MARGIN_FRAGMENTS.get(situation.margin)
        phase = _PHASE_FRAGMENTS[situation.phase]
        # Phase leads in the endgame (promotion first), margin otherwise.
        ordered = [phase, margin] if situation.phase == PHASE_ENDGAME else [margin, phase]
        out.extend(f for f in ordered if f)
        if situation.structure is not None:
            out.append(_STRUCTURE_FRAGMENTS[situation.structure])
    if situation.margin == MARGIN_CRUSHING:
        out.append(_CRUSHING_NOTE)
    elif situation.margin == MARGIN_LOST and mode == COACH_MODE and situation.phase != PHASE_OPENING:
        out.append(_LOST_NOTE)
    return out[:MAX_FRAGMENTS]


def render_playbook(situation: Situation, mode: PromptMode, side_to_move: chess.Color) -> str:
    """The steer line. Coach speaks to the player ('you'); the commentator
    gets neutral third person for the side to move."""
    subject = _YOU if mode == COACH_MODE else _SIDE_WORDS[side_to_move]
    body = " ".join(_fragments(situation, mode))
    return f"{PLAYBOOK_LEAD}{_standing(situation, subject)}; {body}"
