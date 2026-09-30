"""Progress steps a tool reports while it runs, shown under its panel row.

A tool whose work has distinct phases (recommend_move: the engine's own best,
then the candidate) reports each phase so the panel names what the board is
showing. The coordinator installs a reporter around each dispatch; outside a
dispatch, reports go nowhere. Context-scoped, so tools need no extra argument
and nested dispatches (a verifier sub-run) each see their own reporter.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Awaitable, Callable, Iterator

# (step name, step input) -> None. The name picks the panel label; the input
# shows in the row's IN detail.
ProgressReporter = Callable[[str, dict], Awaitable[None]]

_reporter: ContextVar[ProgressReporter | None] = ContextVar("tool_progress", default=None)


@contextmanager
def reporting_progress(reporter: ProgressReporter) -> Iterator[None]:
    """Route report_progress() calls made within the block to `reporter`."""
    token = _reporter.set(reporter)
    try:
        yield
    finally:
        _reporter.reset(token)


async def report_progress(name: str, input_: dict) -> None:
    """Report one step of the running tool; a no-op outside a dispatch."""
    reporter = _reporter.get()
    if reporter is not None:
        await reporter(name, input_)
