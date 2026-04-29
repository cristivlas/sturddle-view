from __future__ import annotations

import hmac

from fastapi import HTTPException, Query, Request, status

from .config import Settings


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def require_token(request: Request, token: str | None = Query(default=None)) -> None:
    """Single shared-secret token, accepted via ?token= or Authorization: Bearer."""
    settings = _settings(request)
    if settings.auth_disabled:
        return
    presented = token
    if presented is None:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            presented = auth.split(None, 1)[1].strip()
    if presented is None or not hmac.compare_digest(presented, settings.token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")
