"""PGN-annotation injection at AI-turn kickoff.

The agent's initial user message carries the loaded PGN's sanitized
comments (per-ply + root) so the commentator can weigh prior author
notes against tool-verified analysis. This module covers the cap math
(`_truncate`, `_cap_annotations`), the env-var knobs (`_int_env`), and
the mode gate in `_build_user_message` (view-only).
"""
from __future__ import annotations

from sturddle_view.api._ai_kick import (
    _build_user_message,
    _cap_annotations,
    _int_env,
    _PER_COMMENT_MAX_ENV,
    _TOTAL_COMMENT_MAX_ENV,
    _TRUNCATION_MARKER,
    _truncate,
)
from sturddle_view.play.mode import Mode


_STARTPOS_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


# _truncate: per-string length cap with the "..." marker. The marker
# counts against the limit, so head_len = limit - 3 for over-limit input.

def test_truncate_short_string_unchanged():
    assert _truncate("hello", 10) == "hello"


def test_truncate_exact_limit_unchanged():
    assert _truncate("hello", 5) == "hello"


def test_truncate_over_limit_appends_marker_and_respects_total_length():
    out = _truncate("abcdefghij", 7)
    assert out.endswith(_TRUNCATION_MARKER)
    assert len(out) == 7
    # head_len = 7 - 3 = 4 -> "abcd..."
    assert out == "abcd..."


def test_truncate_limit_equal_to_marker_returns_marker_only():
    # head_len = 0; result is just the marker.
    out = _truncate("abcdefg", 3)
    assert out == _TRUNCATION_MARKER


def test_truncate_limit_below_marker_hard_cuts_without_marker():
    # When the cap is smaller than the marker, dropping the marker is
    # the only way to honor the cap. Hard-cut to the literal limit.
    out = _truncate("abcdefg", 1)
    assert out == "a"
    out = _truncate("abcdefg", 2)
    assert out == "ab"


def test_truncate_zero_limit_returns_empty():
    assert _truncate("abc", 0) == ""


def test_truncate_negative_limit_returns_empty():
    assert _truncate("abc", -1) == ""


# _cap_annotations: applies per-comment + total budget; preserves None
# slots; returns (None, None)-ish when nothing survives.

def test_cap_annotations_passthrough_when_under_budget():
    comments = ["short one", None, "short two"]
    out, root = _cap_annotations(comments, "root note", 200, 1500)
    assert out == ["short one", None, "short two"]
    assert root == "root note"


def test_cap_annotations_per_comment_truncates_oversized_entry():
    big = "x" * 500
    out, _ = _cap_annotations([big], None, 50, 10_000)
    assert out is not None
    assert len(out[0]) == 50
    assert out[0].endswith(_TRUNCATION_MARKER)


def test_cap_annotations_total_budget_drops_trailing_entries():
    # Two 100-char comments + 50-char budget: first fits (capped to 50),
    # second has no budget left.
    a = "a" * 100
    b = "b" * 100
    out, _ = _cap_annotations([a, b], None, 100, 50)
    assert out is not None
    # First entry capped to remaining budget (50); second dropped to None.
    assert len(out[0]) == 50
    assert out[1] is None


def test_cap_annotations_root_consumes_budget_before_list():
    # Root takes 40 of 60 budget; list has 20 left.
    out, root = _cap_annotations(
        ["a" * 100, "b" * 100],
        "r" * 40,
        100, 60,
    )
    assert root == "r" * 40
    assert out is not None
    assert len(out[0]) == 20
    assert out[1] is None


def test_cap_annotations_root_independently_capped_by_per_comment_max():
    # per_comment_max=10 caps the root even when total_max is huge.
    _, root = _cap_annotations(None, "x" * 500, 10, 10_000)
    assert root is not None
    assert len(root) == 10
    assert root.endswith(_TRUNCATION_MARKER)


def test_cap_annotations_none_inputs_return_none():
    out, root = _cap_annotations(None, None, 200, 1500)
    assert out is None
    assert root is None


def test_cap_annotations_all_none_list_collapses_to_none():
    out, _ = _cap_annotations([None, None], None, 200, 1500)
    assert out is None


def test_cap_annotations_all_dropped_by_budget_collapses_to_none():
    # Root cap (per_comment_max=100) leaves total_budget=0 after the
    # root consumes it all -- every list entry then becomes None and
    # the list collapses to None.
    out, root = _cap_annotations(["a", "b"], "r" * 200, 100, 100)
    assert root is not None
    assert len(root) == 100
    assert out is None


def test_cap_annotations_preserves_none_slots_inside_list():
    # None slots are part of the parallel-list contract (they map to
    # plies without comments) and must survive capping.
    out, _ = _cap_annotations(["a", None, "c"], None, 200, 1500)
    assert out == ["a", None, "c"]


# _int_env: positive int from env, default on anything else.

def test_int_env_returns_default_when_unset(monkeypatch):
    monkeypatch.delenv("SVTEST_AI_KICK_INT_PROBE", raising=False)
    assert _int_env("SVTEST_AI_KICK_INT_PROBE", 42) == 42


def test_int_env_parses_positive_int(monkeypatch):
    monkeypatch.setenv("SVTEST_AI_KICK_INT_PROBE", "123")
    assert _int_env("SVTEST_AI_KICK_INT_PROBE", 42) == 123


def test_int_env_rejects_zero_returns_default(monkeypatch):
    # Zero is a footgun: capping to 0 means "render nothing" -- the
    # caller almost certainly didn't mean that. Treat as garbage.
    monkeypatch.setenv("SVTEST_AI_KICK_INT_PROBE", "0")
    assert _int_env("SVTEST_AI_KICK_INT_PROBE", 42) == 42


def test_int_env_rejects_negative_returns_default(monkeypatch):
    monkeypatch.setenv("SVTEST_AI_KICK_INT_PROBE", "-5")
    assert _int_env("SVTEST_AI_KICK_INT_PROBE", 42) == 42


def test_int_env_rejects_garbage_returns_default(monkeypatch):
    monkeypatch.setenv("SVTEST_AI_KICK_INT_PROBE", "abc")
    assert _int_env("SVTEST_AI_KICK_INT_PROBE", 42) == 42


def test_int_env_default_overrides_apply_via_caps(monkeypatch):
    # Spot-check the actual env vars wired in -- typos in the names
    # would silently fall back to defaults forever.
    monkeypatch.setenv(_PER_COMMENT_MAX_ENV, "37")
    assert _int_env(_PER_COMMENT_MAX_ENV, 200) == 37
    monkeypatch.setenv(_TOTAL_COMMENT_MAX_ENV, "999")
    assert _int_env(_TOTAL_COMMENT_MAX_ENV, 1500) == 999


# _build_user_message: mode-gated annotation flow. The fake HVE captures
# whether view_game_comments was even consulted in each mode.

class _FakeBoard:
    def __init__(self, fen: str):
        self._fen = fen
        self.move_stack: list = []

    def fen(self) -> str:
        return self._fen


class _FakeHVE:
    def __init__(
        self,
        *,
        mode: Mode,
        view_comments=None,
        view_root: str | None = None,
    ):
        self._mode = mode
        self._view_comments = view_comments
        self._view_root = view_root
        self.view_game_comments_calls = 0
        self._board = _FakeBoard(_STARTPOS_FEN)

    def current_board(self):
        return self._board

    def start_fen(self) -> str:
        return _STARTPOS_FEN

    def view_full_moves_san(self) -> list[str]:
        return ["e4", "e5", "Nf3"]

    def lookup_opening(self):
        return None

    def viewed_pgn_result(self):
        return None

    def engine_display_name(self):
        return None

    def pre_analysis_mode(self) -> Mode:
        return self._mode

    def view_game_comments(self):
        self.view_game_comments_calls += 1
        return self._view_comments, self._view_root


def test_build_user_message_viewing_mode_injects_annotations():
    hve = _FakeHVE(
        mode=Mode.VIEWING,
        view_comments=["sharp", None, None],
        view_root="famous miniature",
    )
    msg = _build_user_message(hve)
    assert msg is not None
    assert "Pre-game note: famous miniature" in msg
    assert "Annotations: 1.e4 {sharp}" in msg
    assert hve.view_game_comments_calls == 1


def test_build_user_message_playing_mode_skips_view_comments_accessor():
    # Play-mode comments are mostly machine [%clk]/[%eval] noise that
    # sanitization strips, so we never even call the accessor. Test
    # both the no-call and the no-output sides.
    hve = _FakeHVE(
        mode=Mode.PLAY,
        view_comments=["should not appear"],
        view_root="should not appear either",
    )
    msg = _build_user_message(hve)
    assert msg is not None
    assert "Annotations:" not in msg
    assert "Pre-game note:" not in msg
    assert hve.view_game_comments_calls == 0


def test_build_user_message_viewing_mode_with_no_comments_omits_lines():
    hve = _FakeHVE(mode=Mode.VIEWING, view_comments=None, view_root=None)
    msg = _build_user_message(hve)
    assert msg is not None
    assert "Annotations:" not in msg
    assert "Pre-game note:" not in msg


def test_build_user_message_viewing_mode_respects_env_caps(monkeypatch):
    # Set the per-comment cap below the comment length to prove the
    # env-driven cap actually wires through to the rendered message.
    monkeypatch.setenv(_PER_COMMENT_MAX_ENV, "10")
    monkeypatch.setenv(_TOTAL_COMMENT_MAX_ENV, "100")
    hve = _FakeHVE(
        mode=Mode.VIEWING,
        view_comments=["x" * 200],
        view_root=None,
    )
    msg = _build_user_message(hve)
    assert msg is not None
    # Annotation should appear but truncated -- the "x" run must be
    # <= 10 chars (including the marker) inside the braces.
    assert "Annotations: 1.e4 {" in msg
    # The full 200-char run would be rendered verbatim without the cap;
    # 50 x's still being too many proves the cap fired.
    assert ("x" * 50) not in msg


# Default-cap sanity: the in-module defaults must be positive ints so
# `_int_env` (with `v > 0` guard) accepts them.

def test_default_caps_are_positive_ints():
    from sturddle_view.api._ai_kick import (
        _PER_COMMENT_MAX_DEFAULT,
        _TOTAL_COMMENT_MAX_DEFAULT,
    )
    assert isinstance(_PER_COMMENT_MAX_DEFAULT, int)
    assert _PER_COMMENT_MAX_DEFAULT > 0
    assert isinstance(_TOTAL_COMMENT_MAX_DEFAULT, int)
    assert _TOTAL_COMMENT_MAX_DEFAULT > 0
