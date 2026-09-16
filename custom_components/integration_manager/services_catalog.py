"""Catalog of the services registered in this instance, grouped by domain,
merged with what HA users see: fields/selectors/target from the
integration's ``services.yaml``, names/descriptions from its
``translations/en.json`` and the response support from the registration.
Used by the /services page and published to MQTT so the consuming HA can
call any of them generically.

The yaml is read directly, NOT through HA's async_get_all_descriptions:
that validates "supported_features" filters by importing every base
component (ai_task -> conversation -> hassil, numpy, ...), none of which
this slim image carries.
"""

from __future__ import annotations

import json
import os
from typing import Any

from homeassistant import loader
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.yaml import load_yaml_dict


def _load_json(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


_FILE_CACHE: dict[str, tuple[int, Any]] = {}  # path -> (mtime_ns, parsed)


def _cached(path: str, loader_fn) -> Any:
    """Parse a file once per mtime (services.yaml / translations are read
    on every full republish and every /api/services)."""
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        return None
    hit = _FILE_CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    data = loader_fn(path)
    _FILE_CACHE[path] = (mtime, data)
    return data


def _optional(path: str, loader_fn) -> dict[str, Any]:
    """Blocking: a file an integration may not ship ({} when missing or not a mapping)."""
    try:
        data = _cached(path, loader_fn)
    except OSError:
        return {}
    return data if isinstance(data, dict) else {}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _flat_fields(desc: dict[str, Any], tr_svc: dict[str, Any]) -> dict[str, Any]:
    """Fields of a service, those inside sections (light.turn_on's
    advanced_fields) listed like the others, as HA's own loader does."""
    tr_fields = _dict(tr_svc.get("fields"))
    fields: dict[str, Any] = {}
    for fname, fdesc in _dict(desc.get("fields")).items():
        fdesc = _dict(fdesc)
        if isinstance(fdesc.get("fields"), dict):  # a section
            fields.update(_flat_fields({"fields": fdesc["fields"]}, tr_svc))
            continue
        tr_field = _dict(tr_fields.get(fname))
        fields[fname] = {
            **fdesc,
            "name": tr_field.get("name") or fdesc.get("name"),
            "description": tr_field.get("description") or fdesc.get("description"),
        }
    return fields


def _response(supports: SupportsResponse, desc: dict[str, Any]) -> str | None:
    """How the service answers: "optional", "required", or None for no
    response data at all.

    ``supports_response`` is an argument of ``hass.services.async_register``,
    not a key of services.yaml: HA injects it into the descriptions it builds
    (helpers/service.py), and across core only knx writes ``response:`` in its
    yaml.  Reading the yaml alone published every response-capable service as
    None, so the registry decides here too, as it does on the call path; an
    explicit yaml declaration still wins, for an integration that ships one."""
    yaml_resp = desc.get("response")
    if isinstance(yaml_resp, dict):
        return "optional" if yaml_resp.get("optional") else "required"
    if supports == SupportsResponse.OPTIONAL:
        return "optional"
    if supports == SupportsResponse.ONLY:
        return "required"
    return None


async def service_rows(hass: HomeAssistant) -> list[dict[str, Any]]:
    registered = hass.services.async_services()
    out: list[dict[str, Any]] = []
    for domain in sorted(registered):
        custom, yaml_desc, tr_services = False, {}, {}
        try:
            integration = await loader.async_get_integration(hass, domain)
            custom = not integration.is_built_in
            yaml_desc = await hass.async_add_executor_job(_optional, str(integration.file_path / "services.yaml"), load_yaml_dict)
            tr = await hass.async_add_executor_job(_optional, str(integration.file_path / "translations" / "en.json"), _load_json)
            tr_services = _dict(tr.get("services"))
        except (loader.IntegrationNotFound, HomeAssistantError, OSError, ValueError):
            pass
        services = []
        for name in sorted(registered[domain]):
            desc = _dict(yaml_desc.get(name))
            tr_svc = _dict(tr_services.get(name))
            fields = _flat_fields(desc, tr_svc)
            services.append(
                {
                    "name": name,
                    "title": tr_svc.get("name") or desc.get("name") or "",
                    "description": tr_svc.get("description") or desc.get("description") or "",
                    "fields": fields,
                    "target": desc.get("target"),
                    "response": _response(hass.services.supports_response(domain, name), desc),
                }
            )
        out.append({"domain": domain, "custom": custom, "services": services})
    return out
