"""Host-header guard against DNS rebinding.

Allowed: IP literals, localhost, the container's hostname, names under
.local/.lan/.home/.internal/.home.arpa (never resolvable from the public
DNS an attacker controls), and settings["allowed_hosts"] (comma list) for
custom names.  Everything else gets 403 with an explanation."""

from __future__ import annotations

import ipaddress
import re
import logging
import socket

from aiohttp import web
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)
SAFE_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa", ".localdomain")


def _host_ok(host: str, extra: set[str]) -> bool:
    h = host.strip().lower()
    if h.startswith("["):  # [v6]:port
        h = h[1:].split("]", 1)[0]
    elif h.count(":") == 1:
        h = h.split(":", 1)[0]
    if not h:
        return False
    if h in ("localhost", socket.gethostname().lower()) or h in extra:
        return True
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    return h.endswith(SAFE_SUFFIXES)


def install_host_guard(hass: HomeAssistant, installer) -> None:
    @web.middleware
    async def host_guard(request: web.Request, handler):
        extra = {re.sub(r":\d+$", "", x.strip().lower()) for x in str(installer.settings.data.get("allowed_hosts") or "").split(",") if x.strip()}  # the Host header is compared without its port
        if not _host_ok(request.headers.get("Host", ""), extra):
            return web.Response(status=403, content_type="text/plain",
                                text=f"Host {request.headers.get('Host', '')!r} is not allowed (DNS rebinding guard). "
                                     "Use the IP address, a .local/.lan name, or add the name to allowed_hosts in the settings.")
        return await handler(request)

    try:
        hass.http.app.middlewares.append(host_guard)
    except Exception as err:  # noqa: BLE001 - frozen app (should not happen at setup time)
        _LOGGER.error("host guard not installed: %s", err)
