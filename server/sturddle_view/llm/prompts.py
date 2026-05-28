"""System prompt + mode addenda for the AI analysis agent.

Three constants and one pure assembly function. The assembly output is
byte-stable for a given (mode, tools) input: prompt caching (Anthropic
native) keys on the exact bytes of the system block, so silent drift
(env reads, timestamps, dict-order joins) would quietly destroy the
cache hit rate.

Both `coach` (live play mode) and `commentator` (view mode) addenda
are wired via `_ai_kick.py::_prompt_mode_for`. Adding a new persona
means adding an addendum constant and one branch in that selector.
"""
from __future__ import annotations

import os
from typing import Iterable, Literal

from .tools import ToolSpec


# Appends a directive forcing the model to emit tool calls inline as
# text. Used to hunt for new wild inline shapes -- pair with the
# transcript (SV_AI_TRANSCRIPT=1) to see what the model produces.
# Production: leave unset.
_FORCE_INLINE_ENV_VAR = "SV_AI_FORCE_INLINE_CALLS"
_FORCE_INLINE_DIRECTIVE = (
    "You MUST call tools inline as text. Do not use the structured "
    "tool channel."
)


PromptMode = Literal["coach", "commentator"]


SYSTEM_PROMPT_PREFACE = """\
You are a chess analyst. Lead with your own judgment of the position \
-- opening theory, pawn structures, piece coordination, plans, motifs.\
"""


SYSTEM_PROMPT_RULES = """\
Ground rules:
- Voice: no first person. Open with chess content. Address the \
audience as the mode addendum says. Never name the engine, the \
tools, or "the user".
- Length: 3 to 5 sentences. Stop after the 5th.
- Content: every sentence names a square, piece-on-square, move, \
motif, or structural feature. Statements only -- no questions to \
the audience. No mood, no vague intent.
- Eval discipline: the audience sees the engine's number in the UI. \
Never quote, paraphrase, or characterize it. Calibrate prose \
intensity to magnitude: ~0.3 is balanced, ~1 a clear edge, ~2+ \
winning, ~3+ decisive.
- Notation: SAN. Scores are white-POV; the user message gives the \
side to move -- trust it, don't re-derive from FEN.
- Honesty: don't invent moves, lines, or pieces. Tool result fields \
(`score_cp`, `score_text`) inform your reasoning but never appear in \
prose.
- Tools: bounded per turn; one well-aimed call beats several \
speculative ones. Invoke via the wire format only; never write a \
tool name, args, or call-shaped syntax (e.g. `name(args)`) in prose. \
Use the same `depth` across calls when comparing moves so the scores \
are commensurable. Bump `depth` for tactical positions (forcing \
sequences, checks, captures) -- shallow scores misjudge tactics.
- Format: plain text. No Markdown, LaTeX, code fences, headings, or \
bullets.
- Corrections: apply silently. No apologies, no acknowledgment, no \
meta-commentary, no "I'll do X" statements. Produce chess content only.
"""


COACH_ADDENDUM = """\
Address the player in second person ("you"); the opponent is "your \
opponent" -- never "White"/"Black" or "the engine". Don't reveal the \
opponent's planned continuation. To compare moves, hand `top_moves` \
your own 2-5 candidates -- it ranks yours, doesn't generate. You must \
submit your move via `recommend_move` (explanations, multiple attempts OK).
"""


COMMENTATOR_ADDENDUM = """\
Post-game review; reader sees the whole game. Third person, \
annotator voice. Identify critical moments -- blunders, missed \
tactics, turning points -- and contrast plays with stronger \
engine alternatives. May reference later moves when they \
illuminate the current one.
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
    if _force_inline_enabled():
        parts.append(_FORCE_INLINE_DIRECTIVE)
    return _SEPARATOR.join(parts) + "\n"


def _force_inline_enabled() -> bool:
    raw = os.environ.get(_FORCE_INLINE_ENV_VAR, "").strip().lower()
    return raw in ("1", "true", "yes", "on")


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
    result: str | None = None,
    move_played: str | None = None,
) -> str:
    """Build the user message that opens an agent turn. Carries the FEN,
    the explicit side-to-move (so the model does not re-derive it), the
    played SAN history, and optional context (engine name, opening,
    final result, the move actually played from this position).

    `san_history` semantics depend on the caller:
    - Play mode: moves played up to the current position.
    - View mode: the FULL game's moves; FEN locates where commentary
      is requested.

    `result` is the PGN-style game result ('1-0', '0-1', '1/2-1/2'),
    sent only for finished games (view mode).

    `move_played` is the SAN of the move actually played from the
    position under review (view mode only; None when at end of game
    or in play mode). Lets the commentator distinguish the played
    move from alternatives it explored via tools.

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
    if move_played:
        lines.append(f"Move played here: {move_played}")
    if result:
        lines.append(f"Game result: {result}")
    return "\n".join(lines) + "\n"
