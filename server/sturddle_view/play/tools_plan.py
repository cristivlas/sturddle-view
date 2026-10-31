"""Playbook tool for the AI analysis agent.

`plan` hands the narrator the rendered playbook line for the live position
(docs/ai-playbook-spec.md) on request: strategic themes to draw ideas from,
input not answer -- the move that survives its play-out decides the pick.
No engine, no eval.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from ..llm import ToolSpec
from ..llm.cancel import CancelToken
from ..llm.tools import NO_LIVE_POSITION_ERROR, object_schema, tool_error


# The plan line for the live position, or None without a live position.
PlanProvider = Callable[[], str | None]
PlanTool = Callable[..., Awaitable[dict]]


PLAN_TOOL_NAME = "plan"
_PLAN_KEY = "plan"


PLAN_TOOL_SPEC = ToolSpec(
    name=PLAN_TOOL_NAME,
    description=(
        "Strategic themes for the side to move in this kind of position "
        "(standing, phase, structure). Input for your ideas, not an answer: "
        "the move that survives its play-out decides."
    ),
    input_schema=object_schema({}),
)


def make_plan_tool(plan_provider: PlanProvider) -> PlanTool:
    """Build the `plan` async tool over `plan_provider`'s rendered line."""
    async def plan(input_: dict, *, cancel_token: CancelToken) -> dict:
        line = plan_provider()
        if line is None:
            return tool_error(NO_LIVE_POSITION_ERROR)
        return {_PLAN_KEY: line}

    return plan
