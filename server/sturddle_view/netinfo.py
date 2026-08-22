"""Addresses this server is reachable on, and the entry URL for each."""
from __future__ import annotations

import ipaddress
import socket

import psutil

from .config import LOOPBACK_HOST, WILDCARD_HOST, Settings

_WILDCARD_BINDS = (WILDCARD_HOST, "::", "")
_LOCALHOST = "localhost"
_LINK_LOCAL_PREFIX = "169.254."
_AF_INET_NAME = "AF_INET"
# RFC 5737 TEST-NET-1: resolves via the default route. A UDP connect()
# sends nothing; it only asks the OS which interface it would use.
_ROUTE_PROBE_ADDR = "192.0.2.1"
_ROUTE_PROBE_PORT = 9
_SCHEME_HTTP = "http"
_SCHEME_HTTPS = "https"
_ENTRY_PATH_NO_AUTH = "/"


def is_loopback(host: str) -> bool:
    if host == _LOCALHOST:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    # Unwrap ::ffff:127.0.0.1 (a `--host ::` bind); pre-3.13 is_loopback
    # does not look through the IPv4 mapping itself.
    return (getattr(addr, "ipv4_mapped", None) or addr).is_loopback


def reachable_hosts(bind: str) -> list[str]:
    """For wildcard binds, non-loopback IPv4 addresses on up interfaces
    plus loopback. For specific binds, just the bind address."""
    if bind not in _WILDCARD_BINDS:
        return [bind]

    stats = psutil.net_if_stats()
    candidates: list[str] = []
    for name, addrs in psutil.net_if_addrs().items():
        nic = stats.get(name)
        if nic is None or not nic.isup:
            continue
        for a in addrs:
            ip = a.address
            if a.family.name != _AF_INET_NAME:
                continue
            if not ip or is_loopback(ip) or ip.startswith(_LINK_LOCAL_PREFIX):
                continue
            if ip not in candidates:
                candidates.append(ip)
    candidates.append(LOOPBACK_HOST)
    return candidates


def primary_lan_ip() -> str | None:
    """Address of the default-route interface; None when offline."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((_ROUTE_PROBE_ADDR, _ROUTE_PROBE_PORT))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def lan_hosts(bind: str) -> list[str]:
    """Addresses another device can use, default-route interface first.
    Empty for loopback binds or when offline."""
    hosts = [h for h in reachable_hosts(bind) if not is_loopback(h)]
    primary = primary_lan_ip()
    if primary in hosts:
        hosts.remove(primary)
        hosts.insert(0, primary)
    return hosts


def url_scheme(settings: Settings) -> str:
    return _SCHEME_HTTPS if settings.tls_cert else _SCHEME_HTTP


def entry_url(settings: Settings, host: str) -> str:
    """URL a fresh client opens: the /auth handshake carrying the token,
    or the bare root when auth is disabled."""
    path = _ENTRY_PATH_NO_AUTH if settings.auth_disabled else f"/auth?token={settings.token}"
    return f"{url_scheme(settings)}://{host}:{settings.port}{path}"
