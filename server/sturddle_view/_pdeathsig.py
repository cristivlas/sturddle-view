"""Linux: have the kernel signal a child when its parent dies.

POSIX children (fastchess, engines) run in their own session, so a
crashed or SIGKILLed server would otherwise leave them running.
No equivalent on macOS; Windows uses Job objects (_win_job.py).
"""
from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Callable

_PR_SET_PDEATHSIG = 1


def die_with_parent(sig: int) -> Callable[[], None] | None:
    """Popen ``preexec_fn`` delivering *sig* to the child when the
    spawning thread exits; ``None`` off Linux."""
    if not sys.platform.startswith("linux"):
        return None
    parent = os.getpid()
    # resolve in the parent: no dlopen between fork and exec
    prctl = ctypes.CDLL(None, use_errno=True).prctl

    def preexec() -> None:
        prctl(_PR_SET_PDEATHSIG, sig)
        # parent died before prctl took effect
        if os.getppid() != parent:
            os.kill(os.getpid(), sig)

    return preexec
