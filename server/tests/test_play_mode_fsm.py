"""Tests for Mode enum, Op enum, ALLOWED_MODES_BY_OP matrix, and ModeConflictError (R5 / P8)."""
from __future__ import annotations

import pytest

from sturddle_view.play.mode import (
    ALLOWED_MODES_BY_OP,
    Mode,
    ModeConflictError,
    Op,
    assert_allowed,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_allowed(mode: Mode, op: Op) -> bool:
    return mode in ALLOWED_MODES_BY_OP.get(op, frozenset())


# ---------------------------------------------------------------------------
# Basic enum checks
# ---------------------------------------------------------------------------

def test_initial_mode_is_play():
    assert Mode.PLAY is not None


def test_mode_enum_has_five_members():
    assert set(Mode) == {Mode.PLAY, Mode.PAUSED, Mode.VIEWING, Mode.EDITING, Mode.ANALYZING}


def test_op_enum_covers_guarded_operations():
    expected = {
        Op.SUBMIT_MOVE, Op.TAKEBACK, Op.SWITCH_SIDES, Op.RESIGN,
        Op.PAUSE, Op.RESUME, Op.START_ANALYSIS, Op.ENTER_VIEW_MODE,
        Op.ENTER_EDIT_MODE, Op.VIEW_GOTO, Op.PLAY_FROM_HERE,
        Op.COMMIT_EDIT, Op.CANCEL_EDIT, Op.NEW_GAME,
    }
    assert expected <= set(Op)


# ---------------------------------------------------------------------------
# Matrix spot-checks (sampled from the guard survey)
# ---------------------------------------------------------------------------

def test_submit_move_allowed_only_in_play():
    assert _is_allowed(Mode.PLAY, Op.SUBMIT_MOVE)
    for mode in (Mode.PAUSED, Mode.VIEWING, Mode.EDITING, Mode.ANALYZING):
        assert not _is_allowed(mode, Op.SUBMIT_MOVE)


def test_takeback_allowed_in_play_and_paused():
    assert _is_allowed(Mode.PLAY, Op.TAKEBACK)
    assert _is_allowed(Mode.PAUSED, Op.TAKEBACK)
    for mode in (Mode.VIEWING, Mode.EDITING, Mode.ANALYZING):
        assert not _is_allowed(mode, Op.TAKEBACK)


def test_switch_sides_allowed_only_in_play():
    assert _is_allowed(Mode.PLAY, Op.SWITCH_SIDES)
    for mode in (Mode.PAUSED, Mode.VIEWING, Mode.EDITING, Mode.ANALYZING):
        assert not _is_allowed(mode, Op.SWITCH_SIDES)


def test_resign_allowed_in_play_and_paused():
    assert _is_allowed(Mode.PLAY, Op.RESIGN)
    assert _is_allowed(Mode.PAUSED, Op.RESIGN)
    for mode in (Mode.VIEWING, Mode.EDITING, Mode.ANALYZING):
        assert not _is_allowed(mode, Op.RESIGN)


def test_pause_allowed_only_in_play():
    assert _is_allowed(Mode.PLAY, Op.PAUSE)
    for mode in (Mode.PAUSED, Mode.VIEWING, Mode.EDITING, Mode.ANALYZING):
        assert not _is_allowed(mode, Op.PAUSE)


def test_resume_allowed_only_in_paused():
    assert _is_allowed(Mode.PAUSED, Op.RESUME)
    for mode in (Mode.PLAY, Mode.VIEWING, Mode.EDITING, Mode.ANALYZING):
        assert not _is_allowed(mode, Op.RESUME)


def test_start_analysis_allowed_in_paused_and_viewing():
    assert _is_allowed(Mode.PAUSED, Op.START_ANALYSIS)
    assert _is_allowed(Mode.VIEWING, Op.START_ANALYSIS)
    for mode in (Mode.PLAY, Mode.EDITING, Mode.ANALYZING):
        assert not _is_allowed(mode, Op.START_ANALYSIS)


def test_enter_view_mode_blocked_only_in_editing():
    for mode in (Mode.PLAY, Mode.PAUSED, Mode.VIEWING, Mode.ANALYZING):
        assert _is_allowed(mode, Op.ENTER_VIEW_MODE)
    assert not _is_allowed(Mode.EDITING, Op.ENTER_VIEW_MODE)


def test_enter_edit_mode_allowed_in_viewing_and_analyzing():
    assert _is_allowed(Mode.VIEWING, Op.ENTER_EDIT_MODE)
    assert _is_allowed(Mode.ANALYZING, Op.ENTER_EDIT_MODE)
    for mode in (Mode.PLAY, Mode.PAUSED, Mode.EDITING):
        assert not _is_allowed(mode, Op.ENTER_EDIT_MODE)


def test_view_goto_allowed_only_in_viewing():
    assert _is_allowed(Mode.VIEWING, Op.VIEW_GOTO)
    for mode in (Mode.PLAY, Mode.PAUSED, Mode.EDITING, Mode.ANALYZING):
        assert not _is_allowed(mode, Op.VIEW_GOTO)


def test_play_from_here_allowed_only_in_viewing():
    assert _is_allowed(Mode.VIEWING, Op.PLAY_FROM_HERE)
    for mode in (Mode.PLAY, Mode.PAUSED, Mode.EDITING, Mode.ANALYZING):
        assert not _is_allowed(mode, Op.PLAY_FROM_HERE)


def test_commit_edit_allowed_only_in_editing():
    assert _is_allowed(Mode.EDITING, Op.COMMIT_EDIT)
    for mode in (Mode.PLAY, Mode.PAUSED, Mode.VIEWING, Mode.ANALYZING):
        assert not _is_allowed(mode, Op.COMMIT_EDIT)


def test_cancel_edit_allowed_only_in_editing():
    assert _is_allowed(Mode.EDITING, Op.CANCEL_EDIT)
    for mode in (Mode.PLAY, Mode.PAUSED, Mode.VIEWING, Mode.ANALYZING):
        assert not _is_allowed(mode, Op.CANCEL_EDIT)


def test_new_game_blocked_in_editing():
    assert not _is_allowed(Mode.EDITING, Op.NEW_GAME)
    for mode in (Mode.PLAY, Mode.PAUSED, Mode.VIEWING, Mode.ANALYZING):
        assert _is_allowed(mode, Op.NEW_GAME)


# ---------------------------------------------------------------------------
# Parametrized matrix -- Cartesian (Mode x Op)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", list(Mode))
@pytest.mark.parametrize("op", list(Op))
def test_mode_matrix_parametrized(mode: Mode, op: Op):
    allowed = _is_allowed(mode, op)
    if allowed:
        assert_allowed(mode, op)  # must not raise
    else:
        with pytest.raises(ModeConflictError) as exc_info:
            assert_allowed(mode, op)
        err = exc_info.value
        assert err.current is mode
        assert err.attempted is op


# ---------------------------------------------------------------------------
# ModeConflictError fields
# ---------------------------------------------------------------------------

def test_invalid_transitions_raise_typed_error():
    with pytest.raises(ModeConflictError) as exc_info:
        assert_allowed(Mode.EDITING, Op.SUBMIT_MOVE)
    err = exc_info.value
    assert err.current is Mode.EDITING
    assert err.attempted is Op.SUBMIT_MOVE
    assert isinstance(err, RuntimeError)  # subclass of RuntimeError


def test_mode_conflict_error_is_runtime_error():
    err = ModeConflictError(Mode.VIEWING, Op.TAKEBACK)
    assert isinstance(err, RuntimeError)
    assert err.current is Mode.VIEWING
    assert err.attempted is Op.TAKEBACK
