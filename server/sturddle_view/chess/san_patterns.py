"""Shared SAN regex sub-patterns (source strings for composition, not
compiled). All groups non-capturing so callers' finditer/findall return
whole tokens and recognizers built from them can't drift."""
from __future__ import annotations

SAN_CASTLE = r"O-O-O|O-O"
SAN_PIECE_MOVE = r"[KQRBN][a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?[+#]?"
SAN_PAWN_CAPTURE = r"[a-h]x[a-h][1-8](?:=[QRBN])?[+#]?"
SAN_PAWN_PUSH = r"[a-h][1-8](?:=[QRBN])?[+#]?"
# Any single SAN move, bare pawn pushes included.
SAN_MOVE = rf"(?:{SAN_CASTLE}|{SAN_PIECE_MOVE}|{SAN_PAWN_CAPTURE}|{SAN_PAWN_PUSH})"
# Move-number prefix: "1.", "12.", "3...".
SAN_MOVE_NUMBER = r"\d+\.(?:\.\.)?"
