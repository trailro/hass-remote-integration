"""Device browser: the device registry of this instance with its entities,
how each device is announced to the consuming HA (discovery id/topic) and
the via-device tree.  ``/devices`` is the page, ``/api/devices`` the JSON."""

from __future__ import annotations

import json
from typing import Any

from aiohttp import web

from .ui import load_template, render
from homeassistant import loader
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from . import discovery as disc
from .mqtt_publisher import MqttPublisher, _json_default
from .http_util import ManagerView, with_body

DEVICES_HTML = load_template("devices")


def device_rows(hass: HomeAssistant, publisher: MqttPublisher) -> list[dict[str, Any]]:
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    area_reg = ar.async_get(hass)
    by_device: dict[str, list[er.RegistryEntry]] = {}
    for entry in ent_reg.entities.values():
        if entry.device_id:
            by_device.setdefault(entry.device_id, []).append(entry)
    rows: list[dict[str, Any]] = []
    children = list(getattr(dev_reg, "child_devices", []) or [])  # HA 2026.9+: zones etc. under a parent device
    for dev in list(dev_reg.devices) + children:  # iterating yields entries; .values()/[] are deprecated lookups
        child = disc.is_child_device(dev)
        entries = sorted(by_device.get(dev.id, []), key=lambda e: e.entity_id)
        integrations = sorted({e.platform for e in entries}) or ["—"]
        ents = []
        unavailable = 0
        for e in entries:
            st = hass.states.get(e.entity_id)
            state = st.state if st else None
            if state == "unavailable":
                unavailable += 1
            ents.append(
                {"entity_id": e.entity_id, "name": e.name or e.original_name or (st.attributes.get("friendly_name") if st else None),
                 "state": state, "unit": (st.attributes.get("unit_of_measurement") if st else e.unit_of_measurement),
                 "disabled": e.disabled}
            )
        via_id = dev.parent_device_id if child else dev.via_device_id
        via = dev_reg.async_get(via_id) if via_id else None
        area = area_reg.async_get_area(dev.area_id) if dev.area_id else None
        disc_id, block = disc.device_block(hass, dev.id, integrations[0], publisher.prefix)
        rows.append(
            {
                "id": dev.id,
                "name": dev.name_by_user or dev.name or dev.id,
                "original_name": dev.name,
                "name_by_user": dev.name_by_user,
                "child": child,
                "manufacturer": None if child else dev.manufacturer,
                "model": "child device" if child else dev.model,
                "model_id": None if child else dev.model_id,
                "serial_number": None if child else dev.serial_number,
                "sw_version": None if child else dev.sw_version,
                "hw_version": None if child else dev.hw_version,
                "identifiers": [list(i) for i in dev.identifiers],
                "identifier": ", ".join(f"{i[0]}:{i[1]}" if i[0] != integrations[0] else str(i[1]) for i in sorted(dev.identifiers)),
                "connections": [] if child else [list(c) for c in dev.connections],
                "via_device_id": via_id,
                "via_name": (via.name_by_user or via.name) if via else None,
                "area": area.name if area else None,
                "config_entries": [dev.config_entry_id] if child else sorted(dev.config_entries),
                "disabled_by": dev.disabled_by.value if dev.disabled_by else None,
                "integrations": integrations,
                "entities": ents,
                "unavailable": unavailable,
                "discovery_id": disc_id,
                "discovery_topic": publisher._discovery_topic(disc_id),
                "device_block": block,
            }
        )
    return rows


class DevicesPageView(ManagerView):
    url = "/devices"

    async def get(self, request: web.Request) -> web.Response:
        return web.Response(text=render(DEVICES_HTML, "/devices"), content_type="text/html")


class DevicesApiView(ManagerView):
    url = "/api/devices"

    def __init__(self, hass: HomeAssistant, publisher: MqttPublisher) -> None:
        self.hass = hass
        self.publisher = publisher

    async def get(self, request: web.Request) -> web.Response:
        rows = device_rows(self.hass, self.publisher)
        return web.json_response(rows, dumps=lambda o: json.dumps(o, default=_json_default))


class DeviceActionView(ManagerView):
    url = "/api/devices/{device_id}/{action}"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any], device_id: str, action: str) -> web.Response:
        reg = dr.async_get(self.hass)
        if reg.async_get(device_id) is None:
            return self.json({"ok": False, "error": "unknown device"})
        try:
            if action == "name":
                name = body.get("name")
                if name is not None and not isinstance(name, str):
                    raise ValueError("name must be a string or null")
                reg.async_update_device(device_id, name_by_user=(name.strip() or None) if name else None)
                return self.json({"ok": True})
            if action == "delete":
                dev = reg.async_get(device_id)
                removed = 0
                for entry_id in list(dev.config_entries):
                    entry = self.hass.config_entries.async_get_entry(entry_id)
                    if entry is None:
                        continue
                    try:
                        integration = await loader.async_get_integration(self.hass, entry.domain)
                        component = await integration.async_get_component()
                    except Exception:  # noqa: BLE001
                        component = None
                    hook = getattr(component, "async_remove_config_entry_device", None)
                    if hook is None:
                        return self.json({"ok": False, "error": f"{entry.domain} does not allow removing devices (no async_remove_config_entry_device), like in HA"})
                    if not await hook(self.hass, entry, dev):
                        return self.json({"ok": False, "error": f"{entry.domain} refused to remove this device"})
                    reg.async_update_device(device_id, remove_config_entry_id=entry_id)
                    removed += 1
                if reg.async_get(device_id) is not None and removed == 0:
                    reg.async_remove_device(device_id)  # orphan device without config entries
                return self.json({"ok": True, "config_entries_detached": removed})
        except Exception as err:  # noqa: BLE001
            return self.json({"ok": False, "error": f"{type(err).__name__}: {err}"})
        return self.json_message("unknown action", status_code=400)
