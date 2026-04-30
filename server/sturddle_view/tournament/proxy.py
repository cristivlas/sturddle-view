"""Generic stdio proxy.

Sits between the tournament manager and an engine binary. Forwards
stdin/stdout transparently and broadcasts a copy of all traffic to the
GUI backend server.

No chess or UCI knowledge here; this is a dumb pipe. Run as a separate
process per engine instance so a proxy crash does not take down the
engine.

Invoked as a script (the orchestrator builds the argv when wrapping
each engine for fastchess)::

    python -m sturddle_view.tournament.proxy \\
        --broadcast-url http://127.0.0.1:8765/internal/proxy \\
        --proxy-id <uuid> \\
        --secret <per-tournament-secret> \\
        --engine-name "Sturddle 2.5.0" \\
        -- <engine_binary> [engine_args...]

The broadcast tap is **batched** (see ``BATCH_INTERVAL_S`` and
``BATCH_MAX_LINES``) so that engines emitting hundreds of ``info`` lines
per second don't generate that many HTTP requests.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.error
import urllib.request


# Batching defaults — see spec "Volume & high-concurrency considerations".
BATCH_INTERVAL_S = 0.05  # 50 ms
BATCH_MAX_LINES = 32


class Broadcaster:
    """Buffers proxy lines and POSTs them in batches to the server's
    ``/internal/proxy`` endpoint. Failures are logged to stderr and
    swallowed — the proxy must never block the engine pipe on
    broadcast trouble.
    """

    def __init__(
        self,
        url: str,
        proxy_id: str,
        secret: str,
        engine_name: str | None,
    ) -> None:
        self._url = url
        self._proxy_id = proxy_id
        self._secret = secret
        self._engine_name = engine_name
        self._buf: list[str] = []
        self._last_flush = time.monotonic()
        self._announced = False

    def _post(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            self._url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=2) as _resp:
                pass
        except (urllib.error.URLError, OSError) as e:
            # Don't crash the engine because the GUI server is down.
            print(f"proxy broadcast failed: {e}", file=sys.stderr, flush=True)

    def announce(self) -> None:
        """First post: register the proxy session with the server."""
        if self._announced:
            return
        self._announced = True
        await_safe = self._post  # synchronous; called from sync context
        await_safe({
            "proxy_id": self._proxy_id,
            "secret": self._secret,
            "engine_name": self._engine_name,
            "lines": [],
            "ended": False,
        })

    def add_line(self, line: str) -> None:
        self._buf.append(line)
        now = time.monotonic()
        if (
            len(self._buf) >= BATCH_MAX_LINES
            or now - self._last_flush >= BATCH_INTERVAL_S
        ):
            self.flush()

    def flush(self) -> None:
        if not self._buf:
            return
        lines, self._buf = self._buf, []
        self._last_flush = time.monotonic()
        self._post({
            "proxy_id": self._proxy_id,
            "secret": self._secret,
            "engine_name": self._engine_name,
            "lines": lines,
            "ended": False,
        })

    def end(self) -> None:
        # Final flush + ended sentinel.
        self.flush()
        self._post({
            "proxy_id": self._proxy_id,
            "secret": self._secret,
            "engine_name": self._engine_name,
            "lines": [],
            "ended": True,
        })


async def _pump(
    src: asyncio.StreamReader,
    dst_writer,
    broadcaster: Broadcaster | None,
    tag: str,
) -> None:
    """Forward src -> dst_writer line by line, also pushing each line to
    the broadcaster (if any). ``tag`` is "in" (fastchess→engine) or
    "out" (engine→fastchess) — currently only used for stderr logging
    on broadcast failure.
    """
    del tag  # reserved
    while True:
        line = await src.readline()
        if not line:
            break
        dst_writer.write(line)
        try:
            dst_writer.flush()
        except AttributeError:
            pass
        if broadcaster is not None:
            try:
                broadcaster.add_line(line.decode("utf-8", errors="replace"))
            except Exception as e:  # noqa: BLE001 - belt and suspenders
                print(f"proxy add_line failed: {e}", file=sys.stderr, flush=True)


async def _periodic_flush(broadcaster: Broadcaster, stop_event: asyncio.Event) -> None:
    """Time-based flush: ensures we don't sit on buffered lines longer
    than ``BATCH_INTERVAL_S`` even when no new lines are arriving."""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=BATCH_INTERVAL_S)
        except asyncio.TimeoutError:
            pass
        broadcaster.flush()


async def _run(
    engine_argv: list[str],
    broadcast_url: str | None,
    proxy_id: str,
    secret: str | None,
    engine_name: str | None,
) -> int:
    proc = await asyncio.create_subprocess_exec(
        *engine_argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=sys.stderr,
    )
    assert proc.stdin and proc.stdout

    broadcaster: Broadcaster | None = None
    flush_task: asyncio.Task | None = None
    stop_event = asyncio.Event()
    if broadcast_url and secret:
        broadcaster = Broadcaster(broadcast_url, proxy_id, secret, engine_name)
        broadcaster.announce()
        flush_task = asyncio.create_task(_periodic_flush(broadcaster, stop_event))

    # tournament_manager stdin -> engine stdin
    loop = asyncio.get_running_loop()
    stdin_reader = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(stdin_reader), sys.stdin
    )

    upstream = asyncio.create_task(_pump(stdin_reader, proc.stdin, broadcaster, "in"))
    downstream = asyncio.create_task(_pump(proc.stdout, sys.stdout.buffer, broadcaster, "out"))

    rc = await proc.wait()
    upstream.cancel()
    downstream.cancel()
    stop_event.set()
    if flush_task is not None:
        try:
            await flush_task
        except asyncio.CancelledError:
            pass
    if broadcaster is not None:
        broadcaster.end()
    return rc


def main() -> None:
    parser = argparse.ArgumentParser(description="sturddle-view stdio proxy")
    parser.add_argument("--broadcast-url", default=None)
    parser.add_argument("--proxy-id", required=True)
    parser.add_argument("--secret", default=None,
                        help="per-tournament secret for the broadcast endpoint")
    parser.add_argument("--engine-name", default=None,
                        help="display name reported on session start")
    parser.add_argument("engine", help="Engine binary path")
    parser.add_argument("engine_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    engine_argv = [args.engine, *args.engine_args]
    rc = asyncio.run(_run(
        engine_argv,
        args.broadcast_url,
        args.proxy_id,
        args.secret,
        args.engine_name,
    ))
    sys.exit(rc)


if __name__ == "__main__":
    main()
