"""AI analysis coordinator.

Owns the LLM session for the live play path (path 1 in the spec). Drains
the provider's chunk stream onto the websocket event bus as `ai_info`
events. Path 2/3 (view, post-game) layer onto this in later phases.

Slice B scope: real multi-turn agent loop. Each round = one
provider.stream() call. On a tool_use chunk the coordinator dispatches
via the registry, appends assistant + tool_result messages, and runs
another round. Bounded by MAX_TOOL_ROUNDS so a stuck model can't burn
budget forever.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

from ..events import Event, EventBus
from ..llm import LLMProvider, Message, ProviderChunk, ToolRegistry, UnknownToolError
from ..llm.cancel import CancelToken


log = logging.getLogger(__name__)


# Cap on agent loop rounds per turn (spec §Guardrails: "Tool call cap
# per agent turn"). The env override is for ops; UI exposure lands in
# Phase 4 (Advanced collapsible).
_DEFAULT_MAX_TOOL_ROUNDS = 8
MAX_TOOL_ROUNDS = int(os.environ.get("SV_AI_MAX_TOOL_ROUNDS", _DEFAULT_MAX_TOOL_ROUNDS))


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


def _tool_result_message(tool_use_id: str, result: dict | str) -> Message:
    """Build the user-role tool_result message that closes one tool call.

    Wire shape mirrors Anthropic's. The provider for Ollama translates
    to OpenAI on the way out.
    """
    if not isinstance(result, str):
        result = json.dumps(result)
    return {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": tool_use_id, "content": result}
        ],
    }


class AIAnalysisCoordinator:
    def __init__(
        self,
        bus: EventBus,
        provider: LLMProvider,
        registry: ToolRegistry | None = None,
    ) -> None:
        self._bus = bus
        # Default provider for callers that don't supply one per turn.
        # Per-turn override (run(provider=...)) is the seam later phases
        # use to swap prompts / agents without rebuilding the coordinator.
        self._provider = provider
        self._registry = registry if registry is not None else ToolRegistry()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._cancel_token: CancelToken | None = None

    async def run(
        self,
        *,
        game_id: str | None = None,
        provider: LLMProvider | None = None,
    ) -> None:
        """Run one analysis turn end-to-end.

        Loops: provider round -> on tool_use, dispatch via registry,
        append assistant + tool_result, next round. Stops when the model
        returns without a tool_use or MAX_TOOL_ROUNDS is reached. Emits
        a terminal ai_info event on every exit path so the UI never
        hangs.
        """
        active = provider or self._provider
        async with self._lock:
            self._task = asyncio.current_task()
            self._cancel_token = CancelToken()
            # Slice B: empty user message; Slice D adds the prompt.
            messages: list[Message] = [{"role": "user", "content": ""}]
            tool_schemas = self._registry.schemas() or None
            try:
                round_cap_hit = True  # flipped to False on natural exit
                for _round in range(MAX_TOOL_ROUNDS):
                    round_chunks: list[ProviderChunk] = []
                    pending_tool: ProviderChunk | None = None
                    async for chunk in active.stream(
                        system="",
                        messages=messages,
                        tools=tool_schemas,
                    ):
                        round_chunks.append(chunk)
                        if chunk.kind == "text" and chunk.text:
                            await self._bus.publish(
                                Event(
                                    kind="ai_info",
                                    game_id=game_id,
                                    payload={"delta": chunk.text},
                                )
                            )
                        elif chunk.kind == "tool_use":
                            # In sequential mode (v1), a tool_use ends
                            # the round; downstream chunks after it would
                            # belong to the next round per Anthropic
                            # semantics. We capture the call and break.
                            pending_tool = chunk
                            break
                    if pending_tool is None:
                        # Round ended without tool_use -> turn complete.
                        round_cap_hit = False
                        break
                    messages.append(_assistant_message(round_chunks))
                    tool_output = await self._dispatch_tool(pending_tool)
                    messages.append(
                        _tool_result_message(pending_tool.tool_use_id, tool_output)
                    )
                done_payload: dict = {"done": True}
                if round_cap_hit:
                    # Signal that the loop terminated on the guardrail
                    # rather than reaching a natural answer -- lets the
                    # UI surface "stopped early; raise the tool-call cap
                    # in Settings" if it wants to.
                    done_payload["round_cap"] = True
                await self._bus.publish(
                    Event(
                        kind="ai_info",
                        game_id=game_id,
                        payload=done_payload,
                    )
                )
            except asyncio.CancelledError:
                await self._bus.publish(
                    Event(
                        kind="ai_info",
                        game_id=game_id,
                        payload={"done": True, "cancelled": True},
                    )
                )
                raise
            except Exception as exc:
                # No silent failures (spec §Error Handling). Surface to
                # the bus so the UI exits its "streaming" state, and
                # re-raise so the task's done-callback can log details.
                log.exception("AI agent loop failed")
                await self._bus.publish(
                    Event(
                        kind="ai_info",
                        game_id=game_id,
                        payload={"done": True, "error": type(exc).__name__},
                    )
                )
                raise
            finally:
                self._task = None
                self._cancel_token = None

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
