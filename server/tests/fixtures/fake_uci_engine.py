"""Minimal UCI engine stub for spawn/cancel perf benches.

Responds to: uci, isready, position, go, stop, quit.
Run directly: python -m tests.fixtures.fake_uci_engine
"""
from __future__ import annotations

import sys

_BESTMOVE = "bestmove e2e4"


def main() -> None:
    for raw in sys.stdin:
        line = raw.strip()
        if line == "uci":
            print("id name FakeEngine")
            print("id author test")
            print("uciok", flush=True)
        elif line == "isready":
            print("readyok", flush=True)
        elif line.startswith("position"):
            pass
        elif line.startswith("go") or line == "stop":
            print(_BESTMOVE, flush=True)
        elif line == "quit":
            break


if __name__ == "__main__":
    main()
