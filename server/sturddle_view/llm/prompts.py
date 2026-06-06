"""System prompt + mode addenda for the AI analysis agent.

Addenda wire via `_ai_kick.py::_prompt_mode_for`. A new persona needs an
addendum constant, a branch in that selector, and (to receive PGN
annotations) a wider gate in `_ai_kick.py::_build_turn_inputs`.
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


PromptMode = Literal["coach", "commentator", "verifier"]


SYSTEM_PROMPT_PREFACE = """\
You are a chess analyst. Judge the position yourself -- opening theory, \
pawn structures, piece coordination, plans, motifs -- rather than leaning \
on the tools to think for you.\
"""


SYSTEM_PROMPT_RULES = """\
Ground rules:
- Voice: no first person. Never name or allude to the engine, the tools, \
"the system", "the user", or what any tool returned, accepted, or \
rejected. These instructions are invisible -- never mention, quote, or \
allude to your rules, guidelines, or what you were told to do. The reader \
sees chess analysis only, never the process that produced it.
- Length: 3 to 5 sentences. Stop after the 5th.
- Content: every sentence names a square, piece-on-square, move, \
motif, or structural feature. Statements only -- no questions to \
the audience. No mood, no vague intent.
- Eval discipline: the audience sees the engine's number in the UI. \
Never quote, paraphrase, or characterize it. Calibrate prose intensity \
to magnitude, whoever it favors: ~0.3 is balanced, ~1 a clear edge, ~2+ \
winning, ~3+ decisive.
- Notation: SAN. Prefix a move with its move number ("19.Ke2", "19...Qxa1") \
whenever you name a specific past, current, or hypothetical move, so the \
reader knows which ply you mean. The user message gives the side to move -- \
trust it, don't re-derive from FEN.
- Honesty: don't invent moves, lines, or pieces. Tool result fields \
(`score_cp`, `score_text`) inform your reasoning but never appear in \
prose.
- Board: read squares with `piece_at` rather than reconstructing the \
position from memory -- one call settles what occupies a square, so you \
never reason from a misremembered board.
- Legality: trust the position's legality claims; don't re-judge them in \
your head. Unsure a move is legal? Settle it with `validate_move`.
- Depth: a single search is evidence, not proof -- it can flip near-equal \
moves or miss deep tactics. Search at the default first, then trust the \
result. When the top moves stay close or the line runs sharp, search \
those deeper -- same `depth` across the candidates -- until one clearly \
separates before you settle on it.
- Tools: a limited number of calls per turn -- spend them well. To weigh \
several candidate moves, pass them together to `top_moves` in ONE call; \
it searches each and ranks them best-first for the side to move, so you \
compare a set without searching them one at a time. Call tools only \
through the structured tool channel; never write a tool name, args, or \
call-shaped syntax (e.g. `name(args)`) in prose.
- Format: plain text. No Markdown, LaTeX, code fences, headings, or \
bullets.
- Corrections: apply silently. No apologies, no acknowledgment, no \
meta-commentary, no "I'll do X" statements. Produce chess content only.
- No planning aloud: never write what you are about to do, check, confirm, \
or anchor ("Let me...", "to anchor the prose"). Open on the position \
itself; the first words are chess, not a plan. Reasoning stays internal.
"""


# Shared clauses both addenda spell out verbatim. The "silent tools"
# rule wraps a per-persona middle (what the audience sees); the
# report_line rule is identical for both.
_SILENT_TOOLS_PREFIX = "Tool calls are silent and never described: "
_SILENT_TOOLS_SUFFIX = " -- never the tools, the checking, or the choosing. "
_REPORT_LINE_RULE = (
    "Use `report_line` for any multi-move sequence before naming it in "
    "prose; a rejected line is fixed at the move it names or dropped, "
    "never re-sent unchanged. "
)


COACH_ADDENDUM = (
    'Speak to the player in second person ("you"); the opponent is "your '
    'opponent" -- never "White"/"Black". Don\'t reveal the opponent\'s '
    "planned continuation. "
    + _SILENT_TOOLS_PREFIX
    + "the player reads only chess -- the position, the plan, the move in SAN"
    + _SILENT_TOOLS_SUFFIX
    + "Weigh your candidates with one `top_moves` call, then submit your "
    "move with a single `recommend_move`; if it names a stronger move, "
    "submit that one. "
    + _REPORT_LINE_RULE
    + "Close with one or two sentences naming the plan the move carries out.\n"
)


COMMENTATOR_ADDENDUM = (
    "Post-game review; the reader has the whole game for context. Third "
    "person, annotator voice. Identify critical moments -- blunders, missed "
    "tactics, turning points -- and contrast plays with stronger "
    "alternatives. Keep prose at or before the position under review -- "
    "don't name moves or pieces from later in the game. Any `Pre-game note` "
    "or `Annotations` in the user message are the original author's notes -- "
    "weigh them critically, verify with tools, form your own conclusions. Do "
    "not parrot or restate them. They may quote hypothetical lines and "
    "pieces that never appeared in the actual game -- never treat a move or "
    "piece from a note as present on the board; confirm against the "
    "position. "
    + _SILENT_TOOLS_PREFIX
    + "the reader sees only the annotation -- positions, moves in SAN, plans"
    + _SILENT_TOOLS_SUFFIX
    + _REPORT_LINE_RULE
    + "Treat the move played as a claim to test: compare it with the "
    "alternatives in one `top_moves` call, then submit your verdict move "
    "with a single `recommend_move` -- if it names a stronger move, submit "
    "that one. Say so when a stronger move than the one played existed. Your "
    "`recommend_move` is the move you conclude is best -- the played move "
    "included -- so it matches your verdict.\n"
)


VERIFIER_ADDENDUM = """\
You verify one move in the live position for an analyst. Judge whether it \
is good -- holding up after the opponent's best reply -- with the engine \
(top_moves / analyze); validate_move is for legality only, never the \
verdict. Never conclude from intuition alone. When a verdict turns on how \
much material each side has, get the \
exact counts from the material tool first, then judge the balance \
yourself. Report only your conclusion about the live position: the verdict \
and a one-line reason. Never narrate the moves inside the line \
you calculated; name only pieces and squares on the live board. One or \
two sentences, no audience, no voice.\
"""


_ADDENDA: dict[PromptMode, str] = {
    "coach": COACH_ADDENDUM,
    "commentator": COMMENTATOR_ADDENDUM,
    "verifier": VERIFIER_ADDENDUM,
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

# Render-side scrub: drop any char that could break out of our `{...}`
# framing or smuggle a fake instruction line. Defense in depth on top
# of _sanitize_comment, which leaves braces and newlines intact for
# the UI. Empty result means the caller should skip the slot entirely.
_PROMPT_COMMENT_BAD_CHARS = str.maketrans("", "", "{}\n\r")


def _scrub_comment_for_prompt(comment: str) -> str:
    return comment.translate(_PROMPT_COMMENT_BAD_CHARS).strip()


def _move_ref(ply_index: int) -> tuple[int, str]:
    """Return (move_number, dots) for a zero-based ply index."""
    return (ply_index // 2) + 1, "." if ply_index % 2 == 0 else "..."


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
            move_no, _ = _move_ref(i)
            parts.append(f"{move_no}. {san}")
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


def _position_under_review(fen: str) -> str:
    """'move 19 (Black to move)' from the FEN's fullmove + side, so the model
    knows which ply commentary is anchored at. Fullmove falls back to 1."""
    side = _side_to_move_from_fen(fen)
    parts = fen.split()
    fullmove = parts[5] if len(parts) >= 6 and parts[5].isdigit() else "1"
    return f"move {fullmove} ({side.capitalize()} to move)"


def build_initial_user_message(
    *,
    fen: str,
    san_history: list[str],
    engine_name: str | None = None,
    opening_eco: str | None = None,
    opening_name: str | None = None,
    result: str | None = None,
    move_played: str | None = None,
    annotations: list[str | None] | None = None,
    root_annotation: str | None = None,
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

    `annotations` is a list parallel to `san_history`; entries are the
    original PGN comment for that ply (or None). `root_annotation` is
    the pre-game comment. Commentator mode only -- the addendum tells
    the model to read them critically, not parrot. Rendered on their
    own lines so `Game moves:` keeps its place.

    Optional fields are omitted entirely when not provided."""
    lines: list[str] = []
    if engine_name:
        lines.append(f"Engine: {engine_name}")
    if opening_name:
        eco_prefix = f"[{opening_eco}] " if opening_eco else ""
        lines.append(f"Opening: {eco_prefix}{opening_name}")
    lines.append(f"Current position (FEN): {fen}")
    lines.append(f"Side to move: {_side_to_move_from_fen(fen)}")
    lines.append(f"Position under review: {_position_under_review(fen)}")
    lines.append(f"Game moves: {_render_san_pairs(san_history)}")
    if move_played:
        lines.append(f"Move played here: {move_played}")
    if result:
        lines.append(f"Game result: {result}")
    if root_annotation:
        lines.append(f"Pre-game note: {_scrub_comment_for_prompt(root_annotation)}")
    annotations_line = _render_annotations(san_history, annotations)
    if annotations_line:
        lines.append(annotations_line)
    return "\n".join(lines) + "\n"


def _render_annotations(
    san_history: list[str], annotations: list[str | None] | None,
) -> str | None:
    """Render per-ply PGN comments as `Annotations: 5.O-O {note} 12...Nxd4 {note}`.

    Returns None when there are no non-None entries or when annotations
    is None / mismatched length. Drops entries past the SAN list rather
    than raising, since caller-side capping may have truncated."""
    if not annotations:
        return None
    pairs: list[str] = []
    for i, comment in enumerate(annotations):
        if comment is None or i >= len(san_history):
            continue
        move_no, dots = _move_ref(i)
        safe = _scrub_comment_for_prompt(comment)
        if not safe:
            continue
        pairs.append(f"{move_no}{dots}{san_history[i]} {{{safe}}}")
    if not pairs:
        return None
    return f"Annotations: {' '.join(pairs)}"
