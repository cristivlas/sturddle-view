"""Generic stdio proxy between fastchess and an engine.

Dumb pipe with a broadcast tap to the GUI backend. One process per engine slot.

Invocation (orchestrator builds argv)::

    SV_PROXY_SECRET=<per-tournament-secret> \\
    python -m sturddle_view.tournament.proxy \\
        --broadcast-url http://127.0.0.1:8765/internal/proxy \\
        --engine-name "Sturddle 2.5.0" \\
        -- <engine_binary> [engine_args...]

- Secret comes via env, not argv, so other local users can't read it from
  ``ps`` / ``/proc/<pid>/cmdline``. fastchess inherits and passes it to
  each engine slot.
- ``proxy_id`` is generated per process (fastchess reuses argv across
  slots, so it can't come from the orchestrator's pre-built argv).
- Broadcast tap is batched (``BATCH_INTERVAL_S`` / ``BATCH_MAX_LINES``)
  so chatty ``info`` lines don't translate 1:1 into HTTP requests.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid


# Batching defaults — see spec "Volume & high-concurrency considerations".
BATCH_INTERVAL_S = 0.05  # 50 ms
BATCH_MAX_LINES = 32

# Lines matching this pattern are not broadcast (pipe to engine stays transparent).
# TODO: consider a "System" settings category with user-editable log filters
# (hot-reload and perf implications TBD before exposing in UI).
_BROADCAST_FILTER: re.Pattern | None = None

# Kill-switch: when SV_BROADCAST_INFO=0, drop UCI ``info`` lines from the
# broadcast tap. The pipe to fastchess remains transparent; only the
# observability fan-out is suppressed. Used to bisect perf regressions
# between the broadcast machinery and the engine itself.
_BROADCAST_INFO = os.environ.get("SV_BROADCAST_INFO", "1") != "0"


class Broadcaster:
    """Buffers proxy lines and POSTs them in batches to the server's
    ``/internal/proxy`` endpoint. Failures are logged to stderr and
    swallowed — the proxy must never block the engine pipe on
    broadcast trouble.

    Posts run in a background worker thread so the asyncio loop that
    pumps engine stdio is never blocked by HTTP latency. ``urllib`` is
    synchronous; calling it directly from the loop stalls the engine
    pipe and produces multi-second observable lag.
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
        # Background poster: a single worker thread drains a queue of
        # payloads and POSTs them in order. add_line / flush are
        # non-blocking from the loop's perspective.
        self._post_q: queue.Queue = queue.Queue()
        self._post_stopped = threading.Event()
        self._post_thread = threading.Thread(
            target=self._post_worker, daemon=True
        )
        self._post_thread.start()

    def _post_worker(self) -> None:
        while True:
            payload = self._post_q.get()
            if payload is None:
                self._post_stopped.set()
                return
            self._post_blocking(payload)

    def _post_blocking(self, payload: dict) -> None:
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

    def _post(self, payload: dict) -> None:
        self._post_q.put(payload)

    def announce(self) -> None:
        """First post: register the proxy session with the server."""
        if self._announced:
            return
        self._announced = True
        self._post({
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
        # Final flush + ended sentinel + drain the worker.
        self.flush()
        self._post({
            "proxy_id": self._proxy_id,
            "secret": self._secret,
            "engine_name": self._engine_name,
            "lines": [],
            "ended": True,
        })
        self._post_q.put(None)
        # Bounded wait — don't hold up shutdown if the server is slow.
        self._post_stopped.wait(timeout=2)


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
            decoded = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not _BROADCAST_INFO and decoded.lstrip().startswith("info "):
                continue
            if _BROADCAST_FILTER is None or not _BROADCAST_FILTER.match(decoded):
                try:
                    broadcaster.add_line(decoded)
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


async def _make_stdin_reader(loop: asyncio.AbstractEventLoop) -> asyncio.StreamReader:
    """Hook sys.stdin into a StreamReader. Windows ProactorEventLoop
    can't connect_read_pipe() on the inherited (non-overlapped) stdin
    handle, so use a daemon thread + feed_data on that platform."""
    reader = asyncio.StreamReader(loop=loop)
    if sys.platform == "win32":
        def _pump() -> None:
            try:
                while True:
                    line = sys.stdin.buffer.readline()
                    if not line:
                        loop.call_soon_threadsafe(reader.feed_eof)
                        return
                    loop.call_soon_threadsafe(reader.feed_data, line)
            except Exception:
                loop.call_soon_threadsafe(reader.feed_eof)
        threading.Thread(target=_pump, name="proxy-stdin", daemon=True).start()
    else:
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
        )
    return reader


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
    stdin_reader = await _make_stdin_reader(loop)

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
    parser.add_argument("--proxy-id", default=None,
                        help="optional explicit proxy id (tests). When "
                             "omitted, a per-process uuid is generated so "
                             "concurrent fastchess game-slots running the "
                             "same engine spec get distinct ids.")
    parser.add_argument("--engine-name", default=None,
                        help="display name reported on session start")
    parser.add_argument("engine", help="Engine binary path")
    parser.add_argument("engine_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    proxy_id = args.proxy_id or f"p-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    # Pop (don't get) so the engine subprocess we spawn below doesn't
    # inherit the secret in its environment.
    secret = os.environ.pop("SV_PROXY_SECRET", None)

    engine_argv = [args.engine, *args.engine_args]
    rc = asyncio.run(_run(
        engine_argv,
        args.broadcast_url,
        proxy_id,
        secret,
        args.engine_name,
    ))
    sys.exit(rc)


if __name__ == "__main__":
    main()
