"""Pawn formations: matcher patterns declared in playbook files
(docs/ai-playbook-matchers-spec.md, Stage 2).

A pattern constrains files for two sides, `ours` and `theirs`, in ranks
relative to each side (2 = its home rank). Exact match: tolerance lives
in the pattern (rank ranges, free files). The matcher tries the pattern
for each side; several patterns at once, the most constraints wins, then
load order.
"""
from __future__ import annotations

import functools
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import chess

from .matchers import Match, MatchContext
from .playbook import phase_tag
from .playbook_tags import AXIS_FORMATION, MARGIN_EVEN, PHASE_ENDGAME, PHASES, SOURCE_EVAL

NO_PAWN = "-"
ANY_PAWN = "*"
_RANK_RE = re.compile(r"^([2-7])(?:-([2-7]))?$")
_MIN_RANK, _MAX_RANK = 2, 7
_FILES = "".join(chess.FILE_NAMES)
_LAST_INDEX = len(chess.FILE_NAMES) - 1
# Fewer would fire on half the positions of a game.
MIN_CONSTRAINTS = 3
_DEFAULT_PHASES = PHASES - {PHASE_ENDGAME}

SUFFIX_OURS = "_ours"
SUFFIX_THEIRS = "_theirs"
_PATTERN_FIELDS = frozenset({
    "id", "ours", "theirs", "mirror", "phases", "examples", "counterexamples",
})


class PatternError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FileRule:
    file: chess.File
    # Relative ranks a pawn may stand on; empty: no pawn on the file.
    ranks: frozenset[int]


@dataclass(frozen=True, slots=True)
class Pattern:
    id: str
    ours: tuple[FileRule, ...]
    theirs: tuple[FileRule, ...]
    mirror: bool
    phases: frozenset[str]
    examples: tuple[str, ...]
    counterexamples: tuple[str, ...]

    @property
    def constraints(self) -> int:
        return len(self.ours) + len(self.theirs)

    @property
    def tag_ours(self) -> str:
        return self.id + SUFFIX_OURS

    @property
    def tag_theirs(self) -> str:
        return self.id + SUFFIX_THEIRS


def _ranks(value: object, where: str) -> frozenset[int]:
    if value == NO_PAWN:
        return frozenset()
    if value == ANY_PAWN:
        return frozenset(range(_MIN_RANK, _MAX_RANK + 1))
    match = _RANK_RE.match(value) if isinstance(value, str) else None
    if match is None:
        raise PatternError(f"{where}: bad rank {value!r}")
    low = int(match[1])
    high = int(match[2] or low)
    if low > high:
        raise PatternError(f"{where}: inverted range {value!r}")
    return frozenset(range(low, high + 1))


def _rules(raw: object, where: str) -> tuple[FileRule, ...]:
    if not isinstance(raw, dict):
        raise PatternError(f"{where}: file rules are a table")
    rules = []
    for name, value in sorted(raw.items()):
        if name not in _FILES:
            raise PatternError(f"{where}: bad file {name!r}")
        rules.append(FileRule(_FILES.index(name), _ranks(value, f"{where}.{name}")))
    return tuple(rules)


def _fens(raw: object, where: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise PatternError(f"{where}: at least one FEN")
    for fen in raw:
        try:
            valid = isinstance(fen, str) and chess.Board(fen).is_valid()
        except ValueError:
            valid = False
        if not valid:
            raise PatternError(f"{where}: bad FEN {fen!r}")
    return tuple(raw)


def parse_pattern(raw: object) -> Pattern:
    """A validated pattern from one `[[pattern]]` table, self-checked."""
    if not isinstance(raw, dict) or set(raw) - _PATTERN_FIELDS:
        raise PatternError(f"bad pattern fields in {raw!r}")
    pattern_id = raw.get("id")
    if not isinstance(pattern_id, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", pattern_id):
        raise PatternError(f"bad pattern id {pattern_id!r}")
    phases = raw.get("phases", sorted(_DEFAULT_PHASES))
    if not isinstance(phases, list) or not phases or not set(phases) <= PHASES:
        raise PatternError(f"{pattern_id}: bad phases {phases!r}")
    mirror = raw.get("mirror", False)
    if not isinstance(mirror, bool):
        raise PatternError(f"{pattern_id}: `mirror` is a boolean")
    pattern = Pattern(
        id=pattern_id,
        ours=_rules(raw.get("ours", {}), f"{pattern_id}.ours"),
        theirs=_rules(raw.get("theirs", {}), f"{pattern_id}.theirs"),
        mirror=mirror,
        phases=frozenset(phases),
        examples=_fens(raw.get("examples"), f"{pattern_id}.examples"),
        counterexamples=_fens(raw.get("counterexamples"), f"{pattern_id}.counterexamples"),
    )
    if pattern.constraints < MIN_CONSTRAINTS:
        raise PatternError(f"{pattern_id}: at least {MIN_CONSTRAINTS} constraints")
    _self_check(pattern)
    return pattern


def _relative_rank(square: chess.Square, color: chess.Color) -> int:
    rank = chess.square_rank(square)
    return rank + 1 if color == chess.WHITE else _LAST_INDEX + 1 - rank


def _side_holds(
    board: chess.Board, color: chess.Color, rules: Sequence[FileRule], mirrored: bool,
) -> bool:
    pawns = board.pieces(chess.PAWN, color)
    for rule in rules:
        file = _LAST_INDEX - rule.file if mirrored else rule.file
        ranks = {_relative_rank(sq, color) for sq in pawns if chess.square_file(sq) == file}
        if not rule.ranks:
            if ranks:
                return False
        elif not ranks & rule.ranks:
            return False
    return True


def holds(pattern: Pattern, board: chess.Board, side: chess.Color) -> bool:
    """`side` plays `ours`; the board's pawns fit the pattern."""
    mirrors = (False, True) if pattern.mirror else (False,)
    return any(
        _side_holds(board, side, pattern.ours, m) and _side_holds(board, not side, pattern.theirs, m)
        for m in mirrors
    )


def _self_check(pattern: Pattern) -> None:
    """Examples fit for the side to move only; counterexamples for neither."""
    for fen in pattern.examples:
        board = chess.Board(fen)
        if not holds(pattern, board, board.turn) or holds(pattern, board, not board.turn):
            raise PatternError(f"{pattern.id}: example does not fit: {fen}")
    for fen in pattern.counterexamples:
        board = chess.Board(fen)
        if any(holds(pattern, board, side) for side in chess.COLORS):
            raise PatternError(f"{pattern.id}: counterexample fits: {fen}")


@dataclass(frozen=True)
class FormationMatcher:
    """Every loaded pattern; one formation tag per position."""
    patterns: tuple[Pattern, ...]
    name: str = AXIS_FORMATION

    @functools.cached_property
    def tags(self) -> Mapping[str, frozenset[str]]:
        tags = {t for p in self.patterns for t in (p.tag_ours, p.tag_theirs)}
        return {AXIS_FORMATION: frozenset(tags)}

    def match(self, ctx: MatchContext) -> Sequence[Match]:
        best: tuple[int, str] | None = None
        for pattern in self.patterns:
            if ctx.phase not in pattern.phases:
                continue
            ours = holds(pattern, ctx.board, ctx.our_color)
            theirs = holds(pattern, ctx.board, not ctx.our_color)
            if ours == theirs:
                # Neither, or both at once: ambiguous, no tag.
                continue
            # Strictly more constraints wins; ties keep load order.
            if best is None or pattern.constraints > best[0]:
                best = (pattern.constraints, pattern.tag_ours if ours else pattern.tag_theirs)
        return () if best is None else (Match(AXIS_FORMATION, best[1]),)


def example_misses(matcher: FormationMatcher) -> list[str]:
    """Examples the whole matcher (tie-break, phase gating) does not tag
    with their own pattern, from both sides."""
    misses = []
    for pattern in matcher.patterns:
        for fen in pattern.examples:
            board = chess.Board(fen)
            for color, tag in ((board.turn, pattern.tag_ours), (not board.turn, pattern.tag_theirs)):
                ctx = MatchContext(
                    board=board, our_color=color, margin=MARGIN_EVEN,
                    margin_source=SOURCE_EVAL, phase=phase_tag(board),
                )
                if matcher.match(ctx) != (Match(AXIS_FORMATION, tag),):
                    misses.append(f"{pattern.id}: {fen} not tagged {tag}")
    return misses
