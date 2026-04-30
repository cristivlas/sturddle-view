"""``FastchessRunner`` — implements the ``Runner`` protocol over fastchess.

Responsibilities (per ``docs/tournament-spec.md``):

  - Build the fastchess CLI from a frozen template.
  - Spawn with cross-platform process-group isolation
    (``CREATE_NEW_PROCESS_GROUP`` on Windows, ``start_new_session`` on Unix).
  - Drain stdout/stderr to ``logs/fastchess.log`` via background tasks.
  - ``stop()`` is a hard kill (``proc.kill()`` + ``proc.wait()``); idempotent.
  - Detect clean exit vs killed; emit ``done`` or ``stopped`` accordingly.

Stub for Slice 0 of the tournament implementation plan; concrete
implementation lands in Slice 3.
"""
from __future__ import annotations


class FastchessRunner:
    def __init__(self, binary_path: str) -> None:
        self._binary_path = binary_path
        self._proc = None

    async def start(self, tournament, on_event) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError

    def is_running(self) -> bool:
        return False
