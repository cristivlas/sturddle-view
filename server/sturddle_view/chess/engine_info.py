"""Unified engine-info schema (R7) used by both play and tournament paths.

Two producers, one shape:
- ``serialize_info`` takes a python-chess ``InfoDict`` + board (HVE path).
- ``parse_info_tokens`` takes a raw UCI ``info`` line tail (tournament path,
  receiving stdout from a fastchess-spawned engine without a chess.engine
  wrapper).

Both return the same dict shape:
    {depth, seldepth, time, nodes, nps, hashfull, tbhits, multipv,
     score: {cp|mate: int} | absent,
     pv_uci: [str, ...] | absent,
     pv: [str] | absent}
Keys are absent when the source lacks them. ``pv`` (single-element SAN list)
is only present when a board is supplied to ``serialize_info``; tournament
has no board context, so its output carries ``pv_uci`` only.
"""
from __future__ import annotations

from typing import Any

import chess
import chess.engine

_INT_FIELDS = ("depth", "seldepth", "time", "nodes", "nps",
               "hashfull", "tbhits", "multipv")


def serialize_info(
    info: chess.engine.InfoDict,
    *,
    board: chess.Board | None,
    pov: chess.Color,
) -> dict:
    """Serialize a python-chess InfoDict into the unified schema."""
    out: dict = {}
    for key in _INT_FIELDS:
        if key in info:
            out[key] = info[key]
    score = info.get("score")
    if score is not None:
        side = score.pov(pov)
        if side.is_mate():
            out["score"] = {"mate": side.mate()}
        else:
            out["score"] = {"cp": side.score()}
    pv = info.get("pv")
    if pv:
        uci = [m.uci() for m in pv]
        out["pv_uci"] = uci
        if board is not None:
            try:
                out["pv"] = [board.variation_san(pv)]
            except (ValueError, AssertionError):
                # Fall back to UCI tokens if SAN rendering rejects the line.
                out["pv"] = uci
    return out


def parse_info_tokens(rest: str) -> dict | None:
    """Parse a raw UCI ``info`` line tail (text after the ``info`` keyword).

    Returns the unified shape, or ``None`` for lines with nothing useful
    (e.g. ``info string ...``).
    """
    out: dict[str, Any] = {}
    tokens = rest.split()
    i = 0

    def take_int(j: int) -> tuple[int, int] | None:
        if j >= len(tokens):
            return None
        try:
            return int(tokens[j]), j + 1
        except ValueError:
            return None

    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if tok in _INT_FIELDS:
            taken = take_int(i)
            if taken:
                out[tok], i = taken
        elif tok == "score":
            if i < len(tokens) and tokens[i] == "cp":
                taken = take_int(i + 1)
                if taken:
                    val, i = taken
                    out["score"] = {"cp": val}
            elif i < len(tokens) and tokens[i] == "mate":
                taken = take_int(i + 1)
                if taken:
                    val, i = taken
                    out["score"] = {"mate": val}
            else:
                i += 1
        elif tok == "pv":
            out["pv_uci"] = tokens[i:]
            i = len(tokens)
        else:
            # Unknown token -- skip its argument heuristically.
            i += 1

    return out or None
