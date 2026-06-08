"""start_ai_turn forwards the per-turn round caps from settings into
coord.run(), so a user's Settings values actually bound the agent loop."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from sturddle_view.api._ai_kick import start_ai_turn
from sturddle_view.config import Settings


class _FakeCoord:
    def __init__(self):
        self.run_kwargs: dict | None = None
        self.cancelled = False

    async def cancel(self):
        self.cancelled = True

    async def run(self, **kwargs):
        # Record what the kick handed us; nothing else to do.
        self.run_kwargs = kwargs


def _fake_request(settings, coord):
    # start_ai_turn reads coordinator/factory/settings/hve off app.state and
    # builds a provider via the factory. hve=None makes _build_user_message
    # return None and _prompt_mode_for return coach -- enough to exercise the
    # forwarding path without a live board.
    state = SimpleNamespace(
        ai_coordinator=coord,
        ai_provider_factory=lambda: object(),
        settings=settings,
        hve=None,
        ai_task=None,
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


@pytest.mark.asyncio
async def test_round_caps_forwarded_to_run():
    settings = Settings(token="t", ai_max_tool_rounds=50, ai_verifier_max_rounds=12)
    coord = _FakeCoord()
    request = _fake_request(settings, coord)

    await start_ai_turn(request)
    # The turn runs in a pinned task; await it so run_kwargs is populated.
    await request.app.state.ai_task

    assert coord.run_kwargs["max_tool_rounds"] == 50
    assert coord.run_kwargs["verifier_max_rounds"] == 12


@pytest.mark.asyncio
async def test_round_caps_default_when_unset():
    settings = Settings(token="t")
    coord = _FakeCoord()
    request = _fake_request(settings, coord)

    await start_ai_turn(request)
    await request.app.state.ai_task

    assert coord.run_kwargs["max_tool_rounds"] == 32
    assert coord.run_kwargs["verifier_max_rounds"] == 8
