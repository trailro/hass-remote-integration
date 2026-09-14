"""Service browser: every service registered in this instance, grouped by
integration, with the descriptions/fields HA reads from services.yaml.
``/services`` is the page, ``/api/services`` the JSON behind it."""

from __future__ import annotations

import json
import time
import re
from typing import Any

from aiohttp import web

from .http_util import BadRequest, ManagerView, _bad, _json_object
from .ui import load_template, render
from homeassistant.core import HomeAssistant, SupportsResponse

from .mqtt_publisher import CALL_DENY_DOMAINS, _json_default
from .services_catalog import service_rows

SERVICES_HTML = load_template("services")


class ServicesPageView(ManagerView):
    url = "/services"

    async def get(self, request: web.Request) -> web.Response:
        return web.Response(text=render(SERVICES_HTML, "/services"), content_type="text/html")


class ServicesApiView(ManagerView):
    url = "/api/services"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        rows = [r for r in await service_rows(self.hass) if r["domain"] not in CALL_DENY_DOMAINS]
        return web.json_response(rows, dumps=lambda o: json.dumps(o, default=_json_default))


class ServiceCallView(ManagerView):
    """POST /api/services/call {domain, service, data?, target?}: run a
    service in this container's HA (blocking, response returned when the
    service supports one)."""

    url = "/api/services/call"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request: web.Request) -> web.Response:

        try:
            body = await _json_object(request)
        except BadRequest as err:
            return _bad(self, err)
        domain, service = str(body.get("domain", "")), str(body.get("service", ""))
        data, target = body.get("data") or {}, body.get("target")
        if not re.fullmatch(r"[a-z0-9_]+", domain) or not re.fullmatch(r"[a-z0-9_]+", service):
            return self.json({"ok": False, "error": "domain and service required"})
        if not isinstance(data, dict) or (target is not None and not isinstance(target, dict)):
            return self.json({"ok": False, "error": "data and target must be JSON objects"})
        if domain in CALL_DENY_DOMAINS:
            return self.json({"ok": False, "error": f"{domain}.* is not callable from here (would restart/stop this process or run arbitrary code)"})
        if not self.hass.services.has_service(domain, service):
            return self.json({"ok": False, "error": f"{domain}.{service} is not registered"})
        supports = self.hass.services.supports_response(domain, service)
        return_response = supports is not SupportsResponse.NONE
        t0 = time.monotonic()
        try:
            resp = await self.hass.services.async_call(domain, service, data, blocking=True, target=target or None,
                                                       return_response=return_response)
        except Exception as err:  # noqa: BLE001 - validation errors and integration errors alike go to the UI
            return self.json({"ok": False, "error": f"{type(err).__name__}: {err}"})
        out: dict = {"ok": True, "ms": int((time.monotonic() - t0) * 1000)}
        if return_response:
            out["response"] = resp
        return web.json_response(out, dumps=lambda o: json.dumps(o, default=_json_default))
