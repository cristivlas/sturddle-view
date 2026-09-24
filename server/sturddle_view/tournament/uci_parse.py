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

# UCI commands. A parsed line's ``kind`` is its command name.
UCI_POSITION = "position"
UCI_GO = "go"
UCI_INFO = "info"
UCI_BESTMOVE = "bestmove"
UCI_NEWGAME = "ucinewgame"

# Parsed-line keys.
KIND_KEY = "kind"
FEN_KEY = "fen"
MOVES_KEY = "moves"
MOVE_KEY = "move"
SIDE_TO_MOVE_KEY = "side_to_move"

_STARTPOS = "startpos"
_FEN_ARG = "fen "
_GO_INT_ARGS = ("wtime", "btime", "winc", "binc", "movetime", "depth", "nodes")
_GO_FLAG_ARGS = ("ponder", "infinite")


def command_prefix(command: str) -> str:
    """Line prefix of a UCI command that takes arguments."""
    return f"{command} "


def parse_uci_line(line: str) -> dict[str, Any] | None:
    """Return a small structured dict for recognized UCI lines, or
    ``None`` for lines we don't care about (most ``info``-string
    boilerplate, ``readyok``, ``id``, ``option`` etc.)."""
    s = line.strip()
    if not s:
        return None
    for command, parse in _PARSERS:
        prefix = command_prefix(command)
        if s.startswith(prefix):
            return parse(s[len(prefix):])
    return None


def _position(board: chess.Board, moves: list[str], last_move: str | None, **extra) -> dict:
    return {
        KIND_KEY: UCI_POSITION,
        FEN_KEY: board.fen(),
        MOVES_KEY: moves,
        "last_move": last_move,
        "ply": len(moves),
        **extra,
    }


def _parse_position(rest: str) -> dict[str, Any] | None:
    """``position startpos [moves <m1> <m2> ...]`` ->
    ``{kind: "position", fen: <after-moves-fen>, moves: [...], last_move: ?, ply: N}``.
    """
    if rest.startswith(_STARTPOS):
        board = board_from(None)
        rest = rest[len(_STARTPOS):].strip()
    elif rest.startswith(_FEN_ARG):
        idx = rest.find(f" {MOVES_KEY}")
        if idx == -1:
            fen_str = rest[len(_FEN_ARG):].strip()
            rest = ""
        else:
            fen_str = rest[len(_FEN_ARG):idx].strip()
            rest = rest[idx + 1:]
        try:
            board = board_from(fen_str)
        except ValueError:
            return None
    else:
        return None

    moves: list[str] = []
    if rest.startswith(MOVES_KEY):
        moves_str = rest[len(MOVES_KEY):].strip()
        moves = moves_str.split() if moves_str else []

    last_move: str | None = None
    for uci in moves:
        try:
            move = chess.Move.from_uci(uci)
            board.push(move)
            last_move = uci
        except (ValueError, AssertionError, chess.IllegalMoveError):
            # Engine sent something unexpected. Bail out with what we
            # have -- the browser can show stale state rather than crash.
            return _position(board, moves, last_move, error="illegal_move_in_position")

    return _position(board, moves, last_move, **{SIDE_TO_MOVE_KEY: side_to_move(board)})


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
    parsed[KIND_KEY] = UCI_INFO
    return _add_legacy_aliases(parsed)


def _add_legacy_aliases(parsed: dict[str, Any]) -> dict[str, Any]:
    """Add the legacy keys (``score_cp``/``score_mate``/``pv``) the web
    tournament view still reads. Removed when web migrates."""
    score = parsed.get("score")
    if score is not None:
        if (cp := score.get("cp")) is not None:
            parsed["score_cp"] = cp
        elif (mate := score.get("mate")) is not None:
            parsed["score_mate"] = mate
    # Guard: do not clobber an already-present ``pv`` (e.g. SAN list a
    # future tournament-side board reconstruction might supply).
    pv_uci = parsed.get("pv_uci")
    if pv_uci is not None:
        parsed.setdefault("pv", pv_uci)
    return parsed


def _parse_bestmove(rest: str) -> dict[str, Any]:
    parts = rest.strip().split()
    move = parts[0] if parts else None
    return {KIND_KEY: UCI_BESTMOVE, MOVE_KEY: move}


def _parse_go(rest: str) -> dict[str, Any]:
    """``go wtime W btime B winc Wi binc Bi [movetime M]`` -> dict."""
    out: dict[str, Any] = {KIND_KEY: UCI_GO}
    tokens = rest.split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if tok in _GO_INT_ARGS:
            if i < len(tokens):
                try:
                    out[tok] = int(tokens[i])
                    i += 1
                except ValueError:
                    pass
        elif tok in _GO_FLAG_ARGS:
            out[tok] = True
    return out


_PARSERS = (
    (UCI_POSITION, _parse_position),
    (UCI_INFO, _parse_info),
    (UCI_BESTMOVE, _parse_bestmove),
    (UCI_GO, _parse_go),
)
