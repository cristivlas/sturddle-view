"""Chess utility endpoints backed by python-chess.
Stateless helpers the UI can call instead of reimplementing chess rules in JS.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

import chess

from ..auth import require_token

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chess", tags=["chess"], dependencies=[Depends(require_token)])


class ApplyMoveRequest(BaseModel):
    fen: str
    move: str  # UCI, e.g. "e2e4" or "a7a8q"


class ValidateFenRequest(BaseModel):
    fen: str


@router.post("/apply-move")
def apply_move(body: ApplyMoveRequest) -> dict:
    try:
        board = chess.Board(body.fen)
    except ValueError:
        log.warning("apply-move rejected: invalid FEN %r", body.fen)
        raise HTTPException(status_code=400, detail="invalid FEN")
    try:
        move = chess.Move.from_uci(body.move)
    except ValueError:
        log.warning("apply-move rejected: invalid UCI move %r (fen=%r)", body.move, body.fen)
        raise HTTPException(status_code=400, detail="invalid UCI move")
    if move not in board.legal_moves:
        # Late bestmove: position already advanced. Client will self-correct on next position event.
        log.warning("apply-move late bestmove %r (fen=%r)", body.move, body.fen)
        return Response(status_code=204)
    board.push(move)
    return {"fen": board.fen()}


def _explain_invalid(board: chess.Board) -> str:
    status = board.status()
    if status == chess.STATUS_VALID:
        return "illegal position"
    reasons = [s.name.lower().replace("_", " ") for s in chess.Status if s != chess.STATUS_VALID and status & s]
    return ", ".join(reasons) if reasons else "illegal position"


@router.post("/validate-fen")
def validate_fen(body: ValidateFenRequest) -> dict:
    """Validate a FEN string and return its canonical form."""
    try:
        board = chess.Board(body.fen)
    except ValueError as e:
        msg = str(e).split(":", 1)[0] if ":" in str(e) else str(e)
        raise HTTPException(status_code=400, detail=f"invalid FEN: {msg}") from e
    if not board.is_valid():
        raise HTTPException(status_code=400, detail=_explain_invalid(board))
    return {"fen": board.fen()}
