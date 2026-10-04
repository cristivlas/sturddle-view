"""AI analysis coordinator.

Owns the LLM session for live play. Drains the provider's chunk stream
onto the websocket event bus as `ai_info` events.

Runs a multi-turn agent loop: each round = one `provider.stream()` call.
On a tool_use chunk the coordinator dispatches via the registry, appends
assistant + tool_result messages, and runs another round. Bounded by
MAX_TOOL_ROUNDS so a stuck model can't burn budget forever.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import re
import time
from dataclasses import dataclass, field, fields
from typing import Awaitable, Callable, Iterable, Sequence

import chess

from ..config import (
    _DEFAULT_AI_MAX_RECOMMEND_FAILURES,
    _DEFAULT_AI_MAX_TOOL_ROUNDS,
    _DEFAULT_AI_VERIFIER_MAX_ROUNDS,
)
from ..env_utils import env_bool, env_int
from ..events import (
    ENVELOPE_GAME_ID,
    ENVELOPE_KIND,
    ENVELOPE_PAYLOAD,
    EVT_AI_INFO,
    EVT_AI_OPENING_LINKS,
    EVT_AI_POSITION_NOTE,
    EVT_AI_RECOMMENDATION,
    EVT_AI_THINKING,
    EVT_AI_TOOL_CALL,
    EVT_AI_TOOL_CALL_COMPLETE,
    EVT_AI_TOOL_CALL_FAILED,
    EVT_AI_USAGE,
    Event,
    EventBus,
)
from ..llm import (
    LLMProvider,
    Message,
    PromptMode,
    ProviderChunk,
    ProviderUsage,
    TOOL_SIGNATURE_KEY,
    ToolRegistry,
    ToolSpec,
    UnknownToolError,
    assemble_system_prompt,
    open_transcript,
    split_narrator_steers,
    strip_markdown_stream,
)
from ..llm.cancel import CancelToken
from ..llm.position_check import (
    describe_square,
    find_tool_mentions,
    has_position_flags,
    iter_false_bishop_color_refs,
    iter_false_claim_squares,
    iter_false_file_claims,
    iter_false_file_openness,
    iter_false_illegality_claims,
    iter_false_occupancy_claims,
    iter_false_tactic_claims,
    handled_continuation_spans,
    iter_illegal_continuations,
    iter_illegal_moves,
    iter_illegal_pawn_moves,
    iter_illegal_piece_moves,
    iter_illegal_square_moves,
    iter_stm_moves,
    iter_tool_label_leaks,
    truncate_at_future_line,
)
from ..llm.position_judge import POSITION_JUDGE_CALL_NAME, judge_other_position
from ..llm.tool_progress import ProgressReporter, StepFinisher, reporting_progress
from ..openings import Opening, find_opening_names
from .tools_engine import (
    ANALYZE_TOOL_NAME,
    CANDIDATES_KEY,
    MATERIAL_TOOL_NAME,
    MOVE_UCI_KEY,
    PIECE_AT_TOOL_NAME,
    RECOMMEND_MOVE_TOOL_NAME,
    REPORT_LINE_TOOL_NAME,
    TACTICS_TOOL_NAME,
    TOP_MOVES_TOOL_NAME,
    VALIDATE_MOVE_TOOL_NAME,
    RefuteCheck,
    SearchCache,
    parse_move_canonical,
    parse_move_reporting,
)
from .tools_openings import BookProvider


BoardProvider = Callable[[], chess.Board | None]
# End-of-turn verifier; returns payload for ai_recommendation, or None.
RecommendVerifier = Callable[[chess.Move, int | None, CancelToken], Awaitable[dict | None]]


log = logging.getLogger(__name__)


# Cap on agent loop rounds per turn (spec sec. Guardrails: "Tool call cap
# per agent turn"). UI-settable; env is the headless/no-UI default.
MAX_TOOL_ROUNDS = env_int("SV_AI_MAX_TOOL_ROUNDS", _DEFAULT_AI_MAX_TOOL_ROUNDS)

# Verifier sub-runs get a tighter round budget: one move, a tool call or
# two, a verdict. UI-settable; env is the headless/no-UI default.
VERIFIER_MAX_ROUNDS = env_int("SV_AI_VERIFIER_MAX_ROUNDS", _DEFAULT_AI_VERIFIER_MAX_ROUNDS)

# LLM judge that clears regex position-check flags the prose meant about a
# past/hypothetical/alternate position, not the live board. Only drops flags,
# never adds; off reverts to regex-only. Env: SV_AI_SEMANTIC_CHECK.
SEMANTIC_CHECK_ENABLED = env_bool("SV_AI_SEMANTIC_CHECK", True)

# Consecutive failed recommend_move calls before the loop force-nudges the
# model to rank candidates with top_moves instead of guessing one at a time.
MAX_RECOMMEND_FAILURES = env_int(
    "SV_AI_MAX_RECOMMEND_FAILURES", _DEFAULT_AI_MAX_RECOMMEND_FAILURES
)

_VERIFIER_MODE: PromptMode = "verifier"

# Field names summed into the per-turn usage totals (and mirrored as the
# ai_usage payload keys). Derived from ProviderUsage so a new field there
# flows through without a manual edit here.
_USAGE_TOTAL_KEYS = tuple(f.name for f in fields(ProviderUsage))


# Max length of error_detail copied into the done event. Keeps the
# bus payload small even when a provider returns a wall of HTML / a
# verbose stack trace. Full detail is in the transcript anyway.
ERROR_DETAIL_MAX_LEN = 500

# Per-round prose check against the live board. On a hit the model gets a
# gentle, fact-anchored correction. Move and claim errors get separate asks:
# a bad move may just be the other side's reply or another ply (the checker
# accepts "..." or a move number), but a square's content is plain truth.
_POSITION_CHECK_PREFIX = "[position check] "
_POSITION_CHECK_LEAD = "Quick check on the current position."
# Move/line clause: offer the outs the checker honors before asking to restate.
_POSITION_CHECK_MOVE_CLAUSE = (
    "{facts}. If you meant one of black's moves, write it with a leading "
    "\"...\" (like ...Nf6); if you meant a move from a different turn, write "
    "its move number. Otherwise restate it for the position as it stands now."
)
# Claim clause: square content is POV-independent, so just ask for a restate.
_POSITION_CHECK_CLAIM_CLAUSE = (
    "{facts}. Restate this using only the pieces in the current position."
)
# Tool-mention clause: the reader sees chess only, never the machinery. No
# strike (the phrase is woven into the sentence) -- ask for a rewrite instead.
_POSITION_CHECK_TOOL_CLAUSE = (
    "The reader sees chess only -- never name the tools or engine. Remove "
    "{mentions} and rewrite that sentence to describe only the position."
)
# Leak clause: a tool name / result key written into prose breaks the ground
# rules; the round's prose was hidden, so ask for the analysis again.
_POSITION_CHECK_LEAK_CLAUSE = (
    "Rule violation: you wrote {leaks} in the prose. Tool names and result "
    "fields never appear there -- the reader sees chess only -- so that "
    "analysis was withheld from the reader. Write it again as chess analysis "
    "only, with no tool names."
)
# Joined fact line when an illegal move is flagged (no square to describe).
_ILLEGAL_MOVE_FACT = "{move} isn't legal for the side to move"
# Rephrase clause: items the judge placed in another position. Not wrong, but
# they read as claims about the current board. Forward guidance only (the
# prose stays unstruck, so a restatement would repeat it): the anchor the
# checker honors (a numbered line) or an explicit earlier-move reference.
_POSITION_CHECK_REPHRASE_CLAUSE = (
    "{items} read as the current position, though you meant a different one. "
    "Don't restate it; from here on, name any other position you discuss: "
    "give the moves that reach it with move numbers (like 24...Bd7), or the "
    "earlier move it came before."
)

# Sent once at end-of-turn if the model never called recommend_move; a
# completeness nudge. Trailing _NO_ACK_CLAUSE suppresses the
# "Understood, I'll..." preamble.
_NO_ACK_CLAUSE = " Respond with the tool call only, no acknowledgment."
_RECOMMEND_NUDGE_PROMPTS = {
    "coach": "A `recommend_move` call is still needed." + _NO_ACK_CLAUSE,
    "commentator": (
        "A `recommend_move` call is still needed for the move you "
        "would have played in the position under review." + _NO_ACK_CLAUSE
    ),
}

# Sent once when recommend_move is accepted but the model skips the
# closing conclusion (small models treat the call as the end). One-shot.
# {san} names the recorded move so the conclusion can't drift to another.
_POST_RECOMMEND_NUDGE_PREFIX = (
    "{san} is recorded. State the one-to-two sentence conclusion "
    "now, naming the plan {san} "
)
_POST_RECOMMEND_NUDGE_PROMPTS = {
    "coach": _POST_RECOMMEND_NUDGE_PREFIX + "carries out.",
    "commentator": _POST_RECOMMEND_NUDGE_PREFIX + "reflects.",
}

# Injected once when the closing prose describes a move other than the one
# recorded -- the arrow shows the recorded move, so the two disagree on
# screen. Re-assertion is struck rather than re-prompted.
_RECOMMEND_MISMATCH_NUDGE = (
    "The recorded move is {san}, but the conclusion describes {named} "
    "instead. Restate the conclusion for {san} -- the move you submitted "
    "is the one the reader sees."
)

# Injected after MAX_RECOMMEND_FAILURES consecutive failed recommend_move
# calls: stop guessing one move at a time, rank real candidates in one
# top_moves call. Re-armed by any top_moves call.
_RECOMMEND_FAILURE_NUDGE = (
    "Stop submitting moves one at a time. Call `top_moves` with your "
    "candidate moves, read the ranking, then recommend the best one."
    + _NO_ACK_CLAUSE
)

# One-shot, both narrator modes: the model accepted a move it never
# red-teamed. Hold the accept and ask for a delegate refutation check;
# a stalled model still gets its pick on the next attempt.
_RED_TEAM_FIRST_ERROR = "red_team_first"
_RED_TEAM_FIRST_NUDGE = (
    "Before settling, hand this move to `delegate` to red-team it; "
    "submit again once the verdict holds." + _NO_ACK_CLAUSE
)

# Verifier completeness nudge: a verdict before any top_moves ranking that
# includes the move under test. One-shot per sub-run; a repeat unsearched
# verdict is dropped (verdict_withheld).
_VERIFIER_SEARCH_NUDGE = (
    "A verdict needs a search of the move under test first: call "
    "`top_moves` with it and the strongest alternatives." + _NO_ACK_CLAUSE
)

# A verifier reply must open with one of these (the client badges off the
# same words). Anything else -- a question, chatter -- is not a verdict.
# Leading non-alphanumerics skip wrappers the stream strip leaves (*, #, >, ").
_VERDICT_HOLDS = "holds"
_VERDICT_REFUTED = "refuted"
_VERDICT_LEAD_RE = re.compile(
    rf"^[^A-Za-z0-9]*(?P<kind>{_VERDICT_HOLDS}|{_VERDICT_REFUTED})(?![A-Za-z0-9])",
    re.IGNORECASE,
)
# Delegate result key carrying the verdict prose; also a label the
# narrator must never copy into its prose ("Verdict:").
_VERDICT_KEY = "verdict"

# One-shot: the verifier replied with something other than a verdict. A
# second non-verdict is dropped (no verdict), never passed on.
_VERIFIER_VERDICT_NUDGE = (
    f'Reply with the verdict only: open with "{_VERDICT_HOLDS}" or '
    f'"{_VERDICT_REFUTED}", then a one-line reason. No questions.'
)

# Label introducing the narrator's question in the verifier's user
# message, below the inherited position context.
_VERIFIER_QUESTION_LABEL = "Question to verify:"


@dataclass(frozen=True, slots=True)
class VerifierResult:
    """A verifier sub-run's outcome: the verdict prose ("" when none
    survived the verdict gates) and whether the round cap cut it short."""
    verdict: str
    round_cap: bool = False


# Delegate tool invokes this: narrator question and the parsed move under
# test in, the sub-run's outcome out. No cancel token -- the sub-run shares
# the turn's self._cancel_token.
VerifierRunner = Callable[[str, chess.Move], Awaitable[VerifierResult]]


# A delegate with no usable verdict. no_verdict: the round cap cut the
# sub-run short (the client's gear deep-links to that setting).
# verdict_withheld: a reply the gates rejected -- more rounds won't help.
# The narrator reads the detail, so it names neither the engine nor tools.
_NO_VERDICT_ERROR = "no_verdict"
_VERDICT_WITHHELD_ERROR = "verdict_withheld"
_NO_CONCLUSION_DETAIL = 'no conclusion. Try increasing "Max subagent rounds"'
_UNUSABLE_REPLY_DETAIL = "discarded: the reply was not a usable verdict"
_FALSE_FACT_DETAIL = "discarded: the verdict misstated the position"
_UNCONFIRMED_REFUTATION_DETAIL = "discarded: the refutation does not hold up"
# Set on a withheld refutation the engine overruled: the move survived the
# adversarial check, so it satisfies the red-team hold like a verdict does.
MOVE_SURVIVED_KEY = "move_survived"


def _delegate_error(kind: str, detail: str) -> dict:
    return {"error": kind, "detail": detail}


def _verdict_kind(verdict: str) -> str | None:
    """'holds' / 'refuted' from the verdict's lead word, else None."""
    m = _VERDICT_LEAD_RE.match(verdict)
    return m.group("kind").lower() if m else None


# `delegate` is defined in this module (DELEGATE_TOOL_SPEC); the engine
# tool names are imported from tools_engine (single source of truth).
_DELEGATE_TOOL_NAME = "delegate"


# Declarative, not imperative: a "you do X" instruction invites small
# models to reply "Understood, I will..." as prose. Describing behavior
# removes the thing being acknowledged (same pattern across all cards).
_DELEGATE_TOOL_CARD = (
    "One move per call. The result echoes the canonical `move_uci` and a "
    "holds/refuted verdict about the live position, advisory not quotable."
)


# Prefixed to the delegated question so the verifier knows the exact
# move under attack even when the narrator's question doesn't name it.
_MOVE_UNDER_TEST_PREFIX = "Move under test:"


DELEGATE_TOOL_SPEC = ToolSpec(
    name=_DELEGATE_TOOL_NAME,
    description=(
        "Red-team a candidate move before committing to it: an adversary "
        "searches the live position for a refutation -- the opponent's "
        "strongest reply, the tactic it allows -- and returns a verdict, "
        "holds or refuted with the reason."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "move": {
                "type": "string",
                "description": (
                    "The move to red-team, UCI or SAN (e.g. 'Nf3', 'g1f3')."
                ),
            },
            "question": {
                "type": "string",
                "description": (
                    "What to probe, e.g. "
                    "'does it drop material to the d5 break?'."
                ),
            },
        },
        "required": ["move", "question"],
    },
    card=_DELEGATE_TOOL_CARD,
)


def make_delegate_tool(
    runner: VerifierRunner,
    board_provider: BoardProvider,
    refute_check: RefuteCheck | None = None,
) -> Callable:
    """Build the `delegate` tool. Parses `move` to canonical UCI against
    the live board, dispatches the narrator's question to a verifier
    sub-run, and echoes the canonical `move_uci` back. Malformed input
    returns a structured error so the narrator can recover. A verdict that
    misstates the board, or a "refuted" the engine (`refute_check`) doesn't
    confirm, is withheld; None skips the engine check."""
    async def delegate(input_: dict, *, cancel_token: CancelToken) -> dict:
        question = input_.get("question")
        if not isinstance(question, str) or not question.strip():
            return {"error": "invalid_input", "detail": "question must be a non-empty string"}
        raw_move = input_.get("move")
        if not isinstance(raw_move, str) or not raw_move.strip():
            return {"error": "invalid_input", "detail": "move must be a non-empty string"}
        board = board_provider()
        if board is None:
            return {"error": "invalid_move", "detail": f"could not parse {raw_move!r}"}
        # Surface the real kind (illegal_move vs invalid_move) and the FEN we
        # validated against, so the narrator sees e.g. a wrong-side-to-move
        # move for what it is instead of a flat "could not parse".
        move, kind, detail = parse_move_reporting(board, raw_move)
        if move is None:
            return {"error": kind, "detail": detail, "fen": board.fen()}
        result = await runner(
            f"{_MOVE_UNDER_TEST_PREFIX} {board.san(move)}. {question.strip()}",
            move,
        )
        verdict = result.verdict
        if not verdict:
            if result.round_cap:
                return _delegate_error(_NO_VERDICT_ERROR, _NO_CONCLUSION_DETAIL)
            return _delegate_error(_VERDICT_WITHHELD_ERROR, _UNUSABLE_REPLY_DETAIL)
        # Strict non-LLM validation of the verdict prose. The verdict restates
        # the move under test (legal pre-move) and reasons about its replies
        # (legal post-move), so it straddles the move boundary; validate against
        # both boards and flag only a claim wrong on NEITHER -- a real illegal
        # move/false claim, not a boundary artifact. A false reason discredits
        # the verdict word too, so the narrator never sees it. Full stack on
        # the copy so numbered history refs keep their replay free-pass.
        after = board.copy()
        after.push(move)
        if has_position_flags(verdict, board, after):
            log.info("delegate verdict misstates the board; withheld: %r", verdict)
            return _delegate_error(_VERDICT_WITHHELD_ERROR, _FALSE_FACT_DETAIL)
        # A refutation must survive the engine: the same dominance test the
        # final recommend_move gate applies. Search failures pass it through.
        if (
            refute_check is not None
            and _verdict_kind(verdict) == _VERDICT_REFUTED
            and await refute_check(move, cancel_token) is False
        ):
            log.info("delegate refutation of %s not confirmed; withheld", move.uci())
            return {
                **_delegate_error(_VERDICT_WITHHELD_ERROR, _UNCONFIRMED_REFUTATION_DETAIL),
                MOVE_SURVIVED_KEY: True,
            }
        return {MOVE_UCI_KEY: move.uci(), _VERDICT_KEY: verdict}

    return delegate


# Tool-arg normalizers for the dedup cache. Each maps (input, board) ->
# hashable key, or None to skip caching this call.


def _norm_move_arg(input_: dict, board: chess.Board | None) -> tuple | None:
    # depth is part of the key for recommend_move; harmless for
    # validate_move which doesn't accept it (always None).
    raw = input_.get("move")
    if not isinstance(raw, str) or board is None:
        return None
    move = parse_move_canonical(board, raw)
    return ("move", move.uci(), input_.get("depth")) if move else None


def _norm_square_arg(input_: dict, board: chess.Board | None) -> tuple | None:
    raw = input_.get("square")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        square = chess.square_name(chess.parse_square(raw.lower()))
    except ValueError:
        return None
    # piece_at reads a supplied fen or the live board; key on the position
    # (EPD -- clocks ignored) so a query against one position can't reuse a
    # result from another. An explicit fen matching the live board dedups.
    fen = input_.get("fen")
    if isinstance(fen, str) and fen.strip():
        try:
            # 'startpos' isn't expanded here, so it won't dedup (matches
            # _canonical_fen); the tool itself still resolves it.
            pos = chess.Board(fen.strip()).epd()
        except ValueError:
            return None
    elif board is not None:
        pos = board.epd()
    else:
        return None
    return ("square", pos, square)


def _norm_top_moves(input_: dict, board: chess.Board | None) -> tuple | None:
    # Two-tier key: "canonical" (all candidates parse -> tuple of UCIs)
    # or "raw" fallback (stripped+lowered strings). Tags don't collide.
    raw_moves = input_.get("moves")
    if not isinstance(raw_moves, list) or board is None:
        return None
    ucis: list[str] = []
    all_parseable = True
    for r in raw_moves:
        if not isinstance(r, str):
            return None
        move = parse_move_canonical(board, r)
        if move is None:
            all_parseable = False
            break
        ucis.append(move.uci())
    depth = input_.get("depth")
    if all_parseable:
        # Sort: engine sees `searchmoves` as a set, list order is irrelevant.
        return ("moves", "canonical", tuple(sorted(ucis)), depth)
    raws = tuple(sorted(r.strip().lower() for r in raw_moves))
    return ("moves", "raw", raws, depth)


def _canonical_fen(input_: dict) -> str | None:
    # Canonical key for a position: normalized FEN minus the halfmove and
    # fullmove counters, so the same board with different clocks shares a
    # key. 'startpos' isn't expanded here, so it won't dedup.
    fen = input_.get("fen")
    if not isinstance(fen, str) or not fen.strip():
        return None
    try:
        board_fen = chess.Board(fen.strip()).fen()
    except ValueError:
        return None
    # Keep the first 4 fields (placement, side, castling, en passant);
    # the last 2 (halfmove clock, fullmove number) don't affect results.
    return " ".join(board_fen.split(" ")[:4])


def _norm_report_line(input_: dict, board: chess.Board | None) -> tuple | None:
    # Key on the start position (EPD) + the raw move strings in order. Raw
    # (not canonical) so a broken line -- one a re-submit would repeat
    # verbatim -- still dedups; order matters, so no sorting.
    raw_moves = input_.get("moves")
    if not isinstance(raw_moves, list) or not raw_moves:
        return None
    if not all(isinstance(m, str) for m in raw_moves):
        return None
    from_fen = input_.get("from_fen")
    if isinstance(from_fen, str) and from_fen.strip():
        try:
            pos = chess.Board(from_fen.strip()).epd()
        except ValueError:
            return None
    elif board is not None:
        pos = board.epd()
    else:
        return None
    moves = tuple(m.strip().lower() for m in raw_moves)
    return (REPORT_LINE_TOOL_NAME, pos, moves)


def _norm_analyze(input_: dict, board: chess.Board | None) -> tuple | None:
    canonical = _canonical_fen(input_)
    if canonical is None:
        return None
    return (ANALYZE_TOOL_NAME, canonical, input_.get("depth"))


def _fen_only_normalizer(
    tool_name: str,
) -> Callable[[dict, chess.Board | None], tuple | None]:
    """Dedup key for a tool whose only input is a FEN (material, tactics)."""
    def norm(input_: dict, board: chess.Board | None) -> tuple | None:
        canonical = _canonical_fen(input_)
        if canonical is None:
            return None
        return (tool_name, canonical)
    return norm


_NORMALIZERS: dict[str, Callable[[dict, chess.Board | None], tuple | None]] = {
    RECOMMEND_MOVE_TOOL_NAME: _norm_move_arg,
    VALIDATE_MOVE_TOOL_NAME: _norm_move_arg,
    PIECE_AT_TOOL_NAME: _norm_square_arg,
    TOP_MOVES_TOOL_NAME: _norm_top_moves,
    REPORT_LINE_TOOL_NAME: _norm_report_line,
    ANALYZE_TOOL_NAME: _norm_analyze,
    MATERIAL_TOOL_NAME: _fen_only_normalizer(MATERIAL_TOOL_NAME),
    TACTICS_TOOL_NAME: _fen_only_normalizer(TACTICS_TOOL_NAME),
}


# Stand-in for an assistant turn that produced no real content. Anthropic
# rejects empty assistant content, so a silent round still needs a valid
# turn to keep role alternation (the post-recommend nudge relies on this).
_EMPTY_TURN_PLACEHOLDER = "(no output)"


def _assistant_message(chunks: list[ProviderChunk]) -> Message:
    """Reassemble a provider chunk stream into the assistant turn that
    must be appended to messages before sending the next round.

    Anthropic's wire shape requires the assistant content to be a list
    of blocks ({type:"text"|"tool_use", ...}) -- we collapse contiguous
    text deltas into one block and emit a tool_use block per call. Blank
    text blocks are dropped; a turn that ends up empty gets a placeholder
    so it stays wire-valid. Thinking is not fed back (see the loop below).
    """
    content: list[dict] = []
    text_buf: list[str] = []

    def _flush_text() -> None:
        # Drop blank text blocks -- Anthropic rejects empty content, and a
        # whitespace-only block carries nothing the model needs to re-read.
        joined = "".join(text_buf)
        text_buf.clear()
        if joined.strip():
            content.append({"type": "text", "text": joined})

    for c in chunks:
        # Thinking is omitted: we lack the signature Anthropic needs on a
        # returned thinking block, and reasoning-as-text is the wrong shape.
        if c.kind == "text":
            text_buf.append(c.text)
        elif c.kind == "tool_use":
            _flush_text()
            block = {
                "type": "tool_use",
                "id": c.tool_use_id,
                "name": c.tool_name,
                "input": c.tool_input,
            }
            # Carry an opaque provider signature (Gemini's thought_signature)
            # so it can be echoed back next round. Empty for providers that
            # don't use it; the wire mapping lives in openai_compat.
            if c.tool_signature:
                block[TOOL_SIGNATURE_KEY] = c.tool_signature
            content.append(block)
    _flush_text()
    if not content:
        content.append({"type": "text", "text": _EMPTY_TURN_PLACEHOLDER})
    return {"role": "assistant", "content": content}


class _ThinkTimer:
    """Times a round's thinking phase. Started on the first thinking chunk,
    spent once into the first non-thinking event's payload as thinking_ms.
    Spending is idempotent so prose and tool-call paths can both call it."""

    def __init__(self) -> None:
        self._start: float | None = None
        self._spent = False

    def start(self) -> None:
        if self._start is None:
            self._start = time.monotonic()

    def spend_into(self, payload: dict) -> None:
        ms = self.flush_ms()
        if ms is not None:
            payload["thinking_ms"] = ms

    def flush_ms(self) -> int | None:
        # Elapsed thinking, once. None if already spent or never started.
        if self._start is None or self._spent:
            return None
        self._spent = True
        return round((time.monotonic() - self._start) * 1000)


async def _flush_think(emit, timer: _ThinkTimer, game_id, round_index: int) -> None:
    # Carry unspent thinking on a delta-less ai_thinking when no prose/tool
    # event surfaced it (thinking-only / cached / nested-tool rounds).
    ms = timer.flush_ms()
    if ms is not None:
        await emit(Event(
            kind=EVT_AI_THINKING,
            game_id=game_id,
            payload={"round": round_index, "thinking_ms": ms},
        ))


def _round_produced_output(chunks: list[ProviderChunk]) -> bool:
    """True iff the round produced real fed-back output -- a tool_use or
    non-whitespace text (thinking is not fed back, so it doesn't count).
    The completeness nudge ends the turn on a silent round instead of
    re-nudging a model that produced nothing."""
    for c in chunks:
        if c.kind == "tool_use":
            return True
        if c.kind == "text" and c.text and c.text.strip():
            return True
    return False


def _is_accepted_recommend(output) -> bool:
    """True when a recommend_move result is an accept (ok + a uci string)."""
    return (
        isinstance(output, dict)
        and bool(output.get("ok"))
        and isinstance(output.get("uci"), str)
    )


def _ranks_move(
    tool: ProviderChunk, output, move: chess.Move | None,
) -> bool:
    """True when `tool` is a top_moves ranking whose candidates include
    `move` -- the verifier's evidence it searched the move under test."""
    if move is None or tool.tool_name != TOP_MOVES_TOOL_NAME:
        return False
    if not isinstance(output, dict):
        return False
    candidates = output.get(CANDIDATES_KEY)
    if not isinstance(candidates, list):
        return False
    return any(
        isinstance(c, dict) and c.get(MOVE_UCI_KEY) == move.uci()
        for c in candidates
    )


def _inject_nudge(
    messages: list[Message], round_chunks: list[ProviderChunk], content: str,
) -> None:
    """Append the model's own round turn, then a user nudge. The assistant
    message lands first so the model sees its analysis above the request.
    An empty round becomes a placeholder assistant turn (see
    `_assistant_message`) so the post-recommend nudge can still prompt a
    model that went silent after its accepting call."""
    messages.append(_assistant_message(round_chunks))
    messages.append({"role": "user", "content": content})


def _tool_result_message(
    tool_use_id: str, result: dict | str, *, card: str | None = None
) -> Message:
    """Build the user-role tool_result message that closes one tool call.

    Wire shape mirrors Anthropic's. The provider for Ollama translates
    to OpenAI on the way out.

    `card` (tool-card body) is appended as a separate text content block
    inside the same user message. Kept distinct from the tool_result
    content -- transcripts and log parsers see "data" vs "guidance"
    cleanly. Injected by the coordinator only on the first call to a
    given tool per turn (see docs/ai-analysis-spec.md sec. Skills layer).
    """
    if not isinstance(result, str):
        # ensure_ascii=False: escaped non-ASCII (accented opening names)
        # gets parroted into the model's prose verbatim.
        result = json.dumps(result, ensure_ascii=False)
    content: list[dict] = [
        {"type": "tool_result", "tool_use_id": tool_use_id, "content": result}
    ]
    if card:
        content.append({"type": "text", "text": card})
    return {"role": "user", "content": content}


# Emit sink type: the loop calls it for every ai_* event. Narrator passes
# the coordinator's real _emit; the verifier sub-run passes a filtering
# sink that forwards its tool-call activity but suppresses its prose.
EmitSink = Callable[[Event], Awaitable[None]]


# Event kinds a verifier sub-run forwards to the UI: its engine searches
# (so the user sees progress under "Verifying line"), but NOT its prose
# (ai_info) or reasoning (ai_thinking) -- the verdict stays internal.
_VERIFIER_FORWARDED_KINDS = frozenset(
    {"ai_tool_call", "ai_tool_call_complete", "ai_tool_call_failed"}
)


@dataclass(slots=True)
class _LoopConfig:
    """Per-invocation knobs for the shared agent loop. Narrator and
    verifier sub-runs differ only in these fields; everything else in
    `_run_loop` is shared."""
    mode: PromptMode
    registry: ToolRegistry
    system_prompt: str
    tool_schemas: list[dict] | None
    max_rounds: int
    emit: EmitSink
    game_id: str | None
    provider: LLMProvider
    transcript: object
    # Narrator: nudge to recommend_move when the model never submits one.
    # Verifier: nudge to call a tool before concluding. None disables.
    completeness_nudge: str | None = None
    # True only for the narrator loop -- enables recommend_move tracking
    # so the end-of-turn verifier fires on the chosen move.
    track_recommend: bool = False
    # Hold the first accepted recommend_move until a delegate verdict ran
    # this turn. Only set when `delegate` is actually registered.
    enforce_red_team: bool = False
    # Theory reply for the turn (UCI). A recommend_move naming it skips the
    # red-team hold -- the book vetted it, not `delegate`.
    book_move_uci: str | None = None
    # Per-call thinking override passed to provider.stream(). None = use
    # the provider's setting (narrator); False = force off (verifier).
    thinking_override: bool | None = None
    # Require a tool call on round 0 (verifier: a tool-free verdict is
    # structurally impossible where the provider honors tool_choice, so
    # the no-tool nudge round never runs). Nudge stays as the fallback
    # for providers/models that ignore it.
    force_first_round_tool: bool = False
    # Verifier: the final reply must open with holds/refuted; a non-verdict
    # draws one nudge, then is dropped (empty final_text -> verdict_withheld).
    require_verdict: bool = False
    # Narrator: link opening names in its clean prose (ai_opening_links).
    # The verifier's prose never reaches the panel.
    link_openings: bool = False
    # Verifier: the move under test. A verdict counts only after a top_moves
    # ranking that includes it -- analyze on the live FEN scores the position
    # before the move, so it can't judge the move.
    move_under_test: chess.Move | None = None


@dataclass(slots=True)
class _LoopResult:
    """What the shared loop reports back. `final_text` is the last round's
    prose -- the verifier verdict, with cross-round tool-call self-talk
    dropped (falls back to all-rounds text on round-cap). `recommended_uci`
    is set only when the narrator tracked an accepted recommend_move.
    `rounds` counts provider calls, silent ones included."""
    final_text: str = ""
    recommended_uci: str | None = None
    recommended_depth: int | None = None
    round_cap_hit: bool = False
    text_published: bool = False
    rounds: int = 0


@dataclass(slots=True)
class _LoopState:
    """Turn-wide mutable state of one `_run_loop` call, shared by its
    per-round helpers."""
    cards_injected: set[str] = field(default_factory=set)
    last_call: tuple[tuple, dict] | None = None
    text_parts: list[str] = field(default_factory=list)
    recommended_uci: str | None = None
    recommended_depth: int | None = None
    # SAN of the accepted move -- what the nudges and the mismatch
    # corrective name, since prose speaks SAN, not uci.
    recommended_san: str | None = None
    # Verifier: flips once a top_moves ranking includes the move under test.
    move_searched: bool = False
    nudge_sent: bool = False
    # Narrator: re-nudge toward an accepted recommend_move each clean
    # exit until one lands, but stop once a nudge draws no new attempt.
    # These two track "progress since the last nudge" for that guard.
    recommend_attempts: int = 0
    attempts_at_last_nudge: int = -1
    # Consecutive failed recommend_move calls (illegal/rejected); when it
    # hits MAX_RECOMMEND_FAILURES the model is nudged to use top_moves.
    # Reset by an accepted recommend or a top_moves call.
    consecutive_recommend_failures: int = 0
    recommend_failure_nudge_armed: bool = True
    # True once a delegate call returned a verdict this turn. Gates the
    # red-team hold: the first accepted recommend_move without one is
    # held so the pick survives an adversarial check before it ships.
    red_teamed: bool = False
    red_team_nudge_sent: bool = False
    # Flips when prose lands after recommend_move is accepted (the
    # closing conclusion); gates the post-recommend nudge.
    prose_after_recommend: bool = False
    post_recommend_nudge_sent: bool = False
    # One-shot: after the corrective, a re-asserted wrong move is struck.
    mismatch_nudge_sent: bool = False
    verdict_nudge_sent: bool = False
    round_cap_hit: bool = True  # flipped to False on natural exit
    text_published: bool = False  # flips on first non-whitespace text chunk
    final_text: str = ""  # last round's prose only (verifier verdict)
    # Position-check items already corrected this turn. A re-flagged item
    # is struck without another corrective: the model's acknowledgment
    # repeats the wrong phrase, so re-prompting loops until the round cap.
    corrected_items: set[str] = field(default_factory=set)
    # Labels already asked to name their other position. One-shot: prose
    # about a past position can't be phrased past the regex, so a re-tag
    # must not re-ask forever.
    rephrase_asked: set[str] = field(default_factory=set)
    # Set by the narrator's completeness nudge: the next round is forced.
    force_tool_next: bool = False
    # One-shot: a silent round after a fresh attempt still gets nudged.
    silent_renudge_used: bool = False
    rounds: int = 0


@dataclass(slots=True)
class _Round:
    """One provider round: its chunks and the tool_use that ended it."""
    index: int
    chunks: list[ProviderChunk] = field(default_factory=list)
    pending_tool: ProviderChunk | None = None
    # Per-round thinking duration, carried on the first non-thinking
    # event so the client shows it correctly on replay (a client-side
    # Date.now() delta collapses to ~0 when replay fires at once).
    think_timer: _ThinkTimer = field(default_factory=_ThinkTimer)

    @property
    def had_text(self) -> bool:
        return any(c.kind == "text" and c.text for c in self.chunks)


def _sort_surfaces(surfaces: list[str]) -> list[str]:
    """Longest first so a span isn't half-matched by a shorter one nested
    inside it; deduped."""
    return sorted(set(surfaces), key=len, reverse=True)


# Repeat keys: square for claims (reworded claims match), else lowercased
# label. Type-prefixed so claim e4 != pawn move e4; moves and lines share a
# prefix (a move in a line and standalone is one error).
_MOVE_KEY_PREFIX = "mv:"
_CLAIM_KEY_PREFIX = "sq:"
_FACT_KEY_PREFIX = "fact:"
_TOOL_KEY_PREFIX = "tool:"


def _move_key(item: tuple[str, str]) -> str:
    return _MOVE_KEY_PREFIX + item[1].lower()


def _claim_key(item: tuple[str, str, str]) -> str:
    return _CLAIM_KEY_PREFIX + item[2]


def _fact_key(item: tuple[str, str, str]) -> str:
    return _FACT_KEY_PREFIX + item[1].lower()


def _tool_key(item: str) -> str:
    return _TOOL_KEY_PREFIX + item


def _leak_key(item: tuple[str, str]) -> str:
    return _TOOL_KEY_PREFIX + item[1]


def _split_by_key(items, key, corrected: set[str]) -> tuple[list, list]:
    """(new, repeat): items whose key is / isn't in `corrected`."""
    new = [it for it in items if key(it) not in corrected]
    rep = [it for it in items if key(it) in corrected]
    return new, rep


@dataclass(slots=True)
class _PositionCheck:
    """One round's prose-check result. Each flagged item carries both its
    exact prose `surface` (for the UI to strike) and a normalized `label`
    (for facts/keys); claims also carry their `square`. Empty when clean."""
    board: chess.Board | None
    move_pairs: list[tuple[str, str]]
    claim_triples: list[tuple[str, str, str]]
    line_pairs: list[tuple[str, str]]
    # Tool/engine self-references caught in the prose. Corrective-only -- not
    # struck, since the phrase is woven into the sentence (see find_tool_mentions).
    tool_mentions: list[str] = field(default_factory=list)
    # Plain board-truth claims with a precomputed corrective, each
    # (surface, label, fact): bishop-color references, piece-on-file and
    # file-openness claims, pin/fork claims. Never judged (see judge_items).
    fact_triples: list[tuple[str, str, str]] = field(default_factory=list)
    # Tool names / result keys written into the prose, each (surface, name):
    # 'recommend_move', 'Verdict:'. A rule violation, not a board error: a new
    # leak hides the round's prose from the reader; a repeat is struck.
    tool_leaks: list[tuple[str, str]] = field(default_factory=list)

    @property
    def hit(self) -> bool:
        return bool(
            self.move_pairs or self.claim_triples
            or self.line_pairs or self.tool_mentions
            or self.fact_triples or self.tool_leaks
        )

    @property
    def move_labels(self) -> list[str]:
        return [label for _surface, label in self.move_pairs]

    @property
    def line_labels(self) -> list[str]:
        return [label for _surface, label in self.line_pairs]

    @property
    def surfaces(self) -> list[str]:
        # Exact prose spans for the client to strike.
        return _sort_surfaces(
            [s for s, _ in self.move_pairs]
            + [s for s, _, _ in self.claim_triples]
            + [s for s, _ in self.line_pairs]
            + [s for s, _, _ in self.fact_triples]
        )

    @property
    def hides_prose(self) -> bool:
        return bool(self.tool_leaks)

    @property
    def leak_surfaces(self) -> list[str]:
        return [s for s, _ in self.tool_leaks]

    @property
    def keys(self) -> set[str]:
        """Repeat keys of every flagged item (see _move_key and friends)."""
        return (
            set(map(_move_key, self.move_pairs))
            | set(map(_claim_key, self.claim_triples))
            | set(map(_move_key, self.line_pairs))
            | set(map(_tool_key, self.tool_mentions))
            | set(map(_fact_key, self.fact_triples))
            | set(map(_leak_key, self.tool_leaks))
        )

    def partition(self, corrected: set[str]) -> tuple[_PositionCheck, _PositionCheck]:
        """Split into (new, repeat) by repeat key against `corrected`."""
        moves = _split_by_key(self.move_pairs, _move_key, corrected)
        claims = _split_by_key(self.claim_triples, _claim_key, corrected)
        lines = _split_by_key(self.line_pairs, _move_key, corrected)
        tools = _split_by_key(self.tool_mentions, _tool_key, corrected)
        facts = _split_by_key(self.fact_triples, _fact_key, corrected)
        leaks = _split_by_key(self.tool_leaks, _leak_key, corrected)
        return (
            _PositionCheck(
                self.board, moves[0], claims[0], lines[0], tools[0], facts[0], leaks[0],
            ),
            _PositionCheck(
                self.board, moves[1], claims[1], lines[1], tools[1], facts[1], leaks[1],
            ),
        )

    @property
    def judge_items(self) -> list[tuple[str, str]]:
        """(label, board fact) for every board-context flag the judge may rule
        on (moves, lines, claims); the fact is what makes it false now. Never
        included: tool mentions (board-independent style violations) and fact
        labels (precomputed board facts; the judge kept clearing the bishop
        class wrongly, so the regex verdict is final for the whole class)."""
        if self.board is None:
            return []
        return (
            [(label, _ILLEGAL_MOVE_FACT.format(move=label))
             for label in self.move_labels + self.line_labels]
            + [(label, describe_square(square, self.board))
               for _surface, label, square in self.claim_triples]
        )

    def without_labels(self, cleared: set[str]) -> _PositionCheck:
        """A copy with every flag whose label is in `cleared` dropped. Tool
        mentions and leaks pass through (never judged). Empty `cleared` is a
        no-op."""
        if not cleared:
            return self
        return _PositionCheck(
            self.board,
            [(s, l) for s, l in self.move_pairs if l not in cleared],
            [(s, l, sq) for s, l, sq in self.claim_triples if l not in cleared],
            [(s, l) for s, l in self.line_pairs if l not in cleared],
            self.tool_mentions,
            [(s, l, f) for s, l, f in self.fact_triples if l not in cleared],
            self.tool_leaks,
        )


def _progress_reporter(
    emit: EmitSink, game_id: str | None, round_index: int, parent_id: str,
) -> ProgressReporter:
    """Surface each progress step of the tool `parent_id` as a panel row
    nested under it, and its finish as that row's result (see tool_progress)."""
    steps = itertools.count(1)

    async def report(name: str, input_: dict) -> StepFinisher:
        step_id = f"{parent_id}-{name}-{next(steps)}"
        await emit(Event(
            kind=EVT_AI_TOOL_CALL,
            game_id=game_id,
            payload={
                "round": round_index,
                "name": name,
                "input": input_,
                "tool_use_id": step_id,
                "parent_tool_use_id": parent_id,
            },
        ))

        async def finish(output: object) -> None:
            await emit(Event(
                kind=EVT_AI_TOOL_CALL_COMPLETE,
                game_id=game_id,
                payload={
                    "round": round_index,
                    "name": name,
                    "tool_use_id": step_id,
                    "output": output,
                },
            ))

        return finish

    return report


def _quote_join(items: Sequence[str]) -> str:
    """'"a", "b"' -- prose spans named back to the model."""
    return ", ".join(f'"{item}"' for item in items)


def _judge_summary(labels: list[str], cleared: set[str]) -> str:
    """Panel OUT text: 'cleared 1/2: rook on b1; kept: Nf5'."""
    kept = [label for label in labels if label not in cleared]
    dropped = [label for label in labels if label in cleared]
    head = f"cleared {len(dropped)}/{len(labels)}"
    if dropped:
        head += ": " + ", ".join(dropped)
    return head + (f"; kept: {', '.join(kept)}" if kept else "")


class AIAnalysisCoordinator:
    def __init__(
        self,
        bus: EventBus,
        provider: LLMProvider,
        registry: ToolRegistry | None = None,
        *,
        board_provider: BoardProvider | None = None,
        recommend_verifier: RecommendVerifier | None = None,
        verifier_registry: ToolRegistry | None = None,
        search_cache: SearchCache | None = None,
        book_provider: BookProvider | None = None,
    ) -> None:
        self._bus = bus
        # Default provider for callers that don't supply one per turn.
        # Per-turn override (run(provider=...)) is the seam later phases
        # use to swap prompts / agents without rebuilding the coordinator.
        self._provider = provider
        self._registry = registry if registry is not None else ToolRegistry()
        # Engine-backed toolset for verifier sub-runs (delegate target).
        # When None, `delegate` has nothing to dispatch to and the
        # narrator runs without subagents -- the pre-subagent behavior.
        self._verifier_registry = verifier_registry
        # Board provider gives the dedup-key normalizers the live position
        # to canonicalize move/square args against. None disables dedup
        # keying for those tools (tests / non-live callers).
        self._board_provider = board_provider
        # End-of-turn verifier; None disables it.
        self._recommend_verifier = recommend_verifier
        # Cross-tool engine-search cache, shared with the engine tools.
        # Cleared at turn start (position is stable within a turn, not
        # across). None when the tools aren't cache-wired (tests).
        self._search_cache = search_cache
        # Opening book: lets prose links tell another opening's full name
        # from a short form of the turn's own. None skips that guard.
        self._book_provider = book_provider
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._cancel_token: CancelToken | None = None
        # Provider active for the current turn. Set in run() so verifier
        # sub-runs (delegate) reuse the same per-turn provider instead of
        # the default. None outside a turn.
        self._active_provider: LLMProvider | None = None
        # Opening user message of the turn (FEN, side-to-move, moves).
        # Lets verifier sub-runs ground their tool calls in the same
        # position the narrator sees. Empty outside a turn.
        self._turn_context: str = ""
        # True when this turn carries the opening-theory steer: prose may
        # cite off-board sibling-variation moves, so board-legality checks
        # are skipped (see _position_check). False outside a turn.
        self._opening_turn: bool = False
        # The turn's book move (UCI); recommend_move accepts it unsearched.
        self._turn_book_move: str | None = None
        # Openings put in front of the narrator this turn, by name: the
        # game's own plus every related_openings row. Prose naming one is
        # linked to its line (see _emit_opening_links). Empty outside a turn.
        self._turn_openings: dict[str, Opening] = {}
        # tool_use_id of the in-flight delegate call; its verifier's
        # nested tool events stamp this so the client renders them under
        # the right "Verifying line" row. Late-bound. None when idle.
        self._active_delegate_id: str | None = None
        # game_id of the current turn, for stamping verifier-origin tool
        # events so the client muxes them into this session. None outside
        # a turn.
        self._turn_game_id: str | None = None
        # Round cap for verifier sub-runs this turn. Set in run() from the
        # caller's setting; the module default applies outside a turn.
        self._verifier_max_rounds: int = VERIFIER_MAX_ROUNDS
        # True if any delegate's verifier sub-run capped out this turn.
        self._verifier_round_cap_hit: bool = False
        # In-mem buffer of events emitted by the current/most-recent
        # turn. Reset on run() start, cleared on analysis stop. Lets a
        # client reconnecting mid-analysis rehydrate the panel.
        self._replay_buffer: list[dict] = []
        # Per-turn token totals (narrator + verifier sub-runs combined).
        # None until the provider reports usage -- providers without
        # accounting leave it None, so the done event omits `usage`.
        self._turn_usage: dict[str, int] | None = None
        # Monotonic per-turn sequence stamped on each emitted event;
        # the client uses it to dedupe replay vs live events.
        self._seq = 0

    async def run(
        self,
        *,
        game_id: str | None = None,
        provider: LLMProvider | None = None,
        mode: PromptMode = "coach",
        user_message: str | None = None,
        book_move_uci: str | None = None,
        book_alternatives: tuple[str, ...] = (),
        opening: Opening | None = None,
        max_tool_rounds: int = MAX_TOOL_ROUNDS,
        verifier_max_rounds: int = VERIFIER_MAX_ROUNDS,
    ) -> None:
        """Run one analysis turn end-to-end.

        Loops: provider round -> on tool_use, dispatch via registry,
        append assistant + tool_result, next round. Stops when the model
        returns without a tool_use or `max_tool_rounds` is reached. Emits
        a terminal ai_info event on every exit path so the UI never
        hangs. `max_tool_rounds`/`verifier_max_rounds` default to the
        env-backed module caps; the UI overrides them per turn.

        `user_message` carries the game context (FEN + SAN history) the
        agent needs to actually analyze something. Built by the caller
        (`api/ai.py` for live play) via `build_initial_user_message`.
        None falls back to an empty user message for tests that don't
        care about position context.

        `book_move_uci` is the theory reply the caller found for the
        position: a `recommend_move` of it is accepted without the
        red-team hold. `book_alternatives` (UCI) ride the
        `ai_recommendation` payload when that move is the accepted pick,
        for the board's secondary arrows. `opening` is the opening the
        game is in; prose naming it is linked to its line.
        """
        active = provider or self._provider
        system_prompt = assemble_system_prompt(mode, tools=self._registry.specs())
        opening_user_content = user_message if user_message is not None else ""
        async with self._lock:
            self._task = asyncio.current_task()
            self._cancel_token = CancelToken()
            self._active_provider = active
            # turn_context grounds verifier sub-runs. Strip the narrator-only
            # steers: opening steers name tools the verifier registry lacks,
            # the playbook line is strategy prose that would only bias it.
            turn_context, self._opening_turn = split_narrator_steers(
                opening_user_content
            )
            self._turn_context = turn_context
            self._turn_book_move = book_move_uci
            self._turn_openings = {opening.name: opening} if opening else {}
            self._turn_game_id = game_id
            self._verifier_max_rounds = verifier_max_rounds
            # OR'd true by any delegate whose verifier sub-run hits its round
            # cap this turn; surfaced once on the done event (gear note).
            self._verifier_round_cap_hit = False
            self._seq = 0
            self._replay_buffer = []
            self._turn_usage = None
            # New turn -> the live position has moved; stale searches must
            # not satisfy this turn's requests.
            if self._search_cache is not None:
                self._search_cache.clear()
            messages: list[Message] = [{"role": "user", "content": opening_user_content}]
            tool_schemas = self._registry.schemas() or None
            has_recommend_move = any(
                t.get("name") == RECOMMEND_MOVE_TOOL_NAME
                for t in (tool_schemas or [])
            )
            has_delegate = any(
                t.get("name") == _DELEGATE_TOOL_NAME
                for t in (tool_schemas or [])
            )
            done_payload: dict = {"done": True}
            async with open_transcript() as transcript:
                await transcript.turn_start({
                    "mode": mode,
                    "game_id": game_id,
                    "provider": type(active).__name__,
                    "tools": [t.get("name") for t in tool_schemas] if tool_schemas else [],
                })
                await transcript.system_prompt(system_prompt)
                await transcript.user_message(opening_user_content)
                config = _LoopConfig(
                    mode=mode,
                    registry=self._registry,
                    system_prompt=system_prompt,
                    tool_schemas=tool_schemas,
                    max_rounds=max_tool_rounds,
                    emit=self._emit,
                    game_id=game_id,
                    provider=active,
                    transcript=transcript,
                    completeness_nudge=(
                        _RECOMMEND_NUDGE_PROMPTS[mode] if has_recommend_move else None
                    ),
                    track_recommend=has_recommend_move,
                    enforce_red_team=has_recommend_move and has_delegate,
                    book_move_uci=book_move_uci,
                    link_openings=True,
                )
                recommended_uci: str | None = None
                recommended_depth: int | None = None
                try:
                    result = await self._run_loop(messages, config)
                    recommended_uci = result.recommended_uci
                    recommended_depth = result.recommended_depth
                    # A delegate's verifier sub-run that capped out this turn
                    # never produced a verdict; surface the gear note so the
                    # user can raise the verifier-rounds setting.
                    if self._verifier_round_cap_hit:
                        done_payload["verifier_round_cap"] = True
                    if result.round_cap_hit:
                        # Loop hit the guardrail, not a natural answer;
                        # lets the UI surface "stopped early; raise the
                        # cap in Settings" if it wants to.
                        done_payload["round_cap"] = True
                        log.warning(
                            "AI agent loop hit round cap (%d); raise the Max tool rounds setting if intentional",
                            max_tool_rounds,
                        )
                    elif not result.text_published:
                        # Model exited the loop with zero user-facing
                        # text (reasoning-only models, refusals).
                        done_payload["no_response"] = True
                    elif has_recommend_move and recommended_uci is None:
                        # Naturally ended without any move despite the repeated
                        # nudge -- distinct from a round-cap so the UI says "no
                        # move chosen", not "raise the cap".
                        done_payload["no_recommendation"] = True
                        # Server count: the client can't see silent rounds.
                        done_payload["rounds"] = result.rounds
                        log.info("AI turn ended with no accepted recommend_move")
                except asyncio.CancelledError:
                    done_payload["cancelled"] = True
                    raise
                except Exception as exc:
                    # log.error (not exception): trace is noise for
                    # provider rejections; full detail is in transcript.
                    log.error("AI agent loop failed: %s: %s", type(exc).__name__, exc)
                    done_payload["error"] = type(exc).__name__
                    done_payload["error_detail"] = str(exc)[:ERROR_DETAIL_MAX_LEN]
                    raise
                finally:
                    if (
                        recommended_uci is not None
                        and not done_payload.get("cancelled")
                        and self._recommend_verifier is not None
                        and self._cancel_token is not None
                    ):
                        try:
                            payload = await self._recommendation_payload(
                                recommended_uci, recommended_depth,
                                book_move_uci, book_alternatives,
                            )
                            if payload is not None:
                                await self._emit(
                                    Event(
                                        kind=EVT_AI_RECOMMENDATION,
                                        game_id=game_id,
                                        payload=payload,
                                    )
                                )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            log.warning(
                                "recommendation verification failed for %s: %s: %s",
                                recommended_uci, type(exc).__name__, exc,
                            )
                    # Final totals ride the done event too, so a client that
                    # missed the per-round ai_usage stream (or replays only
                    # the terminal event) still renders the turn's cost.
                    if self._turn_usage is not None:
                        done_payload["usage"] = dict(self._turn_usage)
                        if active.provider_name:
                            done_payload["provider"] = active.provider_name
                    await transcript.turn_end(done_payload)
                    await self._emit(
                        Event(
                            kind=EVT_AI_INFO,
                            game_id=game_id,
                            payload=done_payload,
                        )
                    )
                    # An errored turn tears its panel down client-side; the
                    # buffered done event would only re-fire its error toast
                    # on rehydrate. Drop it.
                    if done_payload.get("error"):
                        self.clear_replay()
                    self._task = None
                    self._cancel_token = None
                    self._active_provider = None
                    self._turn_context = ""
                    self._opening_turn = False
                    self._turn_book_move = None
                    self._turn_openings = {}
                    self._turn_game_id = None
                    self._active_delegate_id = None

    async def _run_loop(
        self, messages: list[Message], config: _LoopConfig,
    ) -> _LoopResult:
        """Shared multi-round agent loop. Drives provider rounds, streams
        text/thinking via `config.emit`, and dispatches tool calls (with
        single-slot dedup + lazy card injection). Returns a `_LoopResult`;
        the caller owns transcript framing, recommend verification, and the
        terminal done event.

        Used by both the narrator turn (`run`, real emit) and verifier
        sub-runs (`_run_verifier`, silent emit). The two differ only via
        `config`."""
        st = _LoopState()
        for round_index in range(config.max_rounds):
            st.rounds = round_index + 1
            rnd = await self._stream_round(messages, config, st, round_index)
            correction = await self._check_round_prose(config, st, rnd)
            if rnd.pending_tool is not None:
                await self._dispatch_round_tool(messages, config, st, rnd, correction)
                continue
            if correction is not None:
                # A mismatch blocks natural exit: append the round's prose and
                # inject the corrective so the model self-corrects next round.
                _inject_nudge(messages, rnd.chunks, correction)
                continue
            if await self._handle_natural_exit(messages, config, st, rnd):
                break
        # On round-cap the narrator falls back to all-rounds text (consumer
        # is recommended_uci). The verifier must NOT -- its prose IS the
        # verdict; cross-round self-talk would poison it. Empty -> no verdict.
        final = st.final_text or ("".join(st.text_parts) if config.track_recommend else "")
        return _LoopResult(
            final_text=final,
            recommended_uci=st.recommended_uci,
            recommended_depth=st.recommended_depth,
            round_cap_hit=st.round_cap_hit,
            text_published=st.text_published,
            rounds=st.rounds,
        )

    async def _stream_round(
        self,
        messages: list[Message],
        config: _LoopConfig,
        st: _LoopState,
        round_index: int,
    ) -> _Round:
        """Run one provider call, streaming its text and thinking onto the
        bus. A tool_use chunk ends the round and is returned as
        `pending_tool`."""
        emit = config.emit
        game_id = config.game_id
        rnd = _Round(index=round_index)
        force_tool = st.force_tool_next or (
            config.force_first_round_tool and round_index == 0
        )
        st.force_tool_next = False
        provider_stream = config.provider.stream(
            system=config.system_prompt,
            messages=messages,
            tools=config.tool_schemas,
            transcript=config.transcript,
            round_index=round_index,
            thinking=config.thinking_override,
            force_tool_call=force_tool,
        )
        # Strip paired markdown (**, __, `) so the panel renders clean
        # prose rather than raw emphasis markers.
        async for chunk in strip_markdown_stream(provider_stream):
            rnd.chunks.append(chunk)
            await config.transcript.chunk(round_index, chunk)
            if chunk.kind == "text" and chunk.text:
                # Non-whitespace only: a whitespace-only turn produced no
                # real answer, so it reads as no_response, not a (missing)
                # recommendation. Matches _round_produced_output's strip.
                if chunk.text.strip():
                    st.text_published = True
                st.text_parts.append(chunk.text)
                payload = {"delta": chunk.text, "round": round_index}
                rnd.think_timer.spend_into(payload)
                await emit(
                    Event(kind=EVT_AI_INFO, game_id=game_id, payload=payload)
                )
            elif chunk.kind == "thinking" and chunk.text:
                rnd.think_timer.start()
                await emit(
                    Event(
                        kind=EVT_AI_THINKING,
                        game_id=game_id,
                        payload={"delta": chunk.text, "round": round_index},
                    )
                )
            elif chunk.kind == "usage" and chunk.usage is not None:
                await self._add_usage(chunk.usage)
            elif chunk.kind == "tool_use":
                # In sequential mode (v1), a tool_use ends the round;
                # downstream chunks after it would belong to the next
                # round per Anthropic semantics. Capture and break.
                # (Providers order the round's usage chunk before any
                # tool_use, so it isn't lost to this break.)
                rnd.pending_tool = chunk
                break
        return rnd

    async def _check_round_prose(
        self, config: _LoopConfig, st: _LoopState, rnd: _Round,
    ) -> str | None:
        """Single-board prose check, every round. A new hit surfaces a
        self-correction note and returns a fact-anchored corrective to
        inject; a repeat is only struck. A clean round's opening names are
        linked instead. None when nothing needs correcting."""
        emit = config.emit
        game_id = config.game_id
        round_index = rnd.index
        pc, repeat_pc = self._position_check(rnd.chunks).partition(st.corrected_items)
        # Split off flags the prose placed in another position: not struck,
        # but the narrator is asked (once per item) to name such positions.
        # Repeats were already ruled on, so they skip the judge.
        pc, other = await self._apply_semantic_check(pc, rnd.chunks, config, round_index)
        # The ask rides along only when a round follows anyway (a corrective
        # or a pending tool call): a round forced for it would repeat the
        # unstruck prose.
        rephrase = (
            sorted(other - st.rephrase_asked)
            if pc.hit or rnd.pending_tool is not None else []
        )
        st.rephrase_asked.update(rephrase)
        # Tool-mention-only hits carry no surface to strike; skip the UI
        # note (it would mark nothing) but still return the corrective so
        # the model rewrites the sentence. A new leak hides the round's
        # prose (the corrective asks for it again); a repeat draws no retry,
        # so hiding would leave the reader nothing -- strike it.
        surfaces = _sort_surfaces(
            pc.surfaces + repeat_pc.surfaces + repeat_pc.leak_surfaces
        )
        hide_prose = pc.hides_prose
        if surfaces or hide_prose:
            await self._emit_position_note(
                emit=emit, game_id=game_id, round_index=round_index,
                surfaces=surfaces, hide_prose=hide_prose,
            )
        elif config.link_openings:
            await self._emit_opening_links(
                emit=emit, game_id=game_id, round_index=round_index,
                chunks=rnd.chunks,
            )
        if pc.hit:
            st.corrected_items |= pc.keys
        if not (pc.hit or rephrase):
            return None
        return self._position_check_message(pc, rephrase)

    async def _handle_natural_exit(
        self,
        messages: list[Message],
        config: _LoopConfig,
        st: _LoopState,
        rnd: _Round,
    ) -> bool:
        """A round that ended without a tool call. Returns True when the turn
        ends here, False when a nudge was injected for another round.

        Completeness nudge: narrator re-nudges each clean exit until a move
        is accepted (stopping on a stall); verifier nudges once to search
        the move before concluding. Gated on real output: a silent round
        ends the turn (a model that produced nothing won't comply with one
        more prompt) rather than burning rounds re-nudging it. Exception,
        once: the narrator went silent right after a fresh (rejected)
        attempt -- it is still engaged, so force one more try."""
        emit = config.emit
        game_id = config.game_id
        mode = config.mode
        round_chunks = rnd.chunks
        produced = _round_produced_output(round_chunks)
        silent_retry = (
            not produced
            and config.track_recommend
            and not st.silent_renudge_used
            and st.nudge_sent
            and st.recommend_attempts > st.attempts_at_last_nudge
        )
        if self._needs_nudge(
            config, st.nudge_sent, st.recommended_uci, st.move_searched,
            st.recommend_attempts, st.attempts_at_last_nudge,
        ) and (produced or silent_retry):
            st.silent_renudge_used = st.silent_renudge_used or silent_retry
            log.info("completeness nudge (%s): injecting nudge", mode)
            st.nudge_sent = True
            st.attempts_at_last_nudge = st.recommend_attempts
            # Narrator: the nudged round must call a tool, so it can't
            # exit in prose again without attempting a move.
            st.force_tool_next = config.track_recommend
            _inject_nudge(messages, round_chunks, config.completeness_nudge)
            return False
        # Prose in a post-acceptance round is the conclusion we
        # wanted -- record it so the nudge below doesn't fire.
        if st.recommended_uci is not None and rnd.had_text:
            st.prose_after_recommend = True
        # Move accepted but no closing conclusion yet: ask for it
        # once. Small models treat the tool call as the end; this
        # backstops the prompt. Narrator-only (track_recommend).
        if (
            config.track_recommend
            and st.recommended_uci is not None
            and not st.prose_after_recommend
            and not st.post_recommend_nudge_sent
        ):
            # Fires even on a silent round -- that IS the trigger
            # (model treated the accepting call as the end). The
            # empty turn becomes a placeholder so the wire stays valid.
            log.info("post-recommend nudge (%s): asking for conclusion", mode)
            st.post_recommend_nudge_sent = True
            _inject_nudge(
                messages,
                round_chunks,
                _POST_RECOMMEND_NUDGE_PROMPTS[mode].format(
                    san=st.recommended_san or "the move",
                ),
            )
            return False
        # Prose naming a different move than the one recorded: the
        # arrow and the text disagree on screen. Re-prompt once, then
        # strike the offending spans and ship (a stalled model would
        # otherwise burn the round budget re-asserting).
        mismatch = self._recommend_mismatch(
            round_chunks, st.recommended_uci, st.recommended_san,
        )
        if mismatch is not None:
            surfaces, named = mismatch
            if not st.mismatch_nudge_sent:
                log.info(
                    "recommend-mismatch nudge (%s): prose names %s, recorded %s",
                    mode, named, st.recommended_san,
                )
                st.mismatch_nudge_sent = True
                _inject_nudge(
                    messages,
                    round_chunks,
                    _RECOMMEND_MISMATCH_NUDGE.format(
                        san=st.recommended_san, named=named,
                    ),
                )
                return False
            await self._emit_position_note(
                emit=emit, game_id=game_id, round_index=rnd.index,
                surfaces=surfaces,
            )
        # Verdict = this final round's prose only, so cross-round
        # tool-call self-talk ("I need the FEN", "let me check")
        # doesn't leak up to the narrator.
        round_text = "".join(
            c.text for c in round_chunks if c.kind == "text" and c.text
        )
        if (
            config.require_verdict
            and round_text.strip()
            and not _VERDICT_LEAD_RE.match(round_text)
        ):
            if not st.verdict_nudge_sent:
                log.info("verdict nudge (%s): reply is not a verdict", mode)
                st.verdict_nudge_sent = True
                _inject_nudge(messages, round_chunks, _VERIFIER_VERDICT_NUDGE)
                return False
            log.warning("verifier reply is not a verdict; dropped: %r", round_text)
            round_text = ""
        # Past the one search nudge: a verdict on a move never searched
        # is invented, so it is dropped rather than passed on.
        if (
            config.move_under_test is not None
            and round_text.strip()
            and not st.move_searched
        ):
            log.warning(
                "verifier verdict without a search of %s; dropped: %r",
                config.move_under_test.uci(), round_text,
            )
            round_text = ""
        st.round_cap_hit = False
        await _flush_think(emit, rnd.think_timer, game_id, rnd.index)
        st.final_text = round_text
        return True

    async def _dispatch_round_tool(
        self,
        messages: list[Message],
        config: _LoopConfig,
        st: _LoopState,
        rnd: _Round,
        correction: str | None,
    ) -> None:
        """Run the round's tool call and append its result (plus any
        `correction` and failure nudge) for the next round."""
        emit = config.emit
        game_id = config.game_id
        mode = config.mode
        round_index = rnd.index
        pending_tool = rnd.pending_tool
        messages.append(_assistant_message(rnd.chunks))
        # Single-slot dedup. Cache hit returns the prior result
        # without re-dispatching and without a duplicate UI dot.
        key = self._dedup_key(pending_tool)
        cache_hit = (
            key is not None
            and st.last_call is not None
            and st.last_call[0] == key
        )
        if cache_hit:
            log.info("tool dedup hit: %s", pending_tool.tool_name)
            tool_output = st.last_call[1]
        else:
            # Surface the call to the UI only on a real dispatch.
            tool_payload = {
                "round": round_index,
                "name": pending_tool.tool_name,
                "input": pending_tool.tool_input,
                "tool_use_id": pending_tool.tool_use_id,
            }
            rnd.think_timer.spend_into(tool_payload)
            await emit(
                Event(
                    kind=EVT_AI_TOOL_CALL,
                    game_id=game_id,
                    payload=tool_payload,
                )
            )
            # Tag the verifier's nested events with this delegate's
            # id (client nesting); cleared after so a later narrator
            # call isn't misattributed.
            is_delegate = pending_tool.tool_name == _DELEGATE_TOOL_NAME
            if is_delegate:
                self._active_delegate_id = pending_tool.tool_use_id
            reporter = _progress_reporter(
                emit, game_id, round_index, pending_tool.tool_use_id,
            )
            try:
                with reporting_progress(reporter):
                    tool_output = await self._dispatch_tool(
                        pending_tool, registry=config.registry,
                    )
            finally:
                if is_delegate:
                    self._active_delegate_id = None
            if key is not None:
                # Cache success and error alike (deterministic rejection
                # is as redundant as success) -- and always the raw
                # result: the red-team hold below is per-call policy.
                st.last_call = (key, tool_output)
        # Hold the first un-red-teamed accept (one-shot; book move
        # exempt) BEFORE any event/transcript sees the output, so the
        # UI, the transcript, and the model all read the same result.
        if (
            config.track_recommend
            and pending_tool.tool_name == RECOMMEND_MOVE_TOOL_NAME
            and config.enforce_red_team
            and not st.red_teamed
            and not st.red_team_nudge_sent
            and _is_accepted_recommend(tool_output)
            and tool_output["uci"] != config.book_move_uci
        ):
            st.red_team_nudge_sent = True
            tool_output = {
                k: v for k, v in tool_output.items() if k != "ok"
            }
            tool_output["error"] = _RED_TEAM_FIRST_ERROR
            tool_output["reason"] = _RED_TEAM_FIRST_NUDGE
            log.info("red-team nudge (%s): holding unchecked accept", mode)
        if not cache_hit:
            await emit(
                Event(
                    kind=EVT_AI_TOOL_CALL_COMPLETE,
                    game_id=game_id,
                    payload={
                        "round": round_index,
                        "name": pending_tool.tool_name,
                        "tool_use_id": pending_tool.tool_use_id,
                        # Surfaced in the panel's tool-call OUT block.
                        "output": tool_output,
                    },
                )
            )
        # A *successful* top_moves call resets the failure streak and
        # re-arms the nudge. An errored call (malformed args, empty list)
        # doesn't count, or repeated bad calls would never escalate.
        if (
            config.track_recommend
            and pending_tool.tool_name == TOP_MOVES_TOOL_NAME
            and isinstance(tool_output, dict)
            and not tool_output.get("error")
        ):
            st.consecutive_recommend_failures = 0
            st.recommend_failure_nudge_armed = True
        # A verdict-bearing delegate marks the pick red-teamed, and so does
        # a refutation the engine overruled (the move survived); other
        # errors (bad move, no verdict) don't count.
        if (
            config.track_recommend
            and pending_tool.tool_name == _DELEGATE_TOOL_NAME
            and isinstance(tool_output, dict)
            and (not tool_output.get("error") or tool_output.get(MOVE_SURVIVED_KEY))
        ):
            st.red_teamed = True
        if _ranks_move(pending_tool, tool_output, config.move_under_test):
            st.move_searched = True
        # An accepted recommend_move is the turn's pick. Safe to read a
        # cached result: the cached uci matches a fresh dispatch's.
        if config.track_recommend and pending_tool.tool_name == RECOMMEND_MOVE_TOOL_NAME:
            st.recommend_attempts += 1
            accepted = _is_accepted_recommend(tool_output)
            if accepted:
                st.consecutive_recommend_failures = 0
                st.recommended_uci = tool_output["uci"]
                st.recommended_depth = tool_output.get("depth")
                st.recommended_san = tool_output.get("san")
                if st.recommended_uci == config.book_move_uci:
                    log.info("book move accepted (%s): %s", mode, st.recommended_uci)
                # A conclusion alongside the accepting call counts -- no
                # separate post-move round needed. Prose in a later round
                # is handled at the natural-exit check.
                if rnd.had_text:
                    st.prose_after_recommend = True
            elif tool_output.get("error") != _RED_TEAM_FIRST_ERROR:
                st.consecutive_recommend_failures += 1
        await config.transcript.tool_result(
            round_index, pending_tool.tool_use_id, tool_output
        )
        # A survived envelope (overruled refutation) carries an error for
        # the model but isn't a failure: the move passed. The panel badges
        # it from the complete event instead.
        if (
            isinstance(tool_output, dict)
            and tool_output.get("error")
            and not tool_output.get(MOVE_SURVIVED_KEY)
        ):
            await emit(
                Event(
                    kind=EVT_AI_TOOL_CALL_FAILED,
                    game_id=game_id,
                    payload={
                        "round": round_index,
                        "tool_use_id": pending_tool.tool_use_id,
                        "error": tool_output.get("error"),
                        "detail": tool_output.get("detail"),
                    },
                )
            )
        card = self._inject_card_once(
            pending_tool.tool_name, st.cards_injected, registry=config.registry,
        )
        messages.append(
            _tool_result_message(
                pending_tool.tool_use_id, tool_output, card=card,
            )
        )
        # Correct a mismatch in this round's prose. After the tool_result
        # so the assistant tool_use is paired before this user message.
        if correction is not None:
            messages.append({"role": "user", "content": correction})
        # Force a top_moves call after enough failed recommend attempts.
        # After the tool_result (every tool_use needs a matching result
        # before a user-role nudge). One-shot until a top_moves re-arms it.
        if (
            st.recommend_failure_nudge_armed
            and st.consecutive_recommend_failures >= MAX_RECOMMEND_FAILURES
        ):
            log.info(
                "recommend-failure nudge (%s): forcing top_moves after %d failures",
                mode, st.consecutive_recommend_failures,
            )
            st.recommend_failure_nudge_armed = False
            messages.append(
                {"role": "user", "content": _RECOMMEND_FAILURE_NUDGE}
            )
        await _flush_think(emit, rnd.think_timer, game_id, round_index)

    def _position_check(
        self, chunks: list[ProviderChunk],
    ) -> _PositionCheck:
        """Run the single-board checks on a round's assembled prose, capturing
        each flag's prose surface (for striking) plus its normalized label.
        Empty when no board_provider, no live board, or no prose."""
        board = self._board_provider() if self._board_provider else None
        if board is None:
            return _PositionCheck(None, [], [], [])
        full_text = "".join(
            c.text for c in chunks if c.kind == "text" and c.text
        )
        # Tool mentions are a style violation, wrong anywhere -- scanned on the
        # full prose, not the truncated board view, so a leak after a future
        # line still counts.
        tool_mentions = find_tool_mentions(full_text)
        tool_leaks = list(iter_tool_label_leaks(full_text, self._leak_names()))
        # Opening turns discuss off-board alternatives (sibling variations,
        # earlier-ply moves), so the board-legality/claim recognizers misfire.
        # Keep only the board-independent tool guards.
        if self._opening_turn:
            return _PositionCheck(
                board, [], [], [], tool_mentions, tool_leaks=tool_leaks,
            )
        # Stop at the first move number past the live ply: beyond it the model
        # is in a hypothetical line, not describing the board.
        text = truncate_at_future_line(full_text, board)
        if not text.strip():
            return _PositionCheck(
                board, [], [], [], tool_mentions, tool_leaks=tool_leaks,
            )
        # SAN moves ('Bxe4') and prose moves ('bishop to a1') are the same
        # kind of error -- an impossible move -- so they share one bucket.
        # One shared dedup set across the move recognizers: a move flagged by
        # an earlier one (keyed by from+to uci, or label) is skipped by later
        # ones, so the same move is never struck twice via different phrasings.
        # The line check owns numbered continuation runs it can anchor: the
        # SAN-token recognizer skips moves inside those spans, so a numbered
        # pair's unmarked Black reply ('24.Nf6+ Qxf6') is neither re-checked
        # from the wrong POV (cleared line) nor struck twice (broken line).
        # Only the bare-token recognizer reads inside a run, so only it takes
        # the spans.
        handled_spans = handled_continuation_spans(text, board)
        seen_moves: set[str] = set()
        move_pairs = (
            list(iter_illegal_moves(text, board, seen_moves, handled_spans))
            + list(iter_illegal_piece_moves(text, board, seen_moves))
            + list(iter_illegal_square_moves(text, board, seen_moves))
            + list(iter_illegal_pawn_moves(text, board, seen_moves))
        )
        line_pairs = [
            (surface, label)
            for surface, label, _span in iter_illegal_continuations(text, board)
        ]
        return _PositionCheck(
            board,
            move_pairs,
            list(iter_false_claim_squares(text, board)),
            line_pairs,
            tool_mentions,
            list(iter_false_bishop_color_refs(text, board))
            + list(iter_false_file_claims(text, board))
            + list(iter_false_file_openness(text, board))
            + list(iter_false_tactic_claims(text, board))
            + list(iter_false_illegality_claims(text, board))
            + list(iter_false_occupancy_claims(text, board)),
            tool_leaks,
        )

    def _leak_names(self) -> list[str]:
        """Tool names and result keys the prose must never carry."""
        names = self._registry.names() + [_VERDICT_KEY]
        if self._verifier_registry is not None:
            names += self._verifier_registry.names()
        return list(dict.fromkeys(names))

    def _recommend_mismatch(
        self,
        chunks: list[ProviderChunk],
        recommended_uci: str | None,
        recommended_san: str | None,
    ) -> tuple[list[str], str] | None:
        """Detect closing prose that describes a move other than the recorded
        one -- the bug where the arrow shows Qg3 and the text explains Ne3.
        Returns (surfaces to strike, first named move) or None when clean.

        Only fires when the recorded move is absent from the prose entirely:
        naming it alongside a rejected alternative ("Qg3 is stronger than
        Ne3") is legitimate comparison, not a mismatch."""
        board = self._board_provider() if self._board_provider else None
        if board is None or not recommended_uci or not recommended_san:
            return None
        text = "".join(c.text for c in chunks if c.kind == "text" and c.text)
        if not text.strip():
            return None
        named: list[tuple[str, str]] = []
        for surface, bare, move in iter_stm_moves(text, board):
            if move.uci() == recommended_uci:
                return None
            named.append((surface, bare))
        if not named:
            return None
        return [surface for surface, _bare in named], named[0][1]

    async def _apply_semantic_check(
        self,
        pc: _PositionCheck,
        chunks: list[ProviderChunk],
        config: _LoopConfig,
        round_index: int,
    ) -> tuple[_PositionCheck, set[str]]:
        """Split off the flags the model judges the prose to place in another
        position: returns (pc without them, their labels). No-op when the flag
        is off or nothing is judgeable. The round trip shows in the panel's
        tool list as a call/result pair."""
        if not SEMANTIC_CHECK_ENABLED or pc.board is None:
            return pc, set()
        items = pc.judge_items
        if not items:
            return pc, set()
        labels = [label for label, _fact in items]
        prose = "".join(c.text for c in chunks if c.kind == "text" and c.text)
        # Delegate prefix keeps a verifier sub-run's id distinct from the
        # narrator's for the same round index.
        scope = f"{self._active_delegate_id}-" if self._active_delegate_id else ""
        call_id = f"{scope}{POSITION_JUDGE_CALL_NAME}-{round_index}"
        await config.emit(Event(
            kind=EVT_AI_TOOL_CALL,
            game_id=config.game_id,
            payload={
                "round": round_index,
                "name": POSITION_JUDGE_CALL_NAME,
                "input": {"labels": labels},
                "tool_use_id": call_id,
            },
        ))
        other = await judge_other_position(config.provider, pc.board, prose, items)
        await config.emit(Event(
            kind=EVT_AI_TOOL_CALL_COMPLETE,
            game_id=config.game_id,
            payload={
                "round": round_index,
                "name": POSITION_JUDGE_CALL_NAME,
                "tool_use_id": call_id,
                "output": _judge_summary(labels, other),
            },
        ))
        return pc.without_labels(other), other

    async def _emit_position_note(
        self,
        *,
        emit: EmitSink,
        game_id: str | None,
        round_index: int,
        surfaces: list[str],
        hide_prose: bool = False,
    ) -> None:
        """Surface a position-check hit to the UI. `surfaces` are the exact
        prose spans the client strikes in the round's folded prose;
        `hide_prose` (a tool leak) hides that prose from the reader instead."""
        log.info(
            "position check round %d: surfaces=%s hide_prose=%s",
            round_index, surfaces, hide_prose,
        )
        payload: dict = {"round": round_index, "surfaces": surfaces}
        if hide_prose:
            payload["hide_prose"] = True
        await emit(Event(kind=EVT_AI_POSITION_NOTE, game_id=game_id, payload=payload))

    async def _emit_opening_links(
        self,
        *,
        emit: EmitSink,
        game_id: str | None,
        round_index: int,
        chunks: list[ProviderChunk],
    ) -> None:
        """Link the turn's openings where a clean round's prose names them:
        each item is the exact prose `surface` plus the opening's `uci` line
        from the start position, which the client plays on double-click. A
        null `uci` marks a span to leave plain: a name that is not ours to
        link but contains a linked surface."""
        prose = "".join(c.text for c in chunks if c.kind == "text" and c.text)
        book = self._book_provider() if self._book_provider is not None else None
        # One item per surface: the client wraps every occurrence of it.
        names = dict(find_opening_names(prose, self._turn_openings.values(), book))
        linked = [surface for surface, opening in names.items() if opening is not None]
        if not linked:
            return
        items = [
            {"surface": surface, "uci": list(opening.moves) if opening else None}
            for surface, opening in names.items()
            if opening is not None or any(s in surface for s in linked)
        ]
        await emit(Event(
            kind=EVT_AI_OPENING_LINKS, game_id=game_id,
            payload={"round": round_index, "items": items},
        ))

    @staticmethod
    def _position_check_message(pc: _PositionCheck, rephrase: Sequence[str] = ()) -> str:
        """Fact-anchored correction with separate asks per error type. Illegal
        moves/lines get the "..."/move-number outs (they may be another side or
        ply); false piece claims state the square's real content and ask only
        for a restate. `rephrase` labels (judged to mean another position) are
        asked to name that position."""
        clauses: list[str] = []
        # Dedup: a move named both in a broken line and standalone in the prose
        # would otherwise repeat its fact. Order-preserving via dict.fromkeys.
        move_facts = [
            _ILLEGAL_MOVE_FACT.format(move=move)
            for move in dict.fromkeys(pc.move_labels + pc.line_labels)
        ]
        if move_facts:
            clauses.append(_POSITION_CHECK_MOVE_CLAUSE.format(facts="; ".join(move_facts)))
        # Square claims and fact claims are all plain board truth -- one
        # "restate" clause, facts joined. Fact correctives are precomputed
        # (see their recognizers).
        claim_facts = (
            [describe_square(square, pc.board) for _surface, _label, square in pc.claim_triples]
            + [fact for _surface, _label, fact in pc.fact_triples]
        )
        if claim_facts:
            clauses.append(_POSITION_CHECK_CLAIM_CLAUSE.format(facts="; ".join(claim_facts)))
        if pc.tool_mentions:
            clauses.append(_POSITION_CHECK_TOOL_CLAUSE.format(mentions=_quote_join(pc.tool_mentions)))
        if pc.tool_leaks:
            clauses.append(_POSITION_CHECK_LEAK_CLAUSE.format(leaks=_quote_join(pc.leak_surfaces)))
        if rephrase:
            clauses.append(_POSITION_CHECK_REPHRASE_CLAUSE.format(items=_quote_join(rephrase)))
        return _POSITION_CHECK_PREFIX + " ".join([_POSITION_CHECK_LEAD, *clauses])

    @staticmethod
    def _needs_nudge(
        config: _LoopConfig,
        nudge_sent: bool,
        recommended_uci: str | None,
        move_searched: bool,
        recommend_attempts: int,
        attempts_at_last_nudge: int,
    ) -> bool:
        """Completeness nudge decision. Narrator re-nudges toward an
        accepted recommend_move on every clean exit until one lands,
        stopping only when a prior nudge drew no new attempt (the model is
        ignoring it -- round_cap is the remaining backstop). Verifier nudge
        stays one-shot: search the move under test before concluding."""
        if config.completeness_nudge is None:
            return False
        if config.track_recommend:
            if recommended_uci is not None:
                return False
            # First nudge always allowed; re-nudge only if the last one
            # produced a fresh attempt (else we'd loop on a stuck model).
            if not nudge_sent:
                return True
            return recommend_attempts > attempts_at_last_nudge
        return not nudge_sent and not move_searched

    def note_openings(self, openings: Iterable[Opening]) -> None:
        """Record openings a tool put in front of the narrator this turn
        (related_openings' `on_shown`), so prose naming them is linked."""
        for opening in openings:
            self._turn_openings.setdefault(opening.name, opening)

    def turn_book_move(self) -> str | None:
        """The in-flight turn's book move (UCI), for recommend_move's
        book_move_provider; None between turns or off book."""
        return self._turn_book_move

    async def _recommendation_payload(
        self,
        uci: str,
        depth: int | None,
        book_move_uci: str | None,
        book_alternatives: tuple[str, ...],
    ) -> dict | None:
        """The `ai_recommendation` payload for the turn's accepted pick. The
        book move ships unsearched -- theory needs no engine check -- with
        its siblings for the board (arrows for siblings of a pick the model
        rejected would mislead); any other pick goes through the verifier."""
        move = chess.Move.from_uci(uci)
        board = self._board_provider() if self._board_provider else None
        if uci != book_move_uci:
            payload = await self._recommend_verifier(move, depth, self._cancel_token)
        elif board is None or move not in board.legal_moves:
            return None
        else:
            payload = {"uci": uci, "san": board.san(move)}
            if book_alternatives:
                payload["alternatives"] = list(book_alternatives)
        # The position the pick belongs to. The client's arrow re-apply
        # guard keys on it -- its own board fen is unsynced mid-replay.
        if payload is not None and board is not None:
            payload["fen"] = board.fen()
        return payload

    def delegate_runner(self) -> VerifierRunner:
        """The verifier sub-run callable to hand `make_delegate_tool`.
        Public seam so wiring (app.py) doesn't reach into a private
        method to build the narrator's `delegate` tool."""
        return self._run_verifier

    async def _run_verifier(self, question: str, move: chess.Move) -> VerifierResult:
        """Run one verifier sub-run for the narrator's `delegate` call on
        `move` (parsed against the live board).

        Forwards only its tool-call events to the UI (via `_verifier_emit`,
        nested under the delegate row) and suppresses its prose/thinking;
        the verdict is returned, not streamed. Uses the verifier registry
        (engine tools, no `delegate` -- one level deep, no recursion) and
        the verifier prompt. Returns the verdict prose (empty when none
        survived the gates) and whether the round cap cut the run short.

        Runs inline within the narrator's turn, sharing its cancel token
        (self._cancel_token); no lock re-entry. Opens its own transcript
        turn for traceability.
        """
        if self._verifier_registry is None:
            return VerifierResult("")
        provider = self._active_provider or self._provider
        system_prompt = assemble_system_prompt(
            _VERIFIER_MODE, tools=self._verifier_registry.specs()
        )
        tool_schemas = self._verifier_registry.schemas() or None
        # Ground the verifier in the same position the narrator sees;
        # without it the model has no FEN/side-to-move and can't form a
        # tool call, so it begs for the position instead of verifying.
        if self._turn_context:
            user_content = (
                f"{self._turn_context}\n{_VERIFIER_QUESTION_LABEL} {question}"
            )
        else:
            user_content = question
        messages: list[Message] = [{"role": "user", "content": user_content}]
        async with open_transcript() as transcript:
            await transcript.turn_start({
                "mode": _VERIFIER_MODE,
                "game_id": None,
                "provider": type(provider).__name__,
                "tools": [t.get("name") for t in tool_schemas] if tool_schemas else [],
                "delegated_question": question,
            })
            await transcript.system_prompt(system_prompt)
            await transcript.user_message(user_content)
            config = _LoopConfig(
                mode=_VERIFIER_MODE,
                registry=self._verifier_registry,
                system_prompt=system_prompt,
                tool_schemas=tool_schemas,
                max_rounds=self._verifier_max_rounds,
                emit=self._verifier_emit,
                game_id=self._turn_game_id,
                provider=provider,
                transcript=transcript,
                completeness_nudge=_VERIFIER_SEARCH_NUDGE,
                track_recommend=False,
                # Engine does the reasoning; model thinking only adds
                # latency (x fan-out) and risks Ollama <think> in verdicts.
                thinking_override=False,
                force_first_round_tool=True,
                require_verdict=True,
                move_under_test=move,
            )
            # done_payload feeds the transcript turn_end only -- a verifier
            # sub-run emits no user-facing done event (it's internal).
            done_payload: dict = {"done": True}
            try:
                result = await self._run_loop(messages, config)
                if result.round_cap_hit:
                    done_payload["round_cap"] = True
                    # Flag the turn so the done event shows the gear note for
                    # the verifier-rounds setting. final_text is "" on a cap
                    # (delegate maps that to no_verdict); warn for diagnosis.
                    self._verifier_round_cap_hit = True
                    log.warning(
                        "verifier sub-run hit round cap (%d) without a verdict; question=%r",
                        self._verifier_max_rounds, question,
                    )
                return VerifierResult(result.final_text, result.round_cap_hit)
            except Exception as exc:
                done_payload["error"] = type(exc).__name__
                done_payload["error_detail"] = str(exc)[:ERROR_DETAIL_MAX_LEN]
                raise
            finally:
                await transcript.turn_end(done_payload)

    async def _add_usage(self, usage: ProviderUsage) -> None:
        """Fold one round's usage into the turn totals and publish the
        cumulative ai_usage event. Called from narrator and verifier
        loops alike -- delegate fan-out is real cost, so it counts toward
        the same turn. Emits via `_emit` directly (not the loop's sink):
        the event is turn-scoped, and the verifier's filtering sink
        would drop it."""
        totals = self._turn_usage
        if totals is None:
            totals = self._turn_usage = {key: 0 for key in _USAGE_TOTAL_KEYS}
        for key in _USAGE_TOTAL_KEYS:
            totals[key] += getattr(usage, key)
        payload = dict(totals)
        # Carried so the client can apply provider-specific billing
        # weights (eff-in). Empty for test doubles -> key omitted.
        name = self._provider_wire_name()
        if name:
            payload["provider"] = name
        await self._emit(
            Event(
                kind=EVT_AI_USAGE,
                game_id=self._turn_game_id,
                payload=payload,
            )
        )

    def _provider_wire_name(self) -> str:
        provider = self._active_provider
        return provider.provider_name if provider is not None else ""

    async def _verifier_emit(self, event: Event) -> None:
        """Emit sink for verifier sub-runs. Forwards engine-search tool
        events (so the user sees progress under the originating "Verifying
        line" row) and drops the verifier's prose/thinking. Each forwarded
        event is stamped with the active delegate's tool_use_id (for
        client-side nesting) and the turn's game_id (for session muxing)."""
        if event.kind not in _VERIFIER_FORWARDED_KINDS:
            return
        # Safe to mutate in place: _run_loop builds a fresh Event per emit.
        # setdefault: a progress step already names its own parent (the
        # verifier's tool row, itself nested under the delegate).
        event.payload.setdefault("parent_tool_use_id", self._active_delegate_id)
        if event.game_id is None:
            event.game_id = self._turn_game_id
        await self._emit(event)

    async def _emit(self, event: Event) -> None:
        self._seq += 1
        event.payload["seq"] = self._seq
        # Shallow-copy the payload so a downstream subscriber that
        # mutates what it receives can't retroactively change replay.
        self._replay_buffer.append({
            ENVELOPE_KIND: event.kind,
            ENVELOPE_PAYLOAD: dict(event.payload),
            ENVELOPE_GAME_ID: event.game_id,
        })
        await self._bus.publish(event)

    def replay(self) -> list[dict]:
        return list(self._replay_buffer)

    def clear_replay(self) -> None:
        # Rebind (not .clear()) so an in-flight replay() iteration on
        # the old list stays consistent.
        self._replay_buffer = []

    def seed_replay(self, events: list[dict]) -> None:
        # Test-support: prime the buffer so a page load rehydrates the panel
        # exactly as a client reconnecting post-turn would. Normalizes to the
        # same envelope shape _emit() produces.
        for ev in events:
            self._replay_buffer.append({
                ENVELOPE_KIND: ev[ENVELOPE_KIND],
                ENVELOPE_PAYLOAD: dict(ev.get(ENVELOPE_PAYLOAD) or {}),
                ENVELOPE_GAME_ID: ev.get(ENVELOPE_GAME_ID),
            })

    def _inject_card_once(
        self, tool_name: str, injected: set[str], *, registry: ToolRegistry,
    ) -> str | None:
        """Return the tool's card on first call this turn, else None.
        Unknown tool names yield None (no card to inject). Mutates
        `injected` to record the first-use moment."""
        if tool_name in injected:
            return None
        try:
            spec = registry.spec(tool_name)
        except UnknownToolError:
            return None
        injected.add(tool_name)
        return spec.card

    def _dedup_key(self, call: ProviderChunk) -> tuple | None:
        """Compute the dedup cache key for a tool call, or None when
        the call can't be normalized (no normalizer / malformed args /
        no live board)."""
        normalizer = _NORMALIZERS.get(call.tool_name)
        if normalizer is None:
            return None
        board = self._board_provider() if self._board_provider else None
        norm = normalizer(call.tool_input, board)
        if norm is None:
            return None
        return (call.tool_name, norm)

    async def _dispatch_tool(
        self, call: ProviderChunk, *, registry: ToolRegistry,
    ) -> dict:
        """Look up + invoke a tool. Unparseable arguments, unknown name, or
        tool-raised exceptions produce structured error results instead of
        breaking the loop -- the model reads the error and recovers (or
        gives up gracefully)."""
        if call.tool_input_error is not None:
            # The provider couldn't parse this call's arguments (e.g. two
            # concatenated JSON objects). Don't invoke the tool with empty
            # input; hand the model the failure so it re-emits one clean call.
            return {
                "error": "malformed_tool_arguments",
                "name": call.tool_name,
                "detail": call.tool_input_error,
            }
        try:
            fn = registry.get(call.tool_name)
        except UnknownToolError:
            return {"error": "unknown_tool", "name": call.tool_name}
        try:
            return await fn(call.tool_input, cancel_token=self._cancel_token)
        except asyncio.CancelledError:
            # Propagate cancellation; the outer except in run() emits
            # the terminal done/cancelled event.
            raise
        except Exception as exc:
            # Returned to the model as a structured error it recovers from
            # (e.g. a provider 429 on a verifier sub-run), so the stack
            # trace is noise -- log the message only.
            log.error("tool %s raised: %s", call.tool_name, exc)
            return {"error": "tool_failed", "name": call.tool_name, "detail": str(exc)}

    async def cancel(self) -> None:
        """Cancel the running turn (if any). Hard-stop per spec: drops the
        agent loop, surfaces partial prose as-is. Idempotent."""
        token = self._cancel_token
        if token is not None:
            token.cancel()
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await asyncio.shield(task)
        except (asyncio.CancelledError, Exception):
            pass
