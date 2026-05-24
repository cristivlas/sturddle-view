"""Slice B Step 2: CancelToken contract.

The cancel token is the per-call cancellation handle passed into every
tool callable. Tool implementations check it cooperatively
(`if token.cancelled: ...`) and abort their work cleanly. The runner
flips the token on a user cancel before tearing down the agent loop.

This is the day-1 piece of the spec's "parallel-tool tolerant" design
(spec §Triggers, Forward-looking) -- per-call tokens mean two concurrent
tools don't share cancellation state.
"""
from __future__ import annotations

import asyncio

import pytest

from sturddle_view.llm.cancel import CancelToken


def test_token_starts_uncancelled():
    t = CancelToken()
    assert t.cancelled is False


def test_cancel_flips_flag():
    t = CancelToken()
    t.cancel()
    assert t.cancelled is True


def test_cancel_is_idempotent():
    t = CancelToken()
    t.cancel()
    t.cancel()
    assert t.cancelled is True


@pytest.mark.asyncio
async def test_wait_cancelled_unblocks_on_cancel():
    # Lets a tool sleep on the token directly (no polling) when waiting
    # on external I/O it cannot interrupt itself -- e.g., a wrapped
    # subprocess that needs an external signal.
    t = CancelToken()
    waiter = asyncio.create_task(t.wait_cancelled())
    # Without cancel, the waiter should be pending. Yield once so the
    # event loop has a chance to schedule it; if it still hasn't
    # completed, the token isn't spuriously firing.
    await asyncio.sleep(0)
    assert not waiter.done()
    t.cancel()
    await waiter
    assert waiter.done()


@pytest.mark.asyncio
async def test_wait_cancelled_completes_immediately_after_prior_cancel():
    t = CancelToken()
    t.cancel()
    # If we call wait_cancelled AFTER cancel, it must complete without
    # blocking -- otherwise tools that check late hang forever.
    await asyncio.wait_for(t.wait_cancelled(), timeout=0.1)


def test_independence_between_tokens():
    a = CancelToken()
    b = CancelToken()
    a.cancel()
    assert a.cancelled is True
    assert b.cancelled is False
