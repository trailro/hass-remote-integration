"""Home Assistant ingress, when HRI runs as the Home Assistant app.

The Supervisor proxies the sidebar panel to this port with the prefix
stripped, from its own address on the hassio network, and both hops add
X-Forwarded-For.  Home Assistant's forwarded middleware answers 400 to that
from a peer that is not a trusted proxy, and HRI does not touch HA's http
settings, so a middleware placed before it takes the X-Forwarded-* headers
off a request whose TRANSPORT peer (not ``request.remote``, which a header
can move) is the Supervisor, and marks it as ingress.  The host guard and the
password check let a marked request through: Home Assistant's login is the
gate, narrowed to the HA users of ``HRI_INGRESS_USERS`` when that is set.
The Supervisor sets X-Remote-User-Id and X-Remote-User-Name from the ingress
session, and drops a client's own copy only when its name is spelled exactly
as its own.  A copy spelled another way is added after them, and the
Supervisor's aiohttp client (ClientSession._prepare_headers) merges it into
its own header, keeping the client's spelling and value: the app receives
only the client's copy.  So a user header not in the Supervisor's spelling,
or repeated, is refused, and the user name is the session's.  A session the
Supervisor opened without user data sends neither header: served unless
HRI_INGRESS_USERS is set.

Only with ``HRI_APP`` set (entrypoint.apply_app_options): on a plain Docker
install nothing is installed and every request is handled as before."""

from __future__ import annotations

import ipaddress
import logging
import os

from aiohttp import web
from multidict import CIMultiDict

_LOGGER = logging.getLogger(__name__)

SUPERVISOR_IP = "172.30.32.2"  # the Supervisor on the hassio network (supervisor/const.py DOCKER_IPV4_NETWORK_MASK[2])
FORWARDED_MIDDLEWARE = "forwarded_middleware"  # homeassistant/components/http/forwarded.py
KEY = "hri_ingress"  # request key: True on a request the Supervisor proxied
USER_KEY = "hri_ingress_user"  # the HA user name the Supervisor set (X-Remote-User-Name), "" when none
USER_HEADER = "X-Remote-User-Name"  # set by the Supervisor from the ingress session (a client's copy: see above)
USER_ID_HEADER = "X-Remote-User-Id"  # likewise; the Supervisor sends it with every session that has a user
_READ_ONLY = frozenset({"GET", "HEAD", "OPTIONS"})


def allowed_users() -> frozenset[str]:
    """HRI_INGRESS_USERS: HA user names, comma separated; empty = every HA user."""
    return frozenset(u.strip().casefold() for u in os.environ.get("HRI_INGRESS_USERS", "").split(",") if u.strip())


def from_supervisor(request: web.Request) -> bool:
    transport = request.transport
    peer = transport.get_extra_info("peername") if transport is not None else None
    if not isinstance(peer, tuple) or not peer:
        return False
    try:
        ip = ipaddress.ip_address(str(peer[0]).split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return str(ip) == SUPERVISOR_IP


def user_headers(names) -> str:
    """How the user headers came, from the header names as received: "user" when X-Remote-User-Id is there once and
    X-Remote-User-Name at most once, both in the Supervisor's spelling; "none" when neither is there in any spelling
    (a session the Supervisor opened without user data); "bad" for anything else, a client's copy among them."""
    count = {USER_HEADER: 0, USER_ID_HEADER: 0}
    for name in names:
        for own in count:
            if name.casefold() == own.casefold():
                if name != own:
                    return "bad"
                count[own] += 1
    if count == {USER_HEADER: 0, USER_ID_HEADER: 0}:
        return "none"
    return "user" if count[USER_ID_HEADER] == 1 and count[USER_HEADER] <= 1 else "bad"


def is_ingress(request: web.Request) -> bool:
    return isinstance(request, web.BaseRequest) and request.get(KEY) is True


def install_ingress(hass, csp: str) -> bool:
    """Insert the ingress middleware right before Home Assistant's forwarded middleware, when HRI_APP is set.  False
    when it is not installed: not an app, or the forwarded middleware is not where it is expected (then ingress
    requests keep getting HA's 400, and nothing is let through)."""
    if not os.environ.get("HRI_APP"):
        return False
    users = allowed_users()

    @web.middleware
    async def hri_ingress(request: web.Request, handler):
        if not from_supervisor(request):
            return await handler(request)
        shape = user_headers(k.decode("utf-8", "surrogateescape") for k, _ in request.raw_headers)
        if shape == "bad":
            _LOGGER.warning("ingress: a user header not spelled or sent as the Supervisor sends it: refused %s %s", request.method, request.path)
            return web.Response(status=403, content_type="text/plain", headers={"Content-Security-Policy": csp},
                                text="This Home Assistant user may not open hass-remote-integration (the user headers are not the Supervisor's).")
        if shape == "none" and users:
            _LOGGER.warning("ingress: no user in the ingress session: refused %s %s", request.method, request.path)
            return web.Response(status=403, content_type="text/plain", headers={"Content-Security-Policy": csp},
                                text="This Home Assistant user may not open hass-remote-integration (the app's ingress_users option).")
        user = request.headers.get(USER_HEADER, "")
        if users and user.casefold() not in users:
            _LOGGER.warning("ingress: Home Assistant user %r is not in ingress_users: refused %s %s", user, request.method, request.path)
            return web.Response(status=403, content_type="text/plain", headers={"Content-Security-Policy": csp},
                                text="This Home Assistant user may not open hass-remote-integration (the app's ingress_users option).")
        headers = CIMultiDict((k, v) for k, v in request.headers.items() if not k.lower().startswith("x-forwarded-"))
        request = request.clone(headers=headers)
        request[KEY] = True
        request[USER_KEY] = user
        if request.method not in _READ_ONLY:
            _LOGGER.info("ingress: %s %s by Home Assistant user %r", request.method, request.path, user)
        return await handler(request)

    middlewares = hass.http.app.middlewares
    index = next((i for i, m in enumerate(middlewares) if getattr(m, "__name__", "") == FORWARDED_MIDDLEWARE), None)
    if index is None:
        _LOGGER.error("ingress not enabled: Home Assistant's %s was not found among the http middlewares; "
                      "the web UI works on the app's port only", FORWARDED_MIDDLEWARE)
        return False
    try:
        middlewares.insert(index, hri_ingress)
    except Exception as err:  # noqa: BLE001 - a frozen app: ingress stays off, the port works as before
        _LOGGER.error("ingress not enabled (%s): the web UI works on the app's port only", err)
        return False
    _LOGGER.info("ingress enabled%s", f" for {len(users)} Home Assistant user(s)" if users else "")
    return True
