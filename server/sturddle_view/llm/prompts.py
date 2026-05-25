"""System prompt + mode addenda for the AI analysis agent.

Three constants and one pure assembly function. The assembly output must
be byte-stable: prompt caching (Anthropic native) keys on the exact
bytes of the system block, so silent drift (env reads, timestamps,
dict-order joins) would quietly destroy the cache hit rate.

Tests pin the assembled output against a frozen literal. Intentional
prompt edits will fail those tests, forcing the author to update the
golden value -- and notice they just invalidated the cache key.

The `commentator` addendum is defined alongside `coach` so the prompt
shape and transport are uniform across paths, even though only `coach`
has callers wired today.
"""
from __future__ import annotations

from typing import Literal


PromptMode = Literal["coach", "commentator"]


SYSTEM_PROMPT = """\
You are a chess analysis assistant. You collaborate with a chess engine \
that produces numeric evaluations, principal variations, and search \
depth. The engine is the source of truth for any numeric claim; your \
job is to turn its output into clear prose for a human reader.

Tools:
- `analyze(fen, time_ms?, depth?)`: run an engine search on a specific \
position. Use this for the current position or a hypothetical line. \
The user message gives you the live FEN; pass it as-is unless you are \
exploring a what-if.
- `top_moves(n?, time_ms?, depth?)`: rank the top N candidate moves in \
the live position. Use this whenever you want to compare alternatives; \
do not chain several `analyze` calls to fake MultiPV.

Ground rules:
- Engine evaluations are ALWAYS from White's point of view: positive \
cp = White is better, negative cp = Black is better, regardless of \
whose turn it is. The user message tells you the side to move; trust \
that field. Never re-derive side-to-move from the FEN.
- Tool results carry both `score_cp` (centipawns, integer; 100 cp = 1 \
pawn) and `score_text` (presentation string like '+0.02' or '+M3'). \
Use `score_text` for prose; never present `score_cp` as if it were \
pawns.
- If you have not seen an engine evaluation for the position you are \
discussing, call `analyze` (or `top_moves`) before claiming anything \
about it. Do not guess.
- You have a bounded tool-call budget per turn. Prefer one well-aimed \
call over several speculative ones.
- Be concise. A few sentences of grounded prose beats a paragraph of \
hedging.
- Never invent moves, lines, or evaluations. If the engine output does \
not support a claim, say so.
"""


COACH_ADDENDUM = """\
You are coaching a human player during a live game against an engine. \
Address the player in the second person.

Focus on the position in front of the player right now: what their \
last move accomplished or missed, what threats and ideas are on the \
board, and what to look for on the next move. Do not reveal the \
opponent engine's planned continuation -- coach the player on what \
they can see and decide for themselves.
"""


COMMENTATOR_ADDENDUM = """\
You are annotating a chess game for a reader who is reviewing it after \
the fact. Write in the third person, in the style of a chess magazine \
annotator.

Identify critical moments -- blunders, missed wins, key strategic \
decisions -- and explain them with reference to the engine \
evaluations. The reader can see the whole game, so feel free to \
reference what happens later when it illuminates an earlier moment.
"""


_ADDENDA: dict[PromptMode, str] = {
    "coach": COACH_ADDENDUM,
    "commentator": COMMENTATOR_ADDENDUM,
}

_SEPARATOR = "\n\n"


def assemble_system_prompt(mode: PromptMode) -> str:
    """Return the full system prompt for `mode`.

    Output is byte-stable: no env reads, no timestamps, no dict-iteration
    order. The two inputs are the module constants above; if they don't
    change, the output doesn't change.
    """
    try:
        addendum = _ADDENDA[mode]
    except KeyError:
        raise ValueError(f"unknown prompt mode: {mode!r}") from None
    return SYSTEM_PROMPT.rstrip("\n") + _SEPARATOR + addendum


_INITIAL_NO_MOVES = "(none yet -- the game has not started)"


def _render_san_pairs(san_history: list[str]) -> str:
    """Render a flat SAN list as a numbered move sequence.

    "1. e4 e5 2. Nf3 Nc6 3. Bb5". Black-only opening (history starts at
    Black's move) is not expressible in plain SAN-pair notation; that
    case only arises from imports with a custom start FEN, where the
    FEN tells the model whose turn it is regardless.
    """
    if not san_history:
        return _INITIAL_NO_MOVES
    parts: list[str] = []
    for i, san in enumerate(san_history):
        if i % 2 == 0:
            parts.append(f"{(i // 2) + 1}. {san}")
        else:
            parts.append(san)
    return " ".join(parts)


def _side_to_move_from_fen(fen: str) -> str:
    """Parse 'w' or 'b' from the FEN's second field. Falls back to
    'white' when the FEN is malformed -- the agent will still get a
    reasonable default, and analyze tools will surface the real error."""
    parts = fen.split()
    if len(parts) >= 2 and parts[1] == "b":
        return "black"
    return "white"


def build_initial_user_message(*, fen: str, san_history: list[str]) -> str:
    """Build the user message that opens an agent turn. Carries the FEN,
    the explicit side-to-move (so the model does not re-derive it), and
    the played SAN history.

    `san_history` semantics depend on the caller:
    - Play mode: moves played up to the current position.
    - View mode: the FULL game's moves; FEN locates where commentary
      is requested.

    Byte-stable for the same inputs (prompt caching keys on these bytes)."""
    return (
        f"Current position (FEN): {fen}\n"
        f"Side to move: {_side_to_move_from_fen(fen)}\n"
        f"Game moves: {_render_san_pairs(san_history)}\n"
    )
