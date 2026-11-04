"""Playbook tag vocabulary (docs/ai-playbook-spec.md, ai-playbook-matchers-spec.md).

Shared by the classifier (play/playbook.py), the matchers
(play/matchers.py), the catalog and the renderer. Strings, so they render
into prompt keys and playbook-file `when` tables directly.
"""
from __future__ import annotations

# Axes. Each names a `Situation` field.
AXIS_MARGIN = "margin"
AXIS_PHASE = "phase"
AXIS_STRUCTURE = "structure"
AXIS_IMBALANCE = "imbalance"
AXIS_BISHOPS = "bishops"
AXIS_PAWN = "pawn"
AXIS_CASTLING = "castling"
AXIS_FORMATION = "formation"

MARGIN_LOST = "lost"
MARGIN_LOSING = "losing"
MARGIN_WORSE = "worse"
MARGIN_EVEN = "even"
MARGIN_BETTER = "better"
MARGIN_WINNING = "winning"
MARGIN_CRUSHING = "crushing"
AHEAD_MARGINS = frozenset({MARGIN_BETTER, MARGIN_WINNING, MARGIN_CRUSHING})
BEHIND_MARGINS = frozenset({MARGIN_WORSE, MARGIN_LOSING, MARGIN_LOST})
# The extreme margins share their neighbor's plan text.
BASE_MARGIN = {MARGIN_LOST: MARGIN_LOSING, MARGIN_CRUSHING: MARGIN_WINNING}
BASE_MARGINS = frozenset({
    MARGIN_LOSING, MARGIN_WORSE, MARGIN_EVEN, MARGIN_BETTER, MARGIN_WINNING,
})

PHASE_OPENING = "opening"
PHASE_MIDDLEGAME = "middlegame"
PHASE_LATE = "late"
PHASE_ENDGAME = "endgame"
PHASES = frozenset({PHASE_OPENING, PHASE_MIDDLEGAME, PHASE_LATE, PHASE_ENDGAME})

STRUCTURE_OPEN = "open"
STRUCTURE_CLOSED = "closed"

SOURCE_EVAL = "eval"
SOURCE_MATERIAL = "material"

# Specific tags. `_ours` / `_theirs` take our side's point of view.
_OPPOSITE = "opposite"
IMBALANCE_IQP_OURS = "iqp_ours"
IMBALANCE_IQP_THEIRS = "iqp_theirs"
IMBALANCE_MINORITY_OURS = "minority_ours"
IMBALANCE_MINORITY_THEIRS = "minority_theirs"
BISHOPS_OPPOSITE_QUEENS = "opposite_queens"
BISHOPS_OPPOSITE = _OPPOSITE
PAWN_PASSED_OUTSIDE_OURS = "passed_outside_ours"
PAWN_PASSED_OUTSIDE_THEIRS = "passed_outside_theirs"
CASTLING_OPPOSITE = _OPPOSITE
