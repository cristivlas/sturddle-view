"""Append-only conversation transcript for AI analysis turns.

Captures every wire-level event that flows between the coordinator, the
provider, and the model: system prompt, user message, request body sent
to the model, raw streamed bytes back from the model, parsed
ProviderChunks, tool calls, tool results, and the terminal state. One
rolling file (`ai-transcript.log`) under the user data dir; the operator
trims it by hand when it grows.

Opt-in via `SV_AI_TRANSCRIPT=1`. When off, the coordinator threads a
`NullTranscript` through everything so call sites do not need null
checks.

Why a dedicated file and not stdlib `logging`:
- Captures raw multi-line bytes (SSE payloads, JSON bodies, full
  prompts) where logging's per-line formatter would mangle structure.
- Survives logger config changes by environment / desktop bundle.
- Single source of truth for after-the-fact debugging -- the operator
  can `tail -f` or grep it without setting up a logging config.
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import platformdirs

from .. import app_dir_name
from .base import ProviderChunk


TRANSCRIPT_FILENAME = "ai-transcript.log"
TRANSCRIPT_ENV_VAR = "SV_AI_TRANSCRIPT"

TURN_SEPARATOR = "=" * 78


def default_transcript_path() -> Path:
    """Where the rolling transcript file lives by default."""
    return Path(platformdirs.user_data_dir(app_dir_name(), appauthor=False)) / TRANSCRIPT_FILENAME


def transcript_enabled() -> bool:
    """True iff SV_AI_TRANSCRIPT is set to a truthy value."""
    raw = os.environ.get(TRANSCRIPT_ENV_VAR, "")
    return raw.strip().lower() in ("1", "true", "yes", "on")


class Transcript:
    """Rolling, append-only transcript writer.

    One instance per coordinator turn; `close()` (or use as an async
    context manager) flushes and releases the file handle. Writes go
    through an `asyncio.Lock` so concurrent calls from the same task
    serialize -- in practice only one task writes at a time, but the
    lock guards against future parallel-tool work without a refactor.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Line-buffered text mode so `tail -f` sees output without
        # waiting for a 4 KiB chunk to fill. UTF-8 to match the rest of
        # the codebase; errors=replace so a stray invalid byte from a
        # provider does not crash the writer.
        self._fh = open(self._path, "a", encoding="utf-8", errors="replace", buffering=1)
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    async def close(self) -> None:
        async with self._lock:
            if not self._fh.closed:
                self._fh.flush()
                self._fh.close()

    async def __aenter__(self) -> "Transcript":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    # ---------- High-level event writers -------------------------------

    async def turn_start(self, meta: dict[str, Any]) -> None:
        await self._write_block("turn_start", _json(meta), with_separator=True)

    async def system_prompt(self, text: str) -> None:
        await self._write_block("system", text)

    async def user_message(self, text: str) -> None:
        await self._write_block("user", text)

    async def request(self, round_index: int, body: dict[str, Any]) -> None:
        await self._write_block(f"round {round_index} request", _json(body))

    async def wire_line(self, round_index: int, line: str) -> None:
        """Raw bytes off the wire (SSE line, raw chunk, etc.).

        Captured even when the line is malformed -- that is the whole
        point: if the provider crashes on a payload, the operator can
        read what came back.
        """
        await self._write_block(f"round {round_index} wire", line)

    async def chunk(self, round_index: int, chunk: ProviderChunk) -> None:
        await self._write_block(
            f"round {round_index} chunk", _chunk_repr(chunk)
        )

    async def tool_result(
        self, round_index: int, tool_use_id: str, result: Any
    ) -> None:
        body = _json({"tool_use_id": tool_use_id, "result": result})
        await self._write_block(f"round {round_index} tool_result", body)

    async def turn_end(self, payload: dict[str, Any]) -> None:
        await self._write_block("turn_end", _json(payload))

    # ---------- Low-level ----------------------------------------------

    async def _write_block(
        self, label: str, body: str, *, with_separator: bool = False
    ) -> None:
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        async with self._lock:
            if self._fh.closed:
                return
            if with_separator:
                self._fh.write(TURN_SEPARATOR + "\n")
            self._fh.write(f"[{ts}] [{label}]\n")
            if body:
                self._fh.write(body)
                if not body.endswith("\n"):
                    self._fh.write("\n")
            self._fh.write("\n")


class NullTranscript(Transcript):
    """No-op writer used when transcripts are disabled.

    Subclasses Transcript so type-checkers and call sites treat it the
    same. Every method is a no-op coroutine.
    """

    def __init__(self) -> None:  # noqa: D401 - intentional shadow
        # Skip Transcript.__init__: we have no file to open.
        self._path = Path()
        self._fh = None  # type: ignore[assignment]
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    async def close(self) -> None:
        return

    async def turn_start(self, meta: dict[str, Any]) -> None: return
    async def system_prompt(self, text: str) -> None: return
    async def user_message(self, text: str) -> None: return
    async def request(self, round_index: int, body: dict[str, Any]) -> None: return
    async def wire_line(self, round_index: int, line: str) -> None: return
    async def chunk(self, round_index: int, chunk: ProviderChunk) -> None: return
    async def tool_result(
        self, round_index: int, tool_use_id: str, result: Any
    ) -> None: return
    async def turn_end(self, payload: dict[str, Any]) -> None: return


@asynccontextmanager
async def open_transcript(
    *, path: Path | None = None, enabled: bool | None = None
) -> Iterator[Transcript]:
    """Yield a Transcript (real or null) and ensure it is closed.

    - `enabled=None` (the default) reads `SV_AI_TRANSCRIPT` from the env.
    - `path=None` uses the default user-data-dir location.

    Call sites do not branch on enabled/disabled: the null writer makes
    every method a no-op, so the same code path works either way.
    """
    is_on = transcript_enabled() if enabled is None else bool(enabled)
    if not is_on:
        writer: Transcript = NullTranscript()
        try:
            yield writer
        finally:
            await writer.close()
        return
    real_path = path or default_transcript_path()
    writer = Transcript(real_path)
    try:
        yield writer
    finally:
        await writer.close()


# ---------- Helpers ----------------------------------------------------


def _json(obj: Any) -> str:
    """Pretty-print a dict/list as JSON, falling back to repr for the
    weird stuff a provider might hand us."""
    try:
        return json.dumps(obj, indent=2, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(obj)


def _chunk_repr(chunk: ProviderChunk) -> str:
    return _json({
        "kind": chunk.kind,
        "text": chunk.text,
        "tool_use_id": chunk.tool_use_id,
        "tool_name": chunk.tool_name,
        "tool_input": chunk.tool_input,
    })
