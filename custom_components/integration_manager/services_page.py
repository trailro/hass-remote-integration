"""Service browser: every service registered in this instance, grouped by
integration, with the descriptions/fields HA reads from services.yaml.
``/services`` is the page, ``/api/services`` the JSON behind it."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import re
from typing import Any

from aiohttp import web

from .http_util import BadRequest, ManagerView, _bad, _json_object
from .ui import load_template, render
from homeassistant.core import HomeAssistant, SupportsResponse

from .mqtt_publisher import CALL_DENY_DOMAINS, CALL_TIMEOUT_S, CALLS_IN_FLIGHT_MAX, _json_default
from .services_catalog import service_rows

_LOGGER = logging.getLogger(__name__)

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
    service supports one), bounded like the MQTT path."""

    url = "/api/services/call"

    # A timeout does not end the call: bounding the request bounds how long a browser waits, not how many
    # calls are still running behind it, and a client that retries would pile them up as over MQTT.
    _in_flight = 0

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request: web.Request) -> web.Response:

        try:
            body = await _json_object(request)
        except BadRequest as err:
            return _bad(self, err)
        # Home Assistant looks services up in lower case, so the name checks and the deny list do too (as over MQTT)
        domain, service = str(body.get("domain", "")).lower(), str(body.get("service", "")).lower()
        data, target = body.get("data") or {}, body.get("target")
        if not re.fullmatch(r"[a-z0-9_]+", domain) or not re.fullmatch(r"[a-z0-9_]+", service):
            return self.json({"ok": False, "error": "domain and service required"})
        if not isinstance(data, dict) or (target is not None and not isinstance(target, dict)):
            return self.json({"ok": False, "error": "data and target must be JSON objects"})
        if domain in CALL_DENY_DOMAINS:
            return self.json({"ok": False, "error": f"{domain}.* is not callable from here (would restart/stop this process or run arbitrary code)"})
        if not self.hass.services.has_service(domain, service):
            return self.json({"ok": False, "error": f"{domain}.{service} is not registered"})
        if type(self)._in_flight >= CALLS_IN_FLIGHT_MAX:
            return self.json({"ok": False, "error": f"too many calls in progress ({CALLS_IN_FLIGHT_MAX}): try again later"})
        supports = self.hass.services.supports_response(domain, service)
        return_response = supports is not SupportsResponse.NONE
        t0 = time.monotonic()
        # Not wait_for(): cancelling a service handler that shields or swallows CancelledError would hang
        # the timeout itself.  The call keeps running; the request gets a timeout now and the page stays
        # usable, and the real outcome is logged when it finally returns.
        task = self.hass.async_create_task(
            self.hass.services.async_call(domain, service, data, blocking=True, target=target or None,
                                          return_response=return_response))
        type(self)._in_flight += 1
        task.add_done_callback(self._call_done(domain, service))
        finished, _ = await asyncio.wait({task}, timeout=CALL_TIMEOUT_S)
        if not finished:
            _LOGGER.warning("%s.%s from /services timed out after %ss", domain, service, CALL_TIMEOUT_S)
            return self.json({"ok": False, "error": f"timeout after {CALL_TIMEOUT_S}s (service still running)"})
        try:
            resp = await task
        except Exception as err:  # noqa: BLE001 - validation errors and integration errors alike go to the UI
            return self.json({"ok": False, "error": f"{type(err).__name__}: {err}"})
        out: dict = {"ok": True, "ms": int((time.monotonic() - t0) * 1000)}
        if return_response:
            out["response"] = resp
        return web.json_response(out, dumps=lambda o: json.dumps(o, default=_json_default))

    @classmethod
    def _call_done(cls, domain: str, service: str) -> Any:
        """Frees the slot whenever the call ends, however late, and keeps a
        timed-out call's exception from being reported as never retrieved."""

        def done(task: asyncio.Task) -> None:
            cls._in_flight -= 1
            if not task.cancelled() and (err := task.exception()) is not None:
                _LOGGER.warning("%s.%s from /services failed: %s", domain, service, err)

        return done
