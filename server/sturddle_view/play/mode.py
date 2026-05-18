"""Mode FSM for HumanVsEngine: Mode enum, Op enum, allowed-mode matrix, typed conflict error."""
from __future__ import annotations

from enum import Enum, IntEnum


class Mode(IntEnum):
    """Power-of-2 values so allowed-mode checks reduce to a bitwise AND."""
    PLAY      = 1 << 0
    PAUSED    = 1 << 1
    VIEWING   = 1 << 2
    EDITING   = 1 << 3
    ANALYZING = 1 << 4


class Op(Enum):
    SUBMIT_MOVE     = "submit_move"
    TAKEBACK        = "takeback"
    SWITCH_SIDES    = "switch_sides"
    RESIGN          = "resign"
    PAUSE           = "pause"
    RESUME          = "resume"
    START_ANALYSIS  = "start_analysis"
    ENTER_VIEW_MODE = "enter_view_mode"
    ENTER_EDIT_MODE = "enter_edit_mode"
    VIEW_GOTO       = "view_goto"
    PLAY_FROM_HERE  = "play_from_here"
    COMMIT_EDIT     = "commit_edit"
    CANCEL_EDIT     = "cancel_edit"
    NEW_GAME        = "new_game"


class ModeConflictError(RuntimeError):
    def __init__(self, current: Mode, attempted: Op) -> None:
        super().__init__(f"op {attempted.name} not allowed in mode {current.name}")
        self.current = current
        self.attempted = attempted


# Allowed modes per operation, reverse-engineered from HVE guard clauses.
# Stored as an integer bitmask on each Op instance (_mask attribute) so
# assert_allowed reduces to a single bitwise AND -- no dict or set lookup.
ALLOWED_MODES_BY_OP: dict[Op, frozenset[Mode]] = {
    Op.SUBMIT_MOVE:     frozenset({Mode.PLAY}),
    Op.TAKEBACK:        frozenset({Mode.PLAY, Mode.PAUSED}),
    Op.SWITCH_SIDES:    frozenset({Mode.PLAY}),
    Op.RESIGN:          frozenset({Mode.PLAY, Mode.PAUSED}),
    Op.PAUSE:           frozenset({Mode.PLAY}),
    Op.RESUME:          frozenset({Mode.PAUSED}),
    Op.START_ANALYSIS:  frozenset({Mode.PAUSED, Mode.VIEWING}),
    Op.ENTER_VIEW_MODE: frozenset({Mode.PLAY, Mode.PAUSED, Mode.VIEWING, Mode.ANALYZING}),
    Op.ENTER_EDIT_MODE: frozenset({Mode.VIEWING}),
    Op.VIEW_GOTO:       frozenset({Mode.VIEWING}),
    Op.PLAY_FROM_HERE:  frozenset({Mode.VIEWING}),
    Op.COMMIT_EDIT:     frozenset({Mode.EDITING}),
    Op.CANCEL_EDIT:     frozenset({Mode.EDITING}),
    Op.NEW_GAME:        frozenset({Mode.PLAY, Mode.PAUSED, Mode.VIEWING, Mode.ANALYZING}),
}

# Attach precomputed bitmask to each Op for O(1) assert_allowed.
for _op, _modes in ALLOWED_MODES_BY_OP.items():
    object.__setattr__(_op, "_mask", int(sum(_modes)))


def assert_allowed(mode: Mode, op: Op) -> None:
    # Matrix test only. HVE guard sites inline `if not (self._mode & Op.X._mask)`
    # -- do NOT refactor back to a call here; Python fn-call overhead breaks the
    # 10% perf gate. test_bench_mode_guard catches any regression.
    if not (mode & op._mask):
        raise ModeConflictError(mode, op)
