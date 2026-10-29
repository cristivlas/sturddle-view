"""`plan` tool: the playbook line on request, not on the first message."""
from __future__ import annotations

import pytest

from sturddle_view.llm.cancel import CancelToken
from sturddle_view.play.tools_plan import PLAN_TOOL_SPEC, make_plan_tool


@pytest.mark.asyncio
async def test_plan_returns_the_rendered_line():
    tool = make_plan_tool(lambda: "Plan: you are level; a plan before tactics.")
    out = await tool({}, cancel_token=CancelToken())
    assert out == {"plan": "Plan: you are level; a plan before tactics."}


@pytest.mark.asyncio
async def test_plan_without_a_live_position_is_an_error():
    out = await make_plan_tool(lambda: None)({}, cancel_token=CancelToken())
    assert out == {"error": "no_live_position"}


def test_plan_takes_no_arguments():
    assert PLAN_TOOL_SPEC.input_schema["properties"] == {}
