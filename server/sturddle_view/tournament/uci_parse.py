"""Light UCI line parser for the live observation pipeline.

Selected line types are enriched on the server side so subscribers
get a board-ready payload without re-parsing in the browser. Per the
spec's "selective parsing" rule this only runs for lines that have at
least one WS subscriber.
"""
from __future__ import annotations

from typing import Any

import chess


def parse_uci_line(line: str) -> dict[str, Any] | None:
    """Return a small structured dict for recognized UCI lines, or
    ``None`` for lines we don't care about (most ``info``-string
    boilerplate, ``readyok``, ``id``, ``option`` etc.)."""
    s = line.strip()
    if not s:
        return None

    if s.startswith("position "):
        return _parse_position(s[len("position "):])
    if s.startswith("info "):
        return _parse_info(s[len("info "):])
    if s.startswith("bestmove "):
        return _parse_bestmove(s[len("bestmove "):])
    if s.startswith("go "):
        return _parse_go(s[len("go "):])
    return None


def _parse_position(rest: str) -> dict[str, Any] | None:
    """``position startpos [moves <m1> <m2> ...]`` →
    ``{kind: "position", fen: <after-moves-fen>, moves: [...], last_move: ?, ply: N}``.
    """
    if rest.startswith("startpos"):
        board = chess.Board()
        rest = rest[len("startpos"):].strip()
    elif rest.startswith("fen "):
        idx = rest.find(" moves")
        if idx == -1:
            fen_str = rest[len("fen "):].strip()
            rest = ""
        else:
            fen_str = rest[len("fen "):idx].strip()
            rest = rest[idx + 1:]
        try:
            board = chess.Board(fen_str)
        except (ValueError, chess.InvalidFenError):
            return None
    else:
        return None

    moves: list[str] = []
    if rest.startswith("moves"):
        moves_str = rest[len("moves"):].strip()
        moves = moves_str.split() if moves_str else []

    last_move: str | None = None
    for uci in moves:
        try:
            move = chess.Move.from_uci(uci)
            board.push(move)
            last_move = uci
        except (ValueError, AssertionError, chess.IllegalMoveError):
            # Engine sent something unexpected. Bail out with what we
            # have — the browser can show stale state rather than crash.
            return {
                "kind": "position",
                "fen": board.fen(),
                "moves": moves,
                "last_move": last_move,
                "ply": len(moves),
                "error": "illegal_move_in_position",
            }

    return {
        "kind": "position",
        "fen": board.fen(),
        "moves": moves,
        "last_move": last_move,
        "ply": len(moves),
        "side_to_move": "white" if board.turn == chess.WHITE else "black",
    }


def _parse_info(rest: str) -> dict[str, Any] | None:
    """A subset of UCI ``info`` fields we care about for the live
    view. We skip the mainline-spamming garbage and surface depth /
    score / pv / time / nodes / nps."""
    out: dict[str, Any] = {"kind": "info"}
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
        if tok in ("depth", "seldepth", "time", "nodes", "nps", "hashfull", "tbhits", "multipv"):
            taken = take_int(i)
            if taken:
                out[tok], i = taken
        elif tok == "score":
            if i < len(tokens) and tokens[i] == "cp":
                taken = take_int(i + 1)
                if taken:
                    out["score_cp"], i = taken
            elif i < len(tokens) and tokens[i] == "mate":
                taken = take_int(i + 1)
                if taken:
                    out["score_mate"], i = taken
            else:
                # unknown score type — skip
                i += 1
        elif tok == "pv":
            out["pv"] = tokens[i:]
            i = len(tokens)
        else:
            # Unknown token — skip its argument heuristically (most
            # info fields are <name> <int>).
            i += 1

    # An info line with nothing useful (e.g. ``info string ...``) is
    # not worth surfacing.
    if len(out) == 1:
        return None
    return out


def _parse_bestmove(rest: str) -> dict[str, Any]:
    parts = rest.strip().split()
    move = parts[0] if parts else None
    return {"kind": "bestmove", "move": move}


def _parse_go(rest: str) -> dict[str, Any]:
    """``go wtime W btime B winc Wi binc Bi [movetime M]`` → dict."""
    out: dict[str, Any] = {"kind": "go"}
    tokens = rest.split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if tok in ("wtime", "btime", "winc", "binc", "movetime", "depth", "nodes"):
            if i < len(tokens):
                try:
                    out[tok] = int(tokens[i])
                    i += 1
                except ValueError:
                    pass
        elif tok == "ponder":
            out["ponder"] = True
        elif tok == "infinite":
            out["infinite"] = True
    return out
