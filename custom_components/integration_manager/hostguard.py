"""Host-header guard against DNS rebinding.

Allowed: IP literals, localhost, the container's hostname, names under
.local/.lan/.home/.internal/.home.arpa (never resolvable from the public
DNS an attacker controls), and settings["allowed_hosts"] (comma list) for
custom names.  Everything else gets 403 with an explanation."""

from __future__ import annotations

import functools
import ipaddress
import logging
import socket

from aiohttp import web
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)
SAFE_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa", ".localdomain")
# every script is a static file (no inline script or handler); style attributes and the login page's <style> element
# remain, hence 'unsafe-inline' for styles
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


@functools.cache
def _own_hostname() -> str:
    # the container's name does not change while it runs, and gethostname is a system call on every request
    return socket.gethostname().lower().rstrip(".")


def _bare(host: str) -> str:
    """A Host value without its port and without the one trailing dot of a fully qualified name
    ("hri.local." is the same host as "hri.local", and browsers send what the user typed)."""
    h = host.strip().lower()
    if h.startswith("["):  # [v6]:port
        h = h[1:].split("]", 1)[0]
    elif h.count(":") == 1:
        h = h.split(":", 1)[0]
    return h[:-1] if h.endswith(".") else h


@functools.lru_cache(maxsize=8)
def _allowed(raw: str) -> frozenset[str]:
    # the setting is read on every request but changes only on a save: parsed once per value
    return frozenset(_bare(x) for x in raw.split(",") if x.strip())


def _host_ok(host: str, extra: set[str] | frozenset[str]) -> bool:
    h = _bare(host)
    if not h:
        return False
    if h in ("localhost", _own_hostname()) or h in extra:
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
        extra = _allowed(str(installer.settings.data.get("allowed_hosts") or ""))  # the Host header is compared without its port
        # the two refusals below answer before the handler, so they carry the policy themselves
        if not _host_ok(request.headers.get("Host", ""), extra):
            return web.Response(status=403, content_type="text/plain", headers={"Content-Security-Policy": CSP},
                                text=f"Host {request.headers.get('Host', '')!r} is not allowed (DNS rebinding guard). "
                                     "Use the IP address, a .local/.lan name, or add the name to allowed_hosts in the settings.")
        if request.path == "/api/onboarding" or request.path.startswith("/api/onboarding/"):
            # an integration that depends on frontend or panel_custom loads HA's onboarding, whose user
            # creation is open while no user exists and reads text/plain bodies (no CORS preflight)
            return web.Response(status=403, content_type="text/plain", headers={"Content-Security-Policy": CSP},
                                text="Home Assistant's onboarding is not available on this port.")
        try:
            response = await handler(request)
        except web.HTTPException as err:  # a raised 404/redirect is an answer too
            err.headers.setdefault("Content-Security-Policy", CSP)
            raise
        if isinstance(response, web.StreamResponse) and not response.prepared:
            response.headers.setdefault("Content-Security-Policy", CSP)
        return response

    try:
        hass.http.app.middlewares.append(host_guard)
    except Exception as err:  # noqa: BLE001 - a frozen app: refuse to run without the guard, as auth.py does
        # without it every page is open to DNS rebinding and HA's onboarding API is reachable: the manager's setup
        # fails, run.py exits and the container's restarts and this line are what the operator sees
        _LOGGER.error("host guard not installed (%s): the manager does not start without it", err)
        raise
