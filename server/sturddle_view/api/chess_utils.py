"""Chess utility endpoints backed by python-chess.
Stateless helpers the UI can call instead of reimplementing chess rules in JS.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import chess

from ..auth import require_token

router = APIRouter(prefix="/api/chess", tags=["chess"], dependencies=[Depends(require_token)])


class ApplyMoveRequest(BaseModel):
    fen: str
    move: str  # UCI, e.g. "e2e4" or "a7a8q"


@router.post("/apply-move")
def apply_move(body: ApplyMoveRequest) -> dict:
    try:
        board = chess.Board(body.fen)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid FEN")
    try:
        move = chess.Move.from_uci(body.move)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid UCI move")
    if move not in board.legal_moves:
        raise HTTPException(status_code=400, detail="illegal move")
    board.push(move)
    return {"fen": board.fen()}
