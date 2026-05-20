"""Canonical hashing for PGN / FEN imports.

Two semantically identical games coming from different sources must
hash equal. Verbatim storage is untouched; canonicalization is for
the hash only. See docs/canonical-hash.md for the full spec.
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


def _canonical_pgn(text: str) -> str:
    game = chess.pgn.read_game(io.StringIO(text))
    if game is None:
        raise ValueError("could not parse PGN")
    # Sort headers alphabetically for the hash only.
    sorted_headers = chess.pgn.Headers()
    for k in sorted(game.headers.keys()):
        sorted_headers[k] = game.headers[k]
    game.headers = sorted_headers
    # Collapse whitespace runs inside every comment in the game tree
    # -- mainline AND variations.
    def _walk(node):
        if node.comment:
            node.comment = _normalize_comment(node.comment)
        if getattr(node, "starting_comment", None):
            node.starting_comment = _normalize_comment(node.starting_comment)
        for child in node.variations:
            _walk(child)
    _walk(game)
    return str(game)


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
