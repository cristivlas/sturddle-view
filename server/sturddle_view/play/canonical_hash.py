"""Canonical hashing for PGN / FEN imports.

Two semantically identical games coming from different sources must
hash equal. Verbatim storage is untouched; canonicalization is for
the hash only.
"""
from __future__ import annotations

import hashlib
import io
import re

import chess
import chess.pgn


_WS_RUN = re.compile(r"\s+")


def _normalize_comment(text: str) -> str:
    return _WS_RUN.sub(" ", text).strip()


def _render_canonical(game: chess.pgn.Game) -> str:
    """Render the canonical PGN string for an already-parsed game.

    Sorts headers and collapses whitespace runs in every comment
    (mainline AND variations), then str()s the game and restores it.
    Callers get a string they can hash; the game object is unchanged
    on return so it can still be used for display/replay.
    """
    orig_headers = game.headers
    sorted_headers = chess.pgn.Headers()
    for k in sorted(orig_headers.keys()):
        sorted_headers[k] = orig_headers[k]
    snapshots: list = []  # [(node, comment, starting_comment_or_None)]

    def _walk(node):
        sc = getattr(node, "starting_comment", None)
        snapshots.append((node, node.comment, sc))
        if node.comment:
            node.comment = _normalize_comment(node.comment)
        if sc:
            node.starting_comment = _normalize_comment(sc)
        for child in node.variations:
            _walk(child)

    game.headers = sorted_headers
    try:
        _walk(game)
        return str(game)
    finally:
        game.headers = orig_headers
        for node, c, sc in snapshots:
            node.comment = c
            if sc is not None:
                node.starting_comment = sc


def canonical_hash_from_game(game: chess.pgn.Game) -> str:
    """Hash an already-parsed game without reparsing the PGN text.

    Callers that already have the parsed tree (e.g. tournament
    read_game_record) should prefer this over canonical_hash to skip
    the redundant parse.
    """
    return hashlib.sha256(_render_canonical(game).encode("utf-8")).hexdigest()


def _canonical_pgn(text: str) -> str:
    game = chess.pgn.read_game(io.StringIO(text))
    if game is None:
        raise ValueError("could not parse PGN")
    return _render_canonical(game)


def _canonical_fen(text: str) -> str:
    board = chess.Board(text.strip())
    return board.fen()


def canonical_hash(text: str, fmt: str) -> str:
    if fmt == "pgn":
        canon = _canonical_pgn(text)
    elif fmt == "fen":
        canon = _canonical_fen(text)
    else:
        raise ValueError(f"unknown format: {fmt!r}")
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()
