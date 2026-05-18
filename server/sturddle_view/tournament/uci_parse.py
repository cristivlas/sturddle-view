"""Light UCI line parser for the live observation pipeline.

Selected line types are enriched on the server side so subscribers
get a board-ready payload without re-parsing in the browser. Per the
spec's "selective parsing" rule this only runs for lines that have at
least one WS subscriber.
"""
from __future__ import annotations

from typing import Any

import chess

from ..chess.board import board_from, side_to_move
from ..chess.engine_info import parse_info_tokens


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
        board = board_from(None)
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
            board = board_from(fen_str)
        except ValueError:
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
        "side_to_move": side_to_move(board),
    }


def _parse_info(rest: str) -> dict[str, Any] | None:
    """Parse a UCI ``info`` line tail into the unified engine-info schema,
    then layer legacy aliases for web tournament-live-game.js.

    TODO: drop ``_add_legacy_aliases`` once the web tournament view migrates
    to read ``score.cp`` / ``score.mate`` / ``pv_uci`` directly. The
    underlying ``parse_info_tokens`` already produces the unified shape.
    """
    parsed = parse_info_tokens(rest)
    if parsed is None:
        return None
    parsed["kind"] = "info"
    return _add_legacy_aliases(parsed)


def _add_legacy_aliases(parsed: dict[str, Any]) -> dict[str, Any]:
    """Add pre-R7 keys (``score_cp``/``score_mate``/``pv``) the web
    tournament view still reads. Removed when web migrates."""
    score = parsed.get("score")
    if score is not None:
        if "cp" in score:
            parsed["score_cp"] = score["cp"]
        elif "mate" in score:
            parsed["score_mate"] = score["mate"]
    # Guard: do not clobber an already-present ``pv`` (e.g. SAN list a
    # future tournament-side board reconstruction might supply).
    if "pv" not in parsed:
        pv_uci = parsed.get("pv_uci")
        if pv_uci is not None:
            parsed["pv"] = pv_uci
    return parsed


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
