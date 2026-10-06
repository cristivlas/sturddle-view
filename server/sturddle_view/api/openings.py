"""Opening list endpoint over the vendored lichess-openings dataset."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..auth import require_token
from ._http import bad_request

router = APIRouter(prefix="/openings", tags=["openings"], dependencies=[Depends(require_token)])

_FEN_KEY = "fen"
_MOVES_KEY = "moves"


@router.get("")
def list_openings(request: Request) -> dict:
    """Full opening list (one row per name); the client filters locally."""
    return {
        "results": [op.as_dict() for op in request.app.state.openings.all()]
    }


@router.post("/line")
def line_openings(payload: dict, request: Request) -> dict:
    """The book opening at each position a played line reaches (null where
    none), so the board's opening label can follow the line."""
    fen = payload.get(_FEN_KEY)
    moves = payload.get(_MOVES_KEY)
    if not isinstance(fen, str):
        raise bad_request(f"{_FEN_KEY} must be a string")
    if not isinstance(moves, list) or not all(isinstance(m, str) for m in moves):
        raise bad_request(f"{_MOVES_KEY} must be a list of strings")
    try:
        hits = request.app.state.openings.line_openings(fen, moves)
    except ValueError as exc:
        raise bad_request(f"bad {_FEN_KEY}: {exc}") from exc
    return {"results": [hit.as_dict() if hit else None for hit in hits]}
