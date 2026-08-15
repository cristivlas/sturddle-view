"""Shared pin data: the forced-termination names client and server must
agree on, plus the parser that lifts the client's set out of play.js.

Not a test module (no ``test_`` prefix); imported by
test_view_analyze_forced_set.py (membership pin, default suite) and
test_e2e_view_analyze_gating.py (payload pins, e2e suite).

A NEW outcome added to python-chess's Board.outcome() would be missed by
both sides until FORCED_PY is updated by hand; the membership test then
drags the client along.
"""
from __future__ import annotations

import re
from pathlib import Path

import chess

# What a STANDARD-chess Board.outcome() (no claimable draws) can produce
# -- exactly the server's start_analysis rejections; variant_* members
# are unreachable. Enum-derived names, so a rename fails here by name.
FORCED_PY = frozenset(t.name.lower() for t in (
    chess.Termination.CHECKMATE,
    chess.Termination.STALEMATE,
    chess.Termination.INSUFFICIENT_MATERIAL,
    chess.Termination.SEVENTYFIVE_MOVES,
    chess.Termination.FIVEFOLD_REPETITION,
))

_PLAY_JS = Path(__file__).resolve().parents[2] / "web" / "app" / "perspectives" / "play.js"
_SET_RE = re.compile(r"FORCED_TERMINATIONS = new Set\(\[(.*?)\]\)", re.S)
_JS_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)


def client_forced_set() -> frozenset[str]:
    """Double-quoted literals of play.js's FORCED_TERMINATIONS, comments
    stripped (a commented-out entry counts as dropped). Pins the literal
    form: rename/computed Set fail "not found"; single quotes parse empty."""
    # Whole-source strip first: a comment containing "])" would truncate
    # the non-greedy match. Naive lexing can only fail loudly ("not
    # found") -- entries contain no "//" or "/*" to strip silently.
    source = _JS_COMMENT_RE.sub("", _PLAY_JS.read_text(encoding="utf-8"))
    m = _SET_RE.search(source)
    if m is None:
        raise AssertionError("FORCED_TERMINATIONS set literal not found in play.js")
    return frozenset(re.findall(r'"([^"]+)"', m.group(1)))
