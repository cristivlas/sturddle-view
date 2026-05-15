from __future__ import annotations

import hmac
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

from .config import Settings


# Name of the cookie set by /auth handshake. Single source of truth for
# the cookie reader, the /auth route, and tests.
AUTH_COOKIE = "sv_auth"


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _present_token(request: Request) -> str | None:
    """Pull a candidate token from cookie or Authorization: Bearer."""
    cookie = request.cookies.get(AUTH_COOKIE)
    if cookie:
        return cookie
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(None, 1)[1].strip()
    return None


def require_token(request: Request) -> None:
    """Token check: cookie (browser) or Authorization: Bearer (CLI)."""
    settings = _settings(request)
    if settings.auth_disabled:
        return
    presented = _present_token(request)
    if presented is None or not hmac.compare_digest(presented, settings.token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")


def check_token_value(settings: Settings, presented: str | None) -> bool:
    """Constant-time token compare. Honors auth_disabled. For WS handlers."""
    if settings.auth_disabled:
        return True
    if not presented:
        return False
    return hmac.compare_digest(presented, settings.token)


def origin_ok(request_or_ws) -> bool:
    """If Origin is present, it must match Host. Missing Origin is allowed
    (non-browser clients don't send it; browsers always do on cross-origin
    requests, so this still defeats CSRF and DNS rebinding from a page).
    """
    headers = request_or_ws.headers
    origin = headers.get("origin")
    if not origin:
        return True
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    host_header = headers.get("host", "")
    if not host_header:
        return False
    return parts.netloc == host_header
