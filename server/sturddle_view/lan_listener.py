"""On-demand LAN listening for desktop mode.

The desktop server binds loopback at launch. Listening on a LAN address
is what makes Windows Firewall prompt, so that happens only when the user
opens About -> Connect from mobile, and lasts for that session."""
from __future__ import annotations

import asyncio
import logging
import os
import socket

import uvicorn

from .config import WILDCARD_HOST
from .netinfo import is_loopback, lan_hosts

log = logging.getLogger(__name__)


class LanListener:
    def __init__(self, server: uvicorn.Server, bind: str) -> None:
        self._server = server
        self._bind = bind
        self._hosts: list[str] = []
        self._lock = asyncio.Lock()

    def hosts(self) -> list[str]:
        """LAN addresses listened on so far (the bind's own when not loopback)."""
        if not is_loopback(self._bind):
            return lan_hosts(self._bind)
        return list(self._hosts)

    async def ensure(self) -> list[str]:
        """Listen on every current LAN address not yet bound (addresses come
        and go with Wi-Fi). Returns the current ones actually listened on,
        primary first; empty when offline or every bind failed (logged)."""
        if not is_loopback(self._bind):
            return lan_hosts(self._bind)
        async with self._lock:
            wanted = lan_hosts(WILDCARD_HOST)
            for host in wanted:
                if host not in self._hosts and await self._listen(host):
                    self._hosts.append(host)
            return [h for h in wanted if h in self._hosts]

    async def _listen(self, host: str) -> bool:
        server = self._server
        config = server.config
        port = config.port
        sock = _bind_socket(host, port)
        if sock is None:
            return False

        # Mirrors the closure uvicorn builds in Server.startup().
        def create_protocol(_loop: asyncio.AbstractEventLoop | None = None) -> asyncio.Protocol:
            return config.http_protocol_class(
                config=config, server_state=server.server_state,
                app_state=server.lifespan.state, _loop=_loop,
            )

        try:
            listening = await asyncio.get_running_loop().create_server(
                create_protocol, sock=sock, ssl=config.ssl, backlog=config.backlog,
            )
        except OSError:
            log.error("LAN listen %s:%d failed", host, port, exc_info=True)
            sock.close()
            return False
        # On the server's own list so its shutdown closes this one too.
        server.servers.append(listening)
        log.info("listening on %s:%d (LAN)", host, port)
        return True


def _bind_socket(host: str, port: int) -> socket.socket | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # SO_REUSEADDR as uvicorn does -- except on Windows, where it lets a
    # bind steal an actively listening port.
    if os.name != "nt":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError:
        log.error("LAN bind %s:%d failed", host, port, exc_info=True)
        sock.close()
        return None
    return sock
