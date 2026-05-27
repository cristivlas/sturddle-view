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
import os
from typing import Awaitable, Callable

import chess

from ..events import Event, EventBus
from ..llm import (
    LLMProvider,
    Message,
    PromptMode,
    ProviderChunk,
    ToolRegistry,
    UnknownToolError,
    assemble_system_prompt,
    open_transcript,
)
from ..llm.cancel import CancelToken
from ..llm.response_validator import (
    find_castle_word_violations,
    find_false_piece_claims,
    find_illegal_moves,
)


BoardProvider = Callable[[], chess.Board | None]
# End-of-turn verifier; returns payload for ai_recommendation, or None.
RecommendVerifier = Callable[[chess.Move, CancelToken], Awaitable[dict | None]]


log = logging.getLogger(__name__)


# Cap on agent loop rounds per turn (spec §Guardrails: "Tool call cap
# per agent turn"). The env override is for ops; UI exposure is pending.
_DEFAULT_MAX_TOOL_ROUNDS = 32
MAX_TOOL_ROUNDS = int(os.environ.get("SV_AI_MAX_TOOL_ROUNDS", _DEFAULT_MAX_TOOL_ROUNDS))

# Max length of error_detail copied into the done event. Keeps the
# bus payload small even when a provider returns a wall of HTML / a
# verbose stack trace. Full detail is in the transcript anyway.
ERROR_DETAIL_MAX_LEN = 500

# Corrective messages injected when the validator finds inconsistencies
# in the round's text. Civil but firm: the model is hallucinating, and
# the turn is not done until the prose is consistent with the live
# position. Two failure modes:
#   - illegal moves (SAN-shaped tokens that the board rejects)
#   - false piece claims ("X on Y" where Y does not hold X)
# Prefix attributes the message to an automated check, not the user
# (the corrective lands in the user-role slot per Anthropic wire shape,
# but it isn't from the human -- mislabeling distracts the model's
# reasoning trace).
_CORRECTIVE_PREFIX = "[automated position check] "
_ILLEGAL_MOVES_PROMPT = (
    "{moves} do not exist in this position. Rewrite without inventing moves."
)
_FALSE_PIECE_PROMPT = (
    "False piece claim(s): {claims}. Rewrite without inventing pieces."
)
_CASTLE_WORD_PROMPT = (
    "Castling is not legal for either side in this position; "
    "rewrite without recommending it."
)


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
    raw = input_.get("move")
    if not isinstance(raw, str) or board is None:
        return None
    move = _parse_move_canonical(raw, board)
    return ("move", move.uci()) if move else None


def _norm_square_arg(input_: dict, board: chess.Board | None) -> tuple | None:
    raw = input_.get("square")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return ("square", chess.square_name(chess.parse_square(raw.lower())))
    except ValueError:
        return None


def _norm_top_moves(input_: dict, board: chess.Board | None) -> tuple | None:
    # Any unparseable candidate -> skip dedup. Conservative: a partial
    # subset of legal moves still produces a meaningful tool result, but
    # we'd rather miss a cache hit than risk a wrong collapse.
    raw_moves = input_.get("moves")
    if not isinstance(raw_moves, list) or board is None:
        return None
    ucis: list[str] = []
    for r in raw_moves:
        if not isinstance(r, str):
            return None
        move = _parse_move_canonical(r, board)
        if move is None:
            return None
        ucis.append(move.uci())
    # Sort: engine sees `searchmoves` as a set, list order is irrelevant.
    return (
        "moves", tuple(sorted(ucis)),
        input_.get("depth"), input_.get("time_ms"),
    )


def _norm_analyze(input_: dict, board: chess.Board | None) -> tuple | None:
    # chess.Board(fen).fen() canonicalizes whitespace AND drops halfmove
    # / fullmove counters into a fixed shape. Intentional collapse: the
    # engine evaluation is position-dependent, not history-dependent,
    # within a single turn.
    fen = input_.get("fen")
    if not isinstance(fen, str) or not fen.strip():
        return None
    try:
        canonical = chess.Board(fen.strip()).fen()
    except ValueError:
        return None
    return ("analyze", canonical, input_.get("depth"), input_.get("time_ms"))


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
    given tool per turn (see docs/ai-analysis-skills-spec.md).
    """
    if not isinstance(result, str):
        result = json.dumps(result)
    content: list[dict] = [
        {"type": "tool_result", "tool_use_id": tool_use_id, "content": result}
    ]
    if card:
        content.append({"type": "text", "text": card})
    return {"role": "user", "content": content}


class AIAnalysisCoordinator:
    def __init__(
        self,
        bus: EventBus,
        provider: LLMProvider,
        registry: ToolRegistry | None = None,
        *,
        board_provider: BoardProvider | None = None,
        recommend_verifier: RecommendVerifier | None = None,
    ) -> None:
        self._bus = bus
        # Default provider for callers that don't supply one per turn.
        # Per-turn override (run(provider=...)) is the seam later phases
        # use to swap prompts / agents without rebuilding the coordinator.
        self._provider = provider
        self._registry = registry if registry is not None else ToolRegistry()
        # Board provider lets the round-exit validator check move-tokens
        # in the model's prose against the live position. None disables
        # validation (tests / non-live callers).
        self._board_provider = board_provider
        # End-of-turn verifier; None disables it.
        self._recommend_verifier = recommend_verifier
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._cancel_token: CancelToken | None = None
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
            self._seq = 0
            self._replay_buffer = []
            messages: list[Message] = [{"role": "user", "content": opening_user_content}]
            tool_schemas = self._registry.schemas() or None
            # Per-turn tool-card injection state. A tool's card is
            # appended to the first tool_result of the turn and never
            # again; the model retains it via the message-list prefix
            # for subsequent rounds. See docs/ai-analysis-skills-spec.md.
            cards_injected: set[str] = set()
            # Last successful recommend_move uci; last wins.
            recommended_uci: str | None = None
            # Single-slot dedup cache; key+result of the prior call.
            last_call: tuple[tuple, dict] | None = None
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
                try:
                    round_cap_hit = True  # flipped to False on natural exit
                    text_published = False  # flips on first non-empty text chunk
                    for round_index in range(MAX_TOOL_ROUNDS):
                        round_chunks: list[ProviderChunk] = []
                        pending_tool: ProviderChunk | None = None
                        async for chunk in active.stream(
                            system=system_prompt,
                            messages=messages,
                            tools=tool_schemas,
                            transcript=transcript,
                            round_index=round_index,
                        ):
                            round_chunks.append(chunk)
                            await transcript.chunk(round_index, chunk)
                            if chunk.kind == "text" and chunk.text:
                                text_published = True
                                await self._emit(
                                    Event(
                                        kind="ai_info",
                                        game_id=game_id,
                                        payload={"delta": chunk.text, "round": round_index},
                                    )
                                )
                            elif chunk.kind == "thinking" and chunk.text:
                                await self._emit(
                                    Event(
                                        kind="ai_thinking",
                                        game_id=game_id,
                                        payload={"delta": chunk.text, "round": round_index},
                                    )
                                )
                            elif chunk.kind == "tool_use":
                                # In sequential mode (v1), a tool_use ends
                                # the round; downstream chunks after it
                                # would belong to the next round per
                                # Anthropic semantics. Capture and break.
                                pending_tool = chunk
                                break
                        # Validate every round's prose, even when a
                        # tool_use follows (policy change -- previously
                        # the tool_use exit path bypassed validation).
                        illegal, false_claims, castle_violations = (
                            self._validate_round_text(round_chunks)
                        )
                        if (
                            pending_tool is None
                            and not illegal
                            and not false_claims
                            and not castle_violations
                        ):
                            round_cap_hit = False
                            break
                        messages.append(_assistant_message(round_chunks))
                        if pending_tool is not None:
                            # Surface dispatch to the UI; result stays
                            # off-screen in v1 (engine side-effects cover
                            # analyze; piece_at / validate_move silent).
                            await self._emit(
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
                            # Single-slot dedup. Only top-level `error`
                            # clears the slot; per-entry errors (e.g.
                            # top_moves errors list) cache normally since
                            # the same input gives the same result.
                            key = self._dedup_key(pending_tool)
                            if (
                                key is not None
                                and last_call is not None
                                and last_call[0] == key
                            ):
                                log.info("tool dedup hit: %s", pending_tool.tool_name)
                                tool_output = last_call[1]
                            else:
                                tool_output = await self._dispatch_tool(pending_tool)
                                is_error = (
                                    isinstance(tool_output, dict)
                                    and tool_output.get("error")
                                )
                                if is_error or key is None:
                                    last_call = None
                                else:
                                    last_call = (key, tool_output)
                            await transcript.tool_result(
                                round_index, pending_tool.tool_use_id, tool_output
                            )
                            # Safe to read from a cached recommend_move
                            # result: the cached uci is identical to a
                            # fresh dispatch's.
                            if (
                                pending_tool.tool_name == "recommend_move"
                                and isinstance(tool_output, dict)
                                and tool_output.get("ok")
                                and isinstance(tool_output.get("uci"), str)
                            ):
                                recommended_uci = tool_output["uci"]
                            if isinstance(tool_output, dict) and tool_output.get("error"):
                                await self._emit(
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
                            card = self._inject_card_once(pending_tool.tool_name, cards_injected)
                            messages.append(
                                _tool_result_message(
                                    pending_tool.tool_use_id, tool_output, card=card,
                                )
                            )
                        if illegal or false_claims or castle_violations:
                            # After tool_result (if any) so every assistant
                            # tool_use has a matching tool_result before the
                            # next user-role message.
                            await self._append_corrective(
                                messages,
                                illegal=illegal,
                                false_claims=false_claims,
                                castle_violations=castle_violations,
                                game_id=game_id,
                                round_index=round_index,
                            )
                    if round_cap_hit:
                        # Signal that the loop terminated on the guardrail
                        # rather than reaching a natural answer; lets the
                        # UI surface "stopped early; raise the tool-call
                        # cap in Settings" if it wants to.
                        done_payload["round_cap"] = True
                        log.warning(
                            "AI agent loop hit round cap (%d); raise SV_AI_MAX_TOOL_ROUNDS if intentional",
                            MAX_TOOL_ROUNDS,
                        )
                    elif not text_published:
                        # Model exited the loop with zero user-facing
                        # text (reasoning-only models, refusals).
                        done_payload["no_response"] = True
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
                            payload = await self._recommend_verifier(move, self._cancel_token)
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
    ) -> None:
        """Append a corrective user message and emit ai_corrective."""
        parts: list[str] = []
        if illegal:
            parts.append(_ILLEGAL_MOVES_PROMPT.format(moves=", ".join(illegal)))
        if false_claims:
            parts.append(_FALSE_PIECE_PROMPT.format(claims=", ".join(false_claims)))
        if castle_violations:
            parts.append(_CASTLE_WORD_PROMPT)
        messages.append({
            "role": "user",
            "content": _CORRECTIVE_PREFIX + " ".join(parts),
        })
        log.info(
            "AI agent loop: validator hits in round %d: moves=%s claims=%s castle=%s",
            round_index, illegal, false_claims, castle_violations,
        )
        await self._emit(
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
        self, chunks: list[ProviderChunk],
    ) -> tuple[list[str], list[str], list[str]]:
        """Run all validators on a round's assembled text.
        Returns (illegal_moves, false_piece_claims, castle_violations).
        Empty triple when clean, when no board_provider is wired, or
        when no live board is available."""
        if self._board_provider is None:
            return [], [], []
        board = self._board_provider()
        if board is None:
            return [], [], []
        text = "".join(c.text for c in chunks if c.kind == "text" and c.text)
        if not text:
            return [], [], []
        return (
            find_illegal_moves(text, board),
            find_false_piece_claims(text, board),
            find_castle_word_violations(text, board),
        )

    def _inject_card_once(self, tool_name: str, injected: set[str]) -> str | None:
        """Return the tool's card on first call this turn, else None.
        Unknown tool names yield None (no card to inject). Mutates
        `injected` to record the first-use moment."""
        if tool_name in injected:
            return None
        try:
            spec = self._registry.spec(tool_name)
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

    async def _dispatch_tool(self, call: ProviderChunk) -> dict:
        """Look up + invoke a tool. Unknown name or tool-raised exceptions
        produce structured error results instead of breaking the loop --
        the model can read the error and recover (or give up gracefully)."""
        try:
            fn = self._registry.get(call.tool_name)
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
