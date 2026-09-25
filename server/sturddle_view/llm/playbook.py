"""Playbook fragments: chess-theory plans keyed on Situation tags, rendered
as the one `Plan:` line on the initial user message that decides the
narrator's pick (docs/ai-playbook-spec.md). Fragments name plans, never
board tactics; the verifier never sees the line.
"""
from __future__ import annotations

import chess

from ..chess.results import SIDE_BLACK, SIDE_WHITE
from ..env_utils import env_int
from ..play.playbook import (
    AHEAD_MARGINS,
    BEHIND_MARGINS,
    BISHOPS_OPPOSITE,
    BISHOPS_OPPOSITE_QUEENS,
    CASTLING_OPPOSITE,
    IMBALANCE_IQP_OURS,
    IMBALANCE_IQP_THEIRS,
    IMBALANCE_MINORITY_OURS,
    IMBALANCE_MINORITY_THEIRS,
    MARGIN_BETTER,
    MARGIN_CRUSHING,
    MARGIN_EVEN,
    MARGIN_LOSING,
    MARGIN_LOST,
    MARGIN_WINNING,
    MARGIN_WORSE,
    PAWN_PASSED_OUTSIDE_OURS,
    PAWN_PASSED_OUTSIDE_THEIRS,
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


# Upper bound on fragments per turn; the crushing / lost note wins a slot.
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
        "seek counterplay and practical chances; prolong the game: no "
        "simplifying trades, keep pieces and tension on, make the opponent "
        "prove the win."
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
        "development, the center and king safety before premature attacks; "
        "no early queen sorties."
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

# Specific-tag fragments; outside the endgame they replace the phase one.
_IMBALANCE_FRAGMENTS = {
    IMBALANCE_IQP_OURS: (
        "isolated d-pawn: control its stop square, play actively now, avoid "
        "endings where it is a target."
    ),
    IMBALANCE_IQP_THEIRS: (
        "enemy isolated d-pawn: blockade it with a knight, trade minor pieces, "
        "aim for an ending."
    ),
    IMBALANCE_MINORITY_OURS: (
        "minority attack: advance the a- and b-pawns, trade one to leave a "
        "weak pawn, target it."
    ),
    IMBALANCE_MINORITY_THEIRS: (
        "enemy minority attack: counter in the center or on the kingside; "
        "guard the weakened queenside pawn."
    ),
}

_BISHOPS_FRAGMENTS = {
    BISHOPS_OPPOSITE_QUEENS: (
        "opposite-colored bishops with queens on: the attacker keeps queens "
        "on and attacks on its bishop's color."
    ),
    BISHOPS_OPPOSITE: (
        "opposite-colored bishops: mass trades tend to draw; the side ahead "
        "needs a second front or outside passer."
    ),
}

_PAWN_FRAGMENTS = {
    PAWN_PASSED_OUTSIDE_OURS: (
        "outside passed pawn: push and support it; it ties enemy pieces down."
    ),
    PAWN_PASSED_OUTSIDE_THEIRS: (
        "enemy outside passed pawn: blockade it with a minor piece; keep the "
        "other pieces free."
    ),
}

_CASTLING_FRAGMENTS = {
    CASTLING_OPPOSITE: (
        "opposite-side castling: pawn-storm the enemy king's wing without "
        "loosening the king's shelter; tempo counts."
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

# Repetition notes ride outside the fragment cap: concrete moves, and the
# margin decides the verdict (draw is the goal behind, the enemy ahead).
_REPEAT_BEHIND_NOTE = (
    "A repetition is available ({moves}): repeating is the drawing try; "
    "take it unless a move clearly improves."
)
_REPEAT_AHEAD_NOTE = "Do not repeat the position ({moves}); make progress."
_MOVES_JOIN = ", "
_NOTE_SEP = " "
_SIDE_WORDS = {chess.WHITE: SIDE_WHITE.capitalize(), chess.BLACK: SIDE_BLACK.capitalize()}


def _standing(situation: Situation, subject: str) -> str:
    """'you are slightly better by material' / 'White is level'."""
    verb = "are" if subject == _YOU else "is"
    source = _BY_MATERIAL if situation.margin_source == SOURCE_MATERIAL else ""
    return f"{subject} {verb} {_MARGIN_WORDS[situation.margin]}{source}"


def _specific(situation: Situation) -> list[str]:
    """Specific-tag fragments in precedence order."""
    tagged = (
        (_IMBALANCE_FRAGMENTS, situation.imbalance),
        (_BISHOPS_FRAGMENTS, situation.bishops),
        (_PAWN_FRAGMENTS, situation.pawn),
        (_CASTLING_FRAGMENTS, situation.castling),
    )
    return [catalog[tag] for catalog, tag in tagged if tag is not None]


def _axis_fragments(situation: Situation) -> list[str]:
    base = _BASE_MARGIN.get(situation.margin, situation.margin)
    combo = _COMBO_FRAGMENTS.get((base, situation.phase))
    combo_on_phase = combo is not None
    if combo is None and situation.structure is not None:
        combo = _COMBO_FRAGMENTS.get((base, situation.structure))
    phase = _PHASE_FRAGMENTS[situation.phase]
    structure = [] if situation.structure is None else [_STRUCTURE_FRAGMENTS[situation.structure]]
    specific = _specific(situation)
    # Outside the endgame, phase is the generic fallback: specific tags replace it.
    phase_slot = specific or [phase]
    if combo_on_phase:
        return [combo, *specific, *structure]
    if combo is not None:
        return [combo, *phase_slot]
    margin = _MARGIN_FRAGMENTS.get(situation.margin)
    margins = [margin] if margin else []
    # Phase leads in the endgame (promotion first), margin otherwise.
    if situation.phase == PHASE_ENDGAME:
        return [phase, *margins, *specific, *structure]
    return [*margins, *phase_slot, *structure]


def _notes(situation: Situation, mode: PromptMode) -> list[str]:
    if situation.margin == MARGIN_CRUSHING:
        return [_CRUSHING_NOTE]
    if situation.margin == MARGIN_LOST and mode == COACH_MODE and situation.phase != PHASE_OPENING:
        return [_LOST_NOTE]
    return []


def _fragments(situation: Situation, mode: PromptMode) -> list[str]:
    # The note counts against the cap and wins: resign / convert advice
    # never loses its slot to an axis fragment.
    notes = _notes(situation, mode)[:MAX_FRAGMENTS]
    return _axis_fragments(situation)[:MAX_FRAGMENTS - len(notes)] + notes


def _repeat_note(situation: Situation) -> str:
    if not situation.repeats:
        return ""
    moves = _MOVES_JOIN.join(situation.repeats)
    if situation.margin in BEHIND_MARGINS:
        return _REPEAT_BEHIND_NOTE.format(moves=moves)
    if situation.margin in AHEAD_MARGINS:
        return _REPEAT_AHEAD_NOTE.format(moves=moves)
    return ""


def render_playbook(situation: Situation, mode: PromptMode, side_to_move: chess.Color) -> str:
    """The plan line. Coach speaks to the player ('you'); the commentator
    gets neutral third person for the side to move."""
    subject = _YOU if mode == COACH_MODE else _SIDE_WORDS[side_to_move]
    body = _NOTE_SEP.join(_fragments(situation, mode))
    note = _repeat_note(situation)
    line = f"{PLAYBOOK_LEAD}{_standing(situation, subject)}; {body}"
    return f"{line}{_NOTE_SEP}{note}" if note else line
