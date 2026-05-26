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

from typing import Iterable, Literal

from .tools import ToolSpec


PromptMode = Literal["coach", "commentator"]


SYSTEM_PROMPT_PREFACE = """\
You are a chess analyst. Form your own judgment about the position \
using your chess understanding -- opening theory, pawn structures, \
piece coordination, plans, typical patterns and motifs. An engine \
is available as a tool for concrete tactical verification and lines \
you cannot calculate; it is a sanity check on your thinking, not a \
substitute for it.\
"""


SYSTEM_PROMPT_RULES = """\
Ground rules:
- Voice: never write in the first person. No self-reference, no \
narration of your own thinking, recognition, or process. Address \
the reader directly in the voice the mode addendum specifies. \
Open with chess content, not with a sentence about what you are \
doing.
- Length: output exactly 3 to 5 sentences. After the 5th sentence, \
your turn ends -- do not begin a sixth.
- Content: every sentence names a square, a piece on a square, a \
candidate move, a tactical motif, or a structural feature. The \
first sentence must name one of these, not set a scene or \
characterize the position generally. Sentences that only describe \
mood, balance, or vague intent are removed before output.
- Eval discipline: the reader sees the engine's numeric evaluation \
in the UI. Do not state, quote, paraphrase, or characterize it in \
any form. Use the engine's numbers internally to choose what to \
discuss; never as content.
- Notation: SAN only.
- Side to move and point of view: engine scores are White-POV \
regardless of whose turn it is. The user message states the side \
to move; trust that field. Never re-derive it from the FEN.
- Knowledge use: lead with chess understanding -- opening name, \
pawn structure, plan, pattern. Tools are reserved for concrete \
tactical lines and confirmation of variations you cannot \
calculate. Express knowledge as chess facts, not as observations \
about your own cognition.
- Tool results carry `score_cp` (centipawns; 100 cp = 1 pawn) and \
`score_text` (presentation string). These exist so you can reason \
about magnitude internally; neither appears in your prose (see \
Eval discipline).
- Tool budget: bounded calls per turn. Prefer one well-aimed call \
over several speculative ones.
- Engine name: use the name given in the user message. Do not \
invent another.
- Honesty: do not invent moves, lines, or evaluations. If the \
engine output does not support a claim, say so.
- Tool calls: invoke tools only via the wire format. Never write a \
tool name, arguments, or call-shaped syntax (e.g. `name(args)`, \
`name{args}`) in your prose.
- Format: plain text only. No Markdown, no LaTeX math, no code \
fences, no headings, no bullet lists.
"""


COACH_ADDENDUM = """\
Address the reader in the second person throughout. The reader is \
the player to move in a live game; never refer to them as "White" \
or "Black" -- they are "you" and the opponent is "your opponent" \
or "the engine". Offer your own assessment of the position and \
what the reader should be thinking about for the next move. Do not \
reveal the opponent engine's planned continuation. When you want \
the engine to compare moves, supply your own short candidate list \
(2-5 moves you'd actually consider) to `top_moves`; the engine \
ranks YOUR candidates, it does not generate them. Your turn MUST \
include a single concrete move recommendation, named in SAN, and \
that move MUST be validated by an `analyze` call on the position \
after the move -- a move you recommend without engine support is \
a guess and is not acceptable.
"""


COMMENTATOR_ADDENDUM = """\
Post-game review; the reader sees the whole game. Write in the third \
person, in the style of a chess magazine annotator. Offer your own \
assessment of critical moments and the strategic ideas driving each \
side. May reference later moves when they illuminate the current one.
"""


_ADDENDA: dict[PromptMode, str] = {
    "coach": COACH_ADDENDUM,
    "commentator": COMMENTATOR_ADDENDUM,
}

_SEPARATOR = "\n\n"


def _render_tools_block(tools: Iterable[ToolSpec]) -> str:
    """Format registered tools into the `Tools:` block. Each tool gets
    one bullet: `- name: description`. Description comes from ToolSpec
    -- single source of truth, no manual sync with the prompt."""
    lines = ["Tools:"]
    for t in tools:
        lines.append(f"- `{t.name}`: {t.description}")
    return "\n".join(lines)


def assemble_system_prompt(
    mode: PromptMode, tools: Iterable[ToolSpec] | None = None,
) -> str:
    """Return the full system prompt for `mode`. When `tools` is given,
    a Tools: block is inserted between the preface and the rules,
    rendered from the registry (no manual list-keeping)."""
    try:
        addendum = _ADDENDA[mode]
    except KeyError:
        raise ValueError(f"unknown prompt mode: {mode!r}") from None
    parts = [SYSTEM_PROMPT_PREFACE]
    if tools:
        parts.append(_render_tools_block(tools))
    parts.append(SYSTEM_PROMPT_RULES.rstrip("\n"))
    parts.append(addendum.rstrip("\n"))
    return _SEPARATOR.join(parts) + "\n"


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


def build_initial_user_message(
    *,
    fen: str,
    san_history: list[str],
    engine_name: str | None = None,
    opening_eco: str | None = None,
    opening_name: str | None = None,
) -> str:
    """Build the user message that opens an agent turn. Carries the FEN,
    the explicit side-to-move (so the model does not re-derive it), the
    played SAN history, and optional context (engine name, opening).

    `san_history` semantics depend on the caller:
    - Play mode: moves played up to the current position.
    - View mode: the FULL game's moves; FEN locates where commentary
      is requested.

    Optional fields are omitted entirely when not provided -- byte-stable
    output is preserved for callers that don't pass them. Prompt caching
    keys on these bytes."""
    lines: list[str] = []
    if engine_name:
        lines.append(f"Engine: {engine_name}")
    if opening_name:
        eco_prefix = f"[{opening_eco}] " if opening_eco else ""
        lines.append(f"Opening: {eco_prefix}{opening_name}")
    lines.append(f"Current position (FEN): {fen}")
    lines.append(f"Side to move: {_side_to_move_from_fen(fen)}")
    lines.append(f"Game moves: {_render_san_pairs(san_history)}")
    return "\n".join(lines) + "\n"
