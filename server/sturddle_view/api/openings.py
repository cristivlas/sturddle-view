"""Opening list endpoint over the vendored lichess-openings dataset."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..auth import require_token

router = APIRouter(prefix="/openings", tags=["openings"], dependencies=[Depends(require_token)])


@router.get("")
def list_openings(request: Request) -> dict:
    """Full opening list (one row per name); the client filters locally."""
    return {
        "results": [
            {"eco": op.eco, "name": op.name, "pgn": op.pgn, "ply": op.ply}
            for op in request.app.state.openings.all()
        ]
    }
