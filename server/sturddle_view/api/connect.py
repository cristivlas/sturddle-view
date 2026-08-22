"""Connect-from-mobile info: a QR code of the LAN entry URL. Served only
to loopback clients (the desktop window or a local browser), so the
token-bearing URL is never handed out over the network."""
from __future__ import annotations

import segno
from fastapi import APIRouter, Depends, Request

from ..auth import require_token
from ..netinfo import entry_url, is_loopback, lan_hosts

router = APIRouter(prefix="/connect", tags=["connect"], dependencies=[Depends(require_token)])

# Black on white regardless of UI theme: phone cameras need the contrast.
_QR_DARK = "#000"
_QR_LIGHT = "#fff"


def _client_is_local(request: Request) -> bool:
    client = request.client
    return client is not None and is_loopback(client.host)


def _qr_data_uri(url: str) -> str:
    return segno.make(url).svg_data_uri(dark=_QR_DARK, light=_QR_LIGHT)


@router.get("")
def get_connect_info(request: Request) -> dict:
    if not _client_is_local(request):
        return {"local": False}
    settings = request.app.state.settings
    hosts = lan_hosts(settings.host)
    url = entry_url(settings, hosts[0]) if hosts else None
    return {
        "local": True,
        "lan_enabled": not is_loopback(settings.host),
        "qr": _qr_data_uri(url) if url else None,
    }
