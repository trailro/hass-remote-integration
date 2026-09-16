"""Shared HTTP plumbing for every view of the manager.

* ``ManagerView``: Home Assistant's own authentication is off (the manager
  has no HA users of its own), so what guards a view is the host guard
  (hostguard.py) plus, when ``HRI_PASSWORD``/``HRI_PASSWORD_FILE`` is set,
  the manager's password middleware (auth.py) in front of every request.
  With no password set the views are open on the LAN, as an appliance.
  Named ``integration_manager:<ClassName>`` automatically.
* ``_json_object`` / ``with_body``: the JSON-body gate.  Requiring the
  application/json content type is what makes cross-site POSTs from a
  browser preflight (and fail); several endpoints install code, so this
  is security-relevant and lives in exactly one place.
"""

from __future__ import annotations

import functools
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView


class ManagerView(HomeAssistantView):
    requires_auth = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "name" not in cls.__dict__:
            cls.name = f"integration_manager:{cls.__name__}"

    def register(self, hass: Any, app: web.Application, router: web.UrlDispatcher) -> None:
        """HomeAssistantView.register without CORS: Home Assistant's
        configured origins (cast.home-assistant.io by default) must not get
        a pass to an API that installs code and does not ask HA for a
        token."""
        from homeassistant.helpers.http import request_handler_factory

        for method in ("get", "post", "delete", "put", "patch", "head", "options"):
            if not (handler := getattr(self, method, None)):
                continue
            wrapped = request_handler_factory(hass, self, handler)
            for url in [self.url, *self.extra_urls]:
                router.add_route(method, url, wrapped)


class BadRequest(Exception):
    """Raised by _json_object; views turn it into a 400."""


async def _json_object(request: web.Request) -> dict:
    """Parse a JSON object body (content type enforced, see module doc)."""
    if request.content_type != "application/json":
        raise BadRequest("Content-Type must be application/json")
    try:
        body = await request.json()
    except ValueError as err:
        raise BadRequest(f"invalid JSON: {err}") from None
    if not isinstance(body, dict):
        raise BadRequest("JSON object expected")
    return body


def _bad(view: HomeAssistantView, err: Exception) -> web.Response:
    return view.json_message(str(err), status_code=400)


def with_body(fn):
    """Decorator: parse the JSON body once and hand it to the handler as
    ``body``; a bad body answers 400 without touching the handler."""

    @functools.wraps(fn)
    async def wrapper(self, request: web.Request, *args: Any, **kwargs: Any) -> web.Response:
        try:
            body = await _json_object(request)
        except BadRequest as err:
            return _bad(self, err)
        return await fn(self, request, body, *args, **kwargs)

    return wrapper
