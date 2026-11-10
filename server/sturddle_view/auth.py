from __future__ import annotations

import hmac
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status
from starlette.requests import HTTPConnection

from .config import Settings


# Name of the cookie set by /auth handshake. Single source of truth for
# the cookie reader, the /auth route, and tests.
AUTH_COOKIE = "sv_auth"
# The /auth handshake route and its token query parameter. Shared by the
# route and the entry-URL builder (netinfo.entry_url).
AUTH_PATH = "/auth"
AUTH_TOKEN_PARAM = "token"
# The UI asks which scope its cookie carries, to hide what would be refused.
AUTH_ROLE_PATH = f"{AUTH_PATH}/role"
INVALID_TOKEN_DETAIL = "invalid token"
HOST_ONLY_DETAIL = "only available on the host computer"

# Two token scopes. Host (desktop window, console link) can do anything.
# Remote (the Connect-from-mobile QR) can play and watch, but not change what
# the server runs or where it writes: routes are host-only unless they sit
# on a module's remote_router.
ROLE_HOST = "host"
ROLE_REMOTE = "remote"

# Logged whenever an auth-less server becomes reachable beyond loopback:
# at startup for a --host bind, or when desktop opens the LAN port.
AUTH_DISABLED_LAN_WARNING = (
    "AUTH DISABLED on non-loopback bind %s -- anyone reachable on the network "
    "can control this server. Use only on a trusted network."
)


def presented_token(conn: HTTPConnection) -> str | None:
    """Candidate token from cookie (browser) or Authorization: Bearer (CLI).
    Works for both Request and WebSocket."""
    cookie = conn.cookies.get(AUTH_COOKIE)
    if cookie:
        return cookie
    auth = conn.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(None, 1)[1].strip()
    return None


def _same_token(presented: str, token: str) -> bool:
    # Bytes: compare_digest raises TypeError on non-ASCII str.
    return hmac.compare_digest(presented.encode(), token.encode())


def token_role(settings: Settings, presented: str | None) -> str | None:
    """Role the token grants, or None when it is neither. Constant-time."""
    if settings.auth_disabled:
        return ROLE_HOST
    if not presented:
        return None
    if _same_token(presented, settings.token):
        return ROLE_HOST
    if _same_token(presented, settings.remote_token):
        return ROLE_REMOTE
    return None


def connection_role(conn: HTTPConnection) -> str | None:
    return token_role(conn.app.state.settings, presented_token(conn))


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_TOKEN_DETAIL)


def require_host(request: Request) -> None:
    role = connection_role(request)
    if role is None:
        raise _unauthorized()
    if role != ROLE_HOST:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=HOST_ONLY_DETAIL)


def require_token(request: Request) -> None:
    """Any valid token: host or remote."""
    if connection_role(request) is None:
        raise _unauthorized()


def ws_allowed(conn: HTTPConnection) -> bool:
    """WebSocket gate: Origin check plus any valid token (streams are
    read-only, so remote clients may watch)."""
    return origin_ok(conn) and connection_role(conn) is not None


def origin_ok(request_or_ws) -> bool:
    """If Origin is present, it must match Host. Missing Origin is allowed
    (non-browser clients don't send it; browsers always do on cross-origin
    requests, so this still defeats CSRF). It does not stop DNS rebinding --
    the attacker's domain is then both Origin and Host; netinfo.host_allowed
    does.
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
