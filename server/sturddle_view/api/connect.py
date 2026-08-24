"""Connect-from-mobile: a QR code of the LAN entry URL, for any
authenticated client (the token in the QR is the one the caller already
holds). Desktop mode opens the LAN port on the first request (see
lan_listener)."""
from __future__ import annotations

import logging

import segno
from fastapi import APIRouter, Depends, Request

from ..auth import AUTH_DISABLED_LAN_WARNING, require_token
from ..config import WILDCARD_HOST
from ..netinfo import entry_url, is_loopback, lan_hosts

log = logging.getLogger(__name__)

router = APIRouter(prefix="/connect", tags=["connect"], dependencies=[Depends(require_token)])

# Black on white regardless of UI theme: phone cameras need the contrast.
_QR_DARK = "#000"
_QR_LIGHT = "#fff"
# Why there is no QR: server mode bound to loopback (needs a --host
# restart), no LAN address to listen on, or every LAN bind failed (logged).
REASON_LOOPBACK_BIND = "loopback_bind"
REASON_OFFLINE = "offline"
REASON_BIND_FAILED = "bind_failed"


def _qr_data_uri(url: str) -> str:
    return segno.make(url).svg_data_uri(dark=_QR_DARK, light=_QR_LIGHT)


async def _desktop_lan_hosts(listener, settings) -> list[str]:
    """Open the LAN port on demand. A loopback launch never logged the
    --no-auth warning, so it is raised here when the exposure happens."""
    before = set(listener.hosts())
    hosts = await listener.ensure()
    if settings.auth_disabled and any(h not in before for h in hosts):
        log.warning(AUTH_DISABLED_LAN_WARNING, ", ".join(hosts))
    return hosts


@router.post("/lan")
async def enable_lan(request: Request) -> dict:
    """Make the server reachable from the LAN (desktop: bind on demand)
    and return the QR code for the entry URL."""
    settings = request.app.state.settings
    listener = request.app.state.lan_listener
    if listener is not None:
        hosts = await _desktop_lan_hosts(listener, settings)
    elif is_loopback(settings.host):
        return {"qr": None, "reason": REASON_LOOPBACK_BIND}
    else:
        hosts = lan_hosts(settings.host)
    if hosts:
        return {"qr": _qr_data_uri(entry_url(settings, hosts[0])), "reason": None}
    bind_failed = listener is not None and bool(lan_hosts(WILDCARD_HOST))
    return {"qr": None, "reason": REASON_BIND_FAILED if bind_failed else REASON_OFFLINE}
