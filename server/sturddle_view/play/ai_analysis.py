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
import json
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import chess

from ..env_utils import env_int
from ..events import Event, EventBus
from ..llm import (
    LLMProvider,
    Message,
    PromptMode,
    ProviderChunk,
    ToolRegistry,
    ToolSpec,
    UnknownToolError,
    assemble_system_prompt,
    open_transcript,
    strip_markdown_stream,
)
from ..llm.cancel import CancelToken
from ..llm.response_validator import (
    find_castle_word_violations,
    find_false_piece_claims,
    find_illegal_moves,
)


BoardProvider = Callable[[], chess.Board | None]
# End-of-turn verifier; returns payload for ai_recommendation, or None.
RecommendVerifier = Callable[[chess.Move, int | None, CancelToken], Awaitable[dict | None]]


log = logging.getLogger(__name__)


# Cap on agent loop rounds per turn (spec §Guardrails: "Tool call cap
# per agent turn"). The env override is for ops; UI exposure is pending.
_DEFAULT_MAX_TOOL_ROUNDS = 32
MAX_TOOL_ROUNDS = env_int("SV_AI_MAX_TOOL_ROUNDS", _DEFAULT_MAX_TOOL_ROUNDS)

# Verifier sub-runs get a tighter round budget: one move, a tool call or
# two, a verdict. The env override is for ops.
_DEFAULT_VERIFIER_MAX_ROUNDS = 8
VERIFIER_MAX_ROUNDS = env_int("SV_AI_VERIFIER_MAX_ROUNDS", _DEFAULT_VERIFIER_MAX_ROUNDS)

_COMMENTATOR_MODE: PromptMode = "commentator"
_VERIFIER_MODE: PromptMode = "verifier"


def _boards_for_validation(
    board: chess.Board, mode: PromptMode, *, committed: bool = False,
) -> list[chess.Board]:
    """Build the board sequence the validators consume. Coach mode:
    just [board]. Commentator mode: current plus every prior position
    via repeated pop() -- so prose referencing earlier-game pieces
    isn't flagged. We work on a copy so the caller's board is untouched.

    `committed` collapses the walk to [board] even in commentator mode:
    once a move is recommended, the closing prose is a live-position
    plan, not earlier-game commentary, so a square reused by a later
    piece (pawn left b2, queen now there) must validate against the
    live board -- the walk would excuse "capturing the pawn on b2"
    because a pawn sat there 30 moves ago."""
    if mode != _COMMENTATOR_MODE or committed or not board.move_stack:
        return [board]
    walker = board.copy()
    out: list[chess.Board] = [walker.copy()]
    while walker.move_stack:
        walker.pop()
        out.append(walker.copy())
    return out

# Max length of error_detail copied into the done event. Keeps the
# bus payload small even when a provider returns a wall of HTML / a
# verbose stack trace. Full detail is in the transcript anyway.
ERROR_DETAIL_MAX_LEN = 500

# Prefix marking the corrective as an automated check, not the human --
# it lands in the user-role slot but mislabeling distracts the model.
_CORRECTIVE_PREFIX = "[automated position check] "
# Per-mode corrective templates. View mode validates against earlier
# positions too, so its wording says "current OR any earlier position"
# to avoid rejecting a legitimate hypothetical-variation reference.
_CORRECTIVES = {
    "coach": {
        "illegal": "Illegal in this position: {moves}. Rewrite without these.",
        "false_piece": "Not on the board: {claims}. Rewrite without these.",
        "castle": "No legal castling for either side. Rewrite without recommending it.",
    },
    "commentator": {
        "illegal": (
            "Not legal at the position under review and not played in "
            "this game: {moves}. Rewrite without these (or mark as "
            "hypothetical)."
        ),
        "false_piece": (
            "Not on the board at the position under review, nor at any "
            "earlier position in this game: {claims}. Rewrite without these."
        ),
        "castle": (
            "No legal castling for either side, in the position under review "
            "or any earlier position. Rewrite without recommending it."
        ),
    },
    "verifier": {
        "illegal": "Illegal in the live position: {moves}. Rewrite without these.",
        "false_piece": "Not on the live board: {claims}. Rewrite without these.",
        "castle": "No legal castling for either side. Rewrite without recommending it.",
    },
}
# Sent once at end-of-turn if the model never called recommend_move; a
# completeness nudge, not a position-check rebuttal (no _CORRECTIVE_PREFIX).
# Trailing _NO_ACK_CLAUSE suppresses the "Understood, I'll..." preamble.
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
_POST_RECOMMEND_NUDGE_PROMPTS = {
    "coach": (
        "The move is recorded. State the one-to-two sentence conclusion "
        "now, naming the plan it commits to."
    ),
    "commentator": (
        "The move is recorded. State the one-to-two sentence conclusion "
        "now, naming the plan it reflects."
    ),
}

# Verifier completeness nudge: if it issues a verdict without ever
# calling a tool, force one tool call before it concludes. One-shot per
# sub-run (same loop guard as the recommend nudge) so it can't loop.
_VERIFIER_TOOL_NUDGE = (
    "A verdict needs a tool check first, not intuition." + _NO_ACK_CLAUSE
)

# Label introducing the narrator's question in the verifier's user
# message, below the inherited position context.
_VERIFIER_QUESTION_LABEL = "Question to verify:"


# Delegate tool invokes this: narrator question in, verdict prose out.
# No cancel token -- the sub-run shares the turn's self._cancel_token.
VerifierRunner = Callable[[str], Awaitable[str]]


_DELEGATE_TOOL_NAME = "delegate"


# Declarative, not imperative: a "you do X" instruction invites small
# models to reply "Understood, I will..." as prose. Describing behavior
# removes the thing being acknowledged (same pattern across all cards).
_DELEGATE_TOOL_CARD = (
    "One focused question per call works best -- naming the move and "
    "what to check (\"Is Nxd4 sound, or does it drop material?\"). The "
    "reply is a verdict about the live position, advisory not quotable."
)


DELEGATE_TOOL_SPEC = ToolSpec(
    name=_DELEGATE_TOOL_NAME,
    description=(
        "Hand one move-verification question to the engine-backed "
        "checker. It searches the live position and returns a short "
        "verdict (sound/unsound + reason). Use it to confirm any line "
        "before you commit to it in prose."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "One focused question naming the move to check and "
                    "what matters, e.g. 'Is Nxd4 sound here?'."
                ),
            },
        },
        "required": ["question"],
    },
    card=_DELEGATE_TOOL_CARD,
)


def make_delegate_tool(runner: VerifierRunner) -> Callable:
    """Build the `delegate` tool. Dispatches the narrator's question to a
    verifier sub-run via `runner` and returns its verdict as a tool
    result. Empty/malformed input returns a structured error so the
    narrator can recover."""
    async def delegate(input_: dict, *, cancel_token: CancelToken) -> dict:
        question = input_.get("question")
        if not isinstance(question, str) or not question.strip():
            return {"error": "invalid_input", "detail": "question must be a non-empty string"}
        verdict = await runner(question.strip())
        if not verdict:
            return {"error": "no_verdict", "detail": "verifier returned no conclusion"}
        return {"verdict": verdict}

    return delegate


# Tool-arg normalizers for the dedup cache. Each maps (input, board) ->
# hashable key, or None to skip caching this call.
_SAN_PARSE_ERRORS = (
    chess.InvalidMoveError, chess.IllegalMoveError, chess.AmbiguousMoveError,
)


def _parse_move_canonical(raw: str, board: chess.Board) -> chess.Move | None:
    """Try UCI then SAN. Returns canonical Move or None on failure."""
    candidate = raw.strip()
    if not candidate:
        return None
    try:
        return board.parse_uci(candidate)
    except _SAN_PARSE_ERRORS:
        pass
    try:
        return board.parse_san(candidate)
    except _SAN_PARSE_ERRORS:
        return None


def _norm_move_arg(input_: dict, board: chess.Board | None) -> tuple | None:
    # depth is part of the key for recommend_move; harmless for
    # validate_move which doesn't accept it (always None).
    raw = input_.get("move")
    if not isinstance(raw, str) or board is None:
        return None
    move = _parse_move_canonical(raw, board)
    return ("move", move.uci(), input_.get("depth")) if move else None


def _norm_square_arg(input_: dict, board: chess.Board | None) -> tuple | None:
    raw = input_.get("square")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return ("square", chess.square_name(chess.parse_square(raw.lower())))
    except ValueError:
        return None


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
        move = _parse_move_canonical(r, board)
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


def _norm_analyze(input_: dict, board: chess.Board | None) -> tuple | None:
    # Board(fen).fen() canonicalizes whitespace and drops the move
    # counters -- intentional: eval is position- not history-dependent
    # within a turn, so those positions share a cache key.
    fen = input_.get("fen")
    if not isinstance(fen, str) or not fen.strip():
        return None
    try:
        canonical = chess.Board(fen.strip()).fen()
    except ValueError:
        return None
    return ("analyze", canonical, input_.get("depth"))


_NORMALIZERS: dict[str, Callable[[dict, chess.Board | None], tuple | None]] = {
    "recommend_move": _norm_move_arg,
    "validate_move": _norm_move_arg,
    "piece_at": _norm_square_arg,
    "top_moves": _norm_top_moves,
    "analyze": _norm_analyze,
}


def _assistant_message(chunks: list[ProviderChunk]) -> Message:
    """Reassemble a provider chunk stream into the assistant turn that
    must be appended to messages before sending the next round.

    Anthropic's wire shape requires the assistant content to be a list
    of blocks ({type:"text"|"tool_use", ...}) -- we collapse contiguous
    text deltas into one block and emit a tool_use block per call.
    """
    content: list[dict] = []
    text_buf: list[str] = []
    for c in chunks:
        if c.kind == "text" or c.kind == "thinking":
            text_buf.append(c.text)
        elif c.kind == "tool_use":
            if text_buf:
                content.append({"type": "text", "text": "".join(text_buf)})
                text_buf = []
            content.append({
                "type": "tool_use",
                "id": c.tool_use_id,
                "name": c.tool_name,
                "input": c.tool_input,
            })
    if text_buf:
        content.append({"type": "text", "text": "".join(text_buf)})
    return {"role": "assistant", "content": content}


def _inject_nudge(
    messages: list[Message], round_chunks: list[ProviderChunk], content: str,
) -> None:
    """Append the model's own round prose, then a one-shot user nudge.
    The assistant message must land first so the model sees its analysis
    above the request. Shared by the completeness + post-recommend nudges."""
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
    given tool per turn (see docs/ai-analysis-spec.md §Skills layer).
    """
    if not isinstance(result, str):
        result = json.dumps(result)
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
    # Per-call thinking override passed to provider.stream(). None = use
    # the provider's setting (narrator); False = force off (verifier).
    thinking_override: bool | None = None


@dataclass(slots=True)
class _LoopResult:
    """What the shared loop reports back. `final_text` is the last round's
    prose -- the verifier verdict, with cross-round tool-call self-talk
    dropped (falls back to all-rounds text on round-cap). `recommended_uci`
    is set only when the narrator tracked a recommend_move."""
    final_text: str = ""
    recommended_uci: str | None = None
    recommended_depth: int | None = None
    round_cap_hit: bool = False
    text_published: bool = False


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
        # Board provider lets the round-exit validator check move-tokens
        # in the model's prose against the live position. None disables
        # validation (tests / non-live callers).
        self._board_provider = board_provider
        # End-of-turn verifier; None disables it.
        self._recommend_verifier = recommend_verifier
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
        # tool_use_id of the in-flight delegate call; its verifier's
        # nested tool events stamp this so the client renders them under
        # the right "Verifying line" row. Late-bound. None when idle.
        self._active_delegate_id: str | None = None
        # game_id of the current turn, for stamping verifier-origin tool
        # events so the client muxes them into this session. None outside
        # a turn.
        self._turn_game_id: str | None = None
        # In-mem buffer of events emitted by the current/most-recent
        # turn. Reset on run() start, cleared on analysis stop. Lets a
        # client reconnecting mid-analysis rehydrate the panel.
        self._replay_buffer: list[dict] = []
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
    ) -> None:
        """Run one analysis turn end-to-end.

        Loops: provider round -> on tool_use, dispatch via registry,
        append assistant + tool_result, next round. Stops when the model
        returns without a tool_use or MAX_TOOL_ROUNDS is reached. Emits
        a terminal ai_info event on every exit path so the UI never
        hangs.

        `user_message` carries the game context (FEN + SAN history) the
        agent needs to actually analyze something. Built by the caller
        (`api/ai.py` for live play) via `build_initial_user_message`.
        None falls back to an empty user message for tests that don't
        care about position context.
        """
        active = provider or self._provider
        system_prompt = assemble_system_prompt(mode, tools=self._registry.specs())
        opening_user_content = user_message if user_message is not None else ""
        async with self._lock:
            self._task = asyncio.current_task()
            self._cancel_token = CancelToken()
            self._active_provider = active
            self._turn_context = opening_user_content
            self._turn_game_id = game_id
            self._seq = 0
            self._replay_buffer = []
            messages: list[Message] = [{"role": "user", "content": opening_user_content}]
            tool_schemas = self._registry.schemas() or None
            has_recommend_move = any(
                t.get("name") == "recommend_move"
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
                    max_rounds=MAX_TOOL_ROUNDS,
                    emit=self._emit,
                    game_id=game_id,
                    provider=active,
                    transcript=transcript,
                    completeness_nudge=(
                        _RECOMMEND_NUDGE_PROMPTS[mode] if has_recommend_move else None
                    ),
                    track_recommend=has_recommend_move,
                )
                recommended_uci: str | None = None
                recommended_depth: int | None = None
                try:
                    result = await self._run_loop(messages, config)
                    recommended_uci = result.recommended_uci
                    recommended_depth = result.recommended_depth
                    if result.round_cap_hit:
                        # Loop hit the guardrail, not a natural answer;
                        # lets the UI surface "stopped early; raise the
                        # cap in Settings" if it wants to.
                        done_payload["round_cap"] = True
                        log.warning(
                            "AI agent loop hit round cap (%d); raise SV_AI_MAX_TOOL_ROUNDS if intentional",
                            MAX_TOOL_ROUNDS,
                        )
                    elif not result.text_published:
                        # Model exited the loop with zero user-facing
                        # text (reasoning-only models, refusals).
                        done_payload["no_response"] = True
                    elif has_recommend_move and recommended_uci is None:
                        # Naturally ended without an accepted move despite
                        # the repeated nudge -- distinct from a round-cap so
                        # the UI says "no move chosen", not "raise the cap".
                        done_payload["no_recommendation"] = True
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
                            move = chess.Move.from_uci(recommended_uci)
                            payload = await self._recommend_verifier(
                                move, recommended_depth, self._cancel_token
                            )
                            if payload is not None:
                                await self._emit(
                                    Event(
                                        kind="ai_recommendation",
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
                    await transcript.turn_end(done_payload)
                    await self._emit(
                        Event(
                            kind="ai_info",
                            game_id=game_id,
                            payload=done_payload,
                        )
                    )
                    self._task = None
                    self._cancel_token = None
                    self._active_provider = None
                    self._turn_context = ""
                    self._turn_game_id = None
                    self._active_delegate_id = None

    async def _run_loop(
        self, messages: list[Message], config: _LoopConfig,
    ) -> _LoopResult:
        """Shared multi-round agent loop. Drives provider rounds, streams
        text/thinking via `config.emit`, validates prose, dispatches tool
        calls (with single-slot dedup + lazy card injection), and injects
        correctives. Returns a `_LoopResult`; the caller owns transcript
        framing, recommend verification, and the terminal done event.

        Used by both the narrator turn (`run`, real emit) and verifier
        sub-runs (`_run_verifier`, silent emit). The two differ only via
        `config`."""
        emit = config.emit
        game_id = config.game_id
        mode = config.mode
        cards_injected: set[str] = set()
        last_call: tuple[tuple, dict] | None = None
        text_parts: list[str] = []
        recommended_uci: str | None = None
        recommended_depth: int | None = None
        any_tool_called = False
        nudge_sent = False
        # Narrator: re-nudge toward an accepted recommend_move each clean
        # exit until one lands, but stop once a nudge draws no new attempt.
        # These two track "progress since the last nudge" for that guard.
        recommend_attempts = 0
        attempts_at_last_nudge = -1
        # Flips when prose lands after recommend_move is accepted (the
        # closing conclusion); gates the post-recommend nudge.
        prose_after_recommend = False
        post_recommend_nudge_sent = False
        round_cap_hit = True  # flipped to False on natural exit
        text_published = False  # flips on first non-empty text chunk
        final_text = ""  # last round's prose only (verifier verdict)
        for round_index in range(config.max_rounds):
            round_chunks: list[ProviderChunk] = []
            pending_tool: ProviderChunk | None = None
            provider_stream = config.provider.stream(
                system=config.system_prompt,
                messages=messages,
                tools=config.tool_schemas,
                transcript=config.transcript,
                round_index=round_index,
                thinking=config.thinking_override,
            )
            # Strip paired markdown (**, __, `) so validators see clean
            # prose -- a "bishop on **f2**" wrapper would otherwise hide
            # the square from them.
            async for chunk in strip_markdown_stream(provider_stream):
                round_chunks.append(chunk)
                await config.transcript.chunk(round_index, chunk)
                if chunk.kind == "text" and chunk.text:
                    text_published = True
                    text_parts.append(chunk.text)
                    await emit(
                        Event(
                            kind="ai_info",
                            game_id=game_id,
                            payload={"delta": chunk.text, "round": round_index},
                        )
                    )
                elif chunk.kind == "thinking" and chunk.text:
                    await emit(
                        Event(
                            kind="ai_thinking",
                            game_id=game_id,
                            payload={"delta": chunk.text, "round": round_index},
                        )
                    )
                elif chunk.kind == "tool_use":
                    # In sequential mode (v1), a tool_use ends the round;
                    # downstream chunks after it would belong to the next
                    # round per Anthropic semantics. Capture and break.
                    pending_tool = chunk
                    break
            round_had_text = any(
                c.kind == "text" and c.text for c in round_chunks
            )
            # Validate every round's prose, even when a tool_use follows.
            # Once a move is recommended, the closing prose is a
            # live-position plan -- drop the commentator history walk so a
            # square reused by a later piece can't excuse a false claim.
            illegal, false_claims, castle_violations = (
                self._validate_round_text(
                    round_chunks, mode, committed=recommended_uci is not None,
                )
            )
            if (
                pending_tool is None
                and not illegal
                and not false_claims
                and not castle_violations
            ):
                # Natural exit. Completeness nudge: narrator re-nudges each
                # clean exit until a move is accepted (stopping on a stall);
                # verifier nudges once to call a tool before concluding.
                if self._needs_nudge(
                    config, nudge_sent, recommended_uci, any_tool_called,
                    recommend_attempts, attempts_at_last_nudge,
                ):
                    log.info("completeness nudge (%s): injecting corrective", mode)
                    nudge_sent = True
                    attempts_at_last_nudge = recommend_attempts
                    _inject_nudge(messages, round_chunks, config.completeness_nudge)
                    continue
                # Prose in a post-acceptance round is the conclusion we
                # wanted -- record it so the nudge below doesn't fire.
                if recommended_uci is not None and round_had_text:
                    prose_after_recommend = True
                # Move accepted but no closing conclusion yet: ask for it
                # once. Small models treat the tool call as the end; this
                # backstops the prompt. Narrator-only (track_recommend).
                if (
                    config.track_recommend
                    and recommended_uci is not None
                    and not prose_after_recommend
                    and not post_recommend_nudge_sent
                ):
                    log.info("post-recommend nudge (%s): asking for conclusion", mode)
                    post_recommend_nudge_sent = True
                    _inject_nudge(
                        messages, round_chunks, _POST_RECOMMEND_NUDGE_PROMPTS[mode],
                    )
                    continue
                round_cap_hit = False
                # Verdict = this final round's prose only, so cross-round
                # tool-call self-talk ("I need the FEN", "let me check")
                # doesn't leak up to the narrator.
                final_text = "".join(
                    c.text for c in round_chunks if c.kind == "text" and c.text
                )
                break
            messages.append(_assistant_message(round_chunks))
            if pending_tool is not None:
                any_tool_called = True
                # Single-slot dedup. Cache hit returns the prior result
                # without re-dispatching and without a duplicate UI dot.
                key = self._dedup_key(pending_tool)
                cache_hit = (
                    key is not None
                    and last_call is not None
                    and last_call[0] == key
                )
                if cache_hit:
                    log.info("tool dedup hit: %s", pending_tool.tool_name)
                    tool_output = last_call[1]
                else:
                    # Surface the call to the UI only on a real dispatch.
                    await emit(
                        Event(
                            kind="ai_tool_call",
                            game_id=game_id,
                            payload={
                                "round": round_index,
                                "name": pending_tool.tool_name,
                                "input": pending_tool.tool_input,
                                "tool_use_id": pending_tool.tool_use_id,
                            },
                        )
                    )
                    # Tag the verifier's nested events with this delegate's
                    # id (client nesting); cleared after so a later narrator
                    # call isn't misattributed.
                    is_delegate = pending_tool.tool_name == _DELEGATE_TOOL_NAME
                    if is_delegate:
                        self._active_delegate_id = pending_tool.tool_use_id
                    try:
                        tool_output = await self._dispatch_tool(
                            pending_tool, registry=config.registry,
                        )
                    finally:
                        if is_delegate:
                            self._active_delegate_id = None
                    if key is not None:
                        # Cache success and error alike; deterministic
                        # rejection is as redundant as deterministic success.
                        last_call = (key, tool_output)
                    await emit(
                        Event(
                            kind="ai_tool_call_complete",
                            game_id=game_id,
                            payload={
                                "round": round_index,
                                "name": pending_tool.tool_name,
                                "tool_use_id": pending_tool.tool_use_id,
                            },
                        )
                    )
                await config.transcript.tool_result(
                    round_index, pending_tool.tool_use_id, tool_output
                )
                # Safe to read from a cached recommend_move result: the
                # cached uci is identical to a fresh dispatch's.
                if config.track_recommend and pending_tool.tool_name == "recommend_move":
                    recommend_attempts += 1
                    if (
                        isinstance(tool_output, dict)
                        and tool_output.get("ok")
                        and isinstance(tool_output.get("uci"), str)
                    ):
                        recommended_uci = tool_output["uci"]
                        recommended_depth = tool_output.get("depth")
                        # A conclusion alongside the accepting call counts
                        # -- no separate post-move round needed. Prose in a
                        # later round is handled at the natural-exit check.
                        if round_had_text:
                            prose_after_recommend = True
                if isinstance(tool_output, dict) and tool_output.get("error"):
                    await emit(
                        Event(
                            kind="ai_tool_call_failed",
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
                    pending_tool.tool_name, cards_injected, registry=config.registry,
                )
                messages.append(
                    _tool_result_message(
                        pending_tool.tool_use_id, tool_output, card=card,
                    )
                )
            if illegal or false_claims or castle_violations:
                # After tool_result (if any) so every assistant tool_use
                # has a matching tool_result before the next user message.
                await self._append_corrective(
                    messages,
                    illegal=illegal,
                    false_claims=false_claims,
                    castle_violations=castle_violations,
                    game_id=game_id,
                    round_index=round_index,
                    mode=mode,
                    emit=emit,
                )
        # On round-cap the narrator falls back to all-rounds text (consumer
        # is recommended_uci). The verifier must NOT -- its prose IS the
        # verdict; cross-round self-talk would poison it. Empty -> no_verdict.
        final = final_text or ("".join(text_parts) if config.track_recommend else "")
        return _LoopResult(
            final_text=final,
            recommended_uci=recommended_uci,
            recommended_depth=recommended_depth,
            round_cap_hit=round_cap_hit,
            text_published=text_published,
        )

    @staticmethod
    def _needs_nudge(
        config: _LoopConfig,
        nudge_sent: bool,
        recommended_uci: str | None,
        any_tool_called: bool,
        recommend_attempts: int,
        attempts_at_last_nudge: int,
    ) -> bool:
        """Completeness nudge decision. Narrator re-nudges toward an
        accepted recommend_move on every clean exit until one lands,
        stopping only when a prior nudge drew no new attempt (the model is
        ignoring it -- round_cap is the remaining backstop). Verifier nudge
        stays one-shot: call a tool before concluding."""
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
        return not nudge_sent and not any_tool_called

    def delegate_runner(self) -> VerifierRunner:
        """The verifier sub-run callable to hand `make_delegate_tool`.
        Public seam so wiring (app.py) doesn't reach into a private
        method to build the narrator's `delegate` tool."""
        return self._run_verifier

    async def _run_verifier(self, question: str) -> str:
        """Run one verifier sub-run for the narrator's `delegate` call.

        Forwards only its tool-call events to the UI (via `_verifier_emit`,
        nested under the delegate row) and suppresses its prose/thinking;
        the verdict is returned, not streamed. Uses the verifier registry
        (engine tools, no `delegate` -- one level deep, no recursion) and
        the verifier prompt. Returns the assembled verdict prose (empty
        string when the model produced none).

        Runs inline within the narrator's turn, sharing its cancel token
        (self._cancel_token); no lock re-entry. Opens its own transcript
        turn for traceability.
        """
        if self._verifier_registry is None:
            return ""
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
                max_rounds=VERIFIER_MAX_ROUNDS,
                emit=self._verifier_emit,
                game_id=self._turn_game_id,
                provider=provider,
                transcript=transcript,
                completeness_nudge=_VERIFIER_TOOL_NUDGE,
                track_recommend=False,
                # Engine does the reasoning; model thinking only adds
                # latency (x fan-out) and risks Ollama <think> in verdicts.
                thinking_override=False,
            )
            # done_payload feeds the transcript turn_end only -- a verifier
            # sub-run emits no user-facing done event (it's internal).
            done_payload: dict = {"done": True}
            try:
                result = await self._run_loop(messages, config)
                if result.round_cap_hit:
                    done_payload["round_cap"] = True
                    # final_text is "" on a verifier cap (see _run_loop);
                    # delegate maps that to no_verdict. Warn so a model that
                    # never concludes in VERIFIER_MAX_ROUNDS is diagnosable.
                    log.warning(
                        "verifier sub-run hit round cap (%d) without a verdict; question=%r",
                        VERIFIER_MAX_ROUNDS, question,
                    )
                return result.final_text
            except Exception as exc:
                done_payload["error"] = type(exc).__name__
                done_payload["error_detail"] = str(exc)[:ERROR_DETAIL_MAX_LEN]
                raise
            finally:
                await transcript.turn_end(done_payload)

    async def _verifier_emit(self, event: Event) -> None:
        """Emit sink for verifier sub-runs. Forwards engine-search tool
        events (so the user sees progress under the originating "Verifying
        line" row) and drops the verifier's prose/thinking. Each forwarded
        event is stamped with the active delegate's tool_use_id (for
        client-side nesting) and the turn's game_id (for session muxing)."""
        if event.kind not in _VERIFIER_FORWARDED_KINDS:
            return
        # Safe to mutate in place: _run_loop builds a fresh Event per emit.
        event.payload["parent_tool_use_id"] = self._active_delegate_id
        if event.game_id is None:
            event.game_id = self._turn_game_id
        await self._emit(event)

    async def _emit(self, event: Event) -> None:
        self._seq += 1
        event.payload["seq"] = self._seq
        # Shallow-copy the payload so a downstream subscriber that
        # mutates what it receives can't retroactively change replay.
        self._replay_buffer.append({
            "kind": event.kind,
            "payload": dict(event.payload),
            "game_id": event.game_id,
        })
        await self._bus.publish(event)

    def replay(self) -> list[dict]:
        return list(self._replay_buffer)

    def clear_replay(self) -> None:
        # Rebind (not .clear()) so an in-flight replay() iteration on
        # the old list stays consistent.
        self._replay_buffer = []

    async def _append_corrective(
        self,
        messages: list[Message],
        *,
        illegal: list[str],
        false_claims: list[str],
        castle_violations: list[str],
        game_id: str | None,
        round_index: int,
        mode: PromptMode,
        emit: EmitSink,
    ) -> None:
        """Append a corrective user message and emit ai_corrective."""
        templates = _CORRECTIVES[mode]
        parts: list[str] = []
        if illegal:
            parts.append(templates["illegal"].format(moves=", ".join(illegal)))
        if false_claims:
            parts.append(templates["false_piece"].format(claims=", ".join(false_claims)))
        if castle_violations:
            parts.append(templates["castle"])
        messages.append({
            "role": "user",
            "content": _CORRECTIVE_PREFIX + " ".join(parts),
        })
        log.info(
            "AI agent loop: validator hits in round %d: moves=%s claims=%s castle=%s",
            round_index, illegal, false_claims, castle_violations,
        )
        await emit(
            Event(
                kind="ai_corrective",
                game_id=game_id,
                payload={
                    "round": round_index + 1,
                    "illegal_moves": illegal,
                    "false_claims": false_claims,
                    "castle_violations": castle_violations,
                },
            )
        )

    def _validate_round_text(
        self, chunks: list[ProviderChunk], mode: PromptMode, *,
        committed: bool = False,
    ) -> tuple[list[str], list[str], list[str]]:
        """Run all validators on a round's assembled text.
        Returns (illegal_moves, false_piece_claims, castle_violations).
        Empty triple when clean, when no board_provider is wired, or
        when no live board is available. In commentator mode the
        board sequence includes every prior position via move_stack
        walk -- references to earlier-game pieces aren't flagged --
        unless `committed`, which collapses the walk to the live board
        for the post-recommendation closing prose."""
        if self._board_provider is None:
            return [], [], []
        board = self._board_provider()
        if board is None:
            return [], [], []
        text = "".join(c.text for c in chunks if c.kind == "text" and c.text)
        if not text:
            return [], [], []
        boards = _boards_for_validation(board, mode, committed=committed)
        # Verifier reasons about hypothetical lines, so a move token
        # ("after exd4...") is expected, not a live-move hallucination --
        # skip illegal-move validation. Piece/castle guards still apply.
        illegal = (
            [] if mode == _VERIFIER_MODE else find_illegal_moves(text, boards)
        )
        return (
            illegal,
            find_false_piece_claims(text, boards),
            find_castle_word_violations(text, boards),
        )

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
        """Look up + invoke a tool. Unknown name or tool-raised exceptions
        produce structured error results instead of breaking the loop --
        the model can read the error and recover (or give up gracefully)."""
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
            log.exception("tool %s raised", call.tool_name)
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
