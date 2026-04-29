"""Generic stdio proxy.

Sits between the tournament manager and an engine binary. Forwards stdin/stdout
transparently and broadcasts a copy of all traffic to the GUI backend.

No chess or UCI knowledge here; this is a dumb pipe. Run as a separate process
per engine instance so a proxy crash does not take down the engine.

Invoked as a script:

    python -m sturddle_view.tournament.proxy <engine_binary> [engine_args...] \\
        --broadcast-url http://127.0.0.1:8765/internal/proxy \\
        --proxy-id <uuid>

The orchestrator rewrites tournament configs to spawn this script in place of
the real engine path.
"""
from __future__ import annotations

import argparse
import asyncio
import sys


async def _pump(src: asyncio.StreamReader, dst_writer, tap, tag: str) -> None:
    """Forward src -> dst_writer line by line, also pushing each line to `tap`."""
    while True:
        line = await src.readline()
        if not line:
            break
        dst_writer.write(line)
        try:
            dst_writer.flush()
        except AttributeError:
            pass
        await tap(tag, line)


async def _run(engine_argv: list[str], broadcast_url: str | None, proxy_id: str) -> int:
    proc = await asyncio.create_subprocess_exec(
        *engine_argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=sys.stderr,
    )
    assert proc.stdin and proc.stdout

    async def tap(tag: str, line: bytes) -> None:
        # TODO: post to broadcast_url. Until then, the proxy is a transparent pipe.
        return None

    # tournament_manager stdin -> engine stdin
    loop = asyncio.get_running_loop()
    stdin_reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(stdin_reader), sys.stdin)

    upstream = asyncio.create_task(_pump(stdin_reader, proc.stdin, tap, "in"))
    downstream = asyncio.create_task(_pump(proc.stdout, sys.stdout.buffer, tap, "out"))

    rc = await proc.wait()
    upstream.cancel()
    downstream.cancel()
    return rc


def main() -> None:
    parser = argparse.ArgumentParser(description="sturddle-view stdio proxy")
    parser.add_argument("--broadcast-url", default=None)
    parser.add_argument("--proxy-id", required=True)
    parser.add_argument("engine", help="Engine binary path")
    parser.add_argument("engine_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    engine_argv = [args.engine, *args.engine_args]
    rc = asyncio.run(_run(engine_argv, args.broadcast_url, args.proxy_id))
    sys.exit(rc)


if __name__ == "__main__":
    main()
