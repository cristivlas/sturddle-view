"""Cancelling an analysis start while the engine owes `readyok` makes
python-chess send `go` after its own `stop`, leaving the engine searching
for nobody. Supervisor-spawned engines do that handshake up front, so no
later command waits on `readyok`."""
from __future__ import annotations

import asyncio
from pathlib import Path

import chess
import chess.engine
import pytest

from sturddle_view.events import EventBus
from sturddle_view.play.engine_supervisor import EngineSupervisor

from .conftest import _write_uci_stub


def _make_held_readyok_fake(root: Path, hold: Path, release: Path) -> str:
    """While `hold` exists, holds the post-`ucinewgame` readyok until
    `release` exists; like a real engine, ignores `stop` when not searching."""
    pre_loop = "import os, time\nsearching = False\n"
    extra = (
        "    elif line == 'ucinewgame':\n"
        "        sys.stdin.readline()  # the isready that follows\n"
        f"        while os.path.exists({str(hold)!r}) and not os.path.exists({str(release)!r}):\n"
        "            time.sleep(0.01)\n"
        "        sys.stdout.write('readyok\\n'); sys.stdout.flush()\n"
        "    elif line.startswith('go'): searching = True\n"
        "    elif line == 'stop' and searching:\n"
        "        searching = False\n"
        "        sys.stdout.write('bestmove e2e4\\n'); sys.stdout.flush()\n"
    )
    return _write_uci_stub(root, "held_readyok_fake", extra, pre_loop=pre_loop)


@pytest.mark.asyncio
@pytest.mark.parametrize("throwaway", [False, True])
async def test_cancelled_analysis_start_leaves_engine_usable(tmp_path: Path, throwaway: bool):
    hold, release = tmp_path / "hold", tmp_path / "release"
    sup = EngineSupervisor(engine_path=_make_held_readyok_fake(tmp_path, hold, release), bus=EventBus())
    if throwaway:
        engine, cleanup = await sup.spawn_throwaway()
    else:
        engine, cleanup = await sup.spawn(), sup.quit
    loop_errors: list[dict] = []
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda _loop, ctx: loop_errors.append(ctx))
    hold.touch()
    try:
        start = asyncio.create_task(engine.analysis(chess.Board()))
        await asyncio.sleep(0)
        start.cancel()
        release.touch()
        try:
            analysis = await start
        except asyncio.CancelledError:
            pass
        else:
            analysis.stop()
        await engine.ping()
        assert not loop_errors
    finally:
        loop.set_exception_handler(None)
        release.touch()
        await cleanup()
