"""Catalog of the services registered in this instance, grouped by domain,
merged with what HA users see: fields/selectors/target/response from the
integration's ``services.yaml`` and names/descriptions from its
``translations/en.json``.  Used by the /services page and published to
MQTT so the consuming HA can call any of them generically.

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
from homeassistant.core import HomeAssistant
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


async def service_rows(hass: HomeAssistant) -> list[dict[str, Any]]:
    registered = hass.services.async_services()
    out: list[dict[str, Any]] = []
    for domain in sorted(registered):
        custom, yaml_desc, tr_services = False, {}, {}
        try:
            integration = await loader.async_get_integration(hass, domain)
            custom = not integration.is_built_in
            path = integration.file_path / "services.yaml"
            if path.is_file():
                yaml_desc = await hass.async_add_executor_job(_cached, str(path), load_yaml_dict) or {}
            tr_path = integration.file_path / "translations" / "en.json"
            if tr_path.is_file():
                tr = await hass.async_add_executor_job(_cached, str(tr_path), _load_json) or {}
                tr_services = tr.get("services") or {}
        except (loader.IntegrationNotFound, HomeAssistantError, OSError, ValueError):
            pass
        services = []
        for name in sorted(registered[domain]):
            desc = yaml_desc.get(name) or {}
            tr_svc = tr_services.get(name) or {}
            fields = {}
            for fname, fdesc in (desc.get("fields") or {}).items():
                tr_field = (tr_svc.get("fields") or {}).get(fname) or {}
                fields[fname] = {
                    **(fdesc or {}),
                    "name": tr_field.get("name") or (fdesc or {}).get("name"),
                    "description": tr_field.get("description") or (fdesc or {}).get("description"),
                }
            resp = desc.get("response")
            services.append(
                {
                    "name": name,
                    "title": tr_svc.get("name") or desc.get("name") or "",
                    "description": tr_svc.get("description") or desc.get("description") or "",
                    "fields": fields,
                    "target": desc.get("target"),
                    "response": None if not isinstance(resp, dict) else ("optional" if resp.get("optional") else "required"),
                }
            )
        out.append({"domain": domain, "custom": custom, "services": services})
    return out
