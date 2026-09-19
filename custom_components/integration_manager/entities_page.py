"""Entity browser: every entity of this instance, live, with what the
translator publishes for it.  ``/entities`` is the page, ``/api/entities``
the JSON behind it (same documents the MQTT publisher emits, plus the
topic and whether the domain gets HA discovery)."""

from __future__ import annotations

import json
from typing import Any

from aiohttp import web

from .ui import load_template, render
from homeassistant.core import HomeAssistant
from homeassistant.core import valid_entity_id
from homeassistant.helpers import entity_registry as er

from . import discovery as disc
from .mqtt_publisher import platform_of, MqttPublisher, _json_default
from .http_util import ManagerView, with_body

ENTITIES_HTML = load_template("entities")


def entity_rows(hass: HomeAssistant, publisher: MqttPublisher) -> list[dict[str, Any]]:
    """Every entity with a state (the publisher's document, plus topic and
    discovery flag) and every registry entry without one (disabled)."""
    compat = disc.compat_for(publisher.config.main_ha_version)  # None unless a main HA version is declared
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for state in hass.states.async_all():
        seen.add(state.entity_id)
        built = publisher.build_document(state)
        if built is None:  # excluded integration or rule: still list it, no topic
            domain, object_id = state.entity_id.split(".", 1)
            entry = er.async_get(hass).async_get(state.entity_id)
            rows.append(
                {
                    "entity_id": state.entity_id,
                    "domain": domain,
                    "object_id": object_id,
                    "integration": platform_of(hass, state.entity_id) or "unregistered",
                    "state": state.state,
                    "attributes": dict(state.attributes),
                    "name": ((entry.name or entry.original_name) if entry else None) or state.attributes.get("friendly_name"),
                    "name_override": entry.name if entry else None,
                    "unique_id": entry.unique_id if entry else None,
                    "disabled": bool(entry and entry.disabled),
                    "last_updated": state.last_updated.isoformat(),
                    "last_reported": state.last_reported.isoformat(),
                    "mqtt_topic": None,
                    "discovery": False,
                    "mqtt_rule": publisher.rules.for_entity(state.entity_id),
                }
            )
            continue
        topic, doc = built
        entry = er.async_get(hass).async_get(state.entity_id)
        doc["name_override"] = entry.name if entry else None
        doc["disabled"] = bool(entry and entry.disabled)
        if not doc.get("name"):  # zone climates carry the name only as friendly_name
            doc["name"] = state.attributes.get("friendly_name")
        doc["mqtt_topic"] = topic
        # the same decision the publisher makes, main_ha_version included: a domain whose MQTT platform the
        # declared main Home Assistant does not have is published as a sensor mirror, and the page has to say
        # so - otherwise it marks an entity as needing a newer main HA that the setting has already handled
        doc["discovery"] = ("native" if doc["domain"] in disc.NATIVE
                            and (compat is None or compat.knows_platform(doc["domain"])) else "mirror")
        doc.setdefault("mqtt_rule", {})
        rows.append(doc)

    for entry in er.async_get(hass).entities.values():
        if entry.entity_id in seen:
            continue
        rows.append(
            {
                "entity_id": entry.entity_id,
                "domain": entry.domain,
                "object_id": entry.entity_id.split(".", 1)[1],
                "integration": entry.platform,
                "state": None,
                "attributes": {},
                "name": entry.name or entry.original_name,
                "original_name": entry.original_name,
                "unique_id": entry.unique_id,
                "name_override": entry.name,
                "disabled": entry.disabled,
                "last_updated": None,
                "last_reported": None,
                "mqtt_topic": None,
                "discovery": False,
            }
        )
    return rows


class EntitiesPageView(ManagerView):
    url = "/entities"

    async def get(self, request: web.Request) -> web.Response:
        return web.Response(text=render(ENTITIES_HTML, "/entities"), content_type="text/html")


class EntitiesApiView(ManagerView):
    url = "/api/entities"

    def __init__(self, hass: HomeAssistant, publisher: MqttPublisher) -> None:
        self.hass = hass
        self.publisher = publisher

    async def get(self, request: web.Request) -> web.Response:
        rows = entity_rows(self.hass, self.publisher)
        return web.json_response(rows, dumps=lambda o: json.dumps(o, default=_json_default))


class EntityActionView(ManagerView):
    """Registry edits the HA frontend would do: rename the entity id, set a
    custom name, enable/disable, delete.  All through the entity registry
    in-process, so the MQTT translator sees the registry events and updates
    the consuming HA (old id gets the removal form)."""

    url = "/api/entities/{entity_id}/{action}"

    def __init__(self, hass: HomeAssistant, publisher: MqttPublisher) -> None:
        self.hass = hass
        self.publisher = publisher

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any], entity_id: str, action: str) -> web.Response:
        reg = er.async_get(self.hass)
        entry = reg.async_get(entity_id)
        if entry is None:
            return self.json({"ok": False, "error": f"{entity_id} is not in the entity registry"})
        try:
            if action == "rename":
                new_id = str(body.get("new_entity_id", "")).strip().lower()
                if not valid_entity_id(new_id):
                    raise ValueError(f"invalid entity id: {new_id!r}")
                if new_id.split(".", 1)[0] != entry.domain:
                    raise ValueError("the domain cannot change")
                if new_id != entity_id and (reg.async_get(new_id) or self.hass.states.get(new_id)):
                    raise ValueError(f"{new_id} already exists")
                if new_id != entity_id:
                    reg.async_update_entity(entity_id, new_entity_id=new_id)
                return self.json({"ok": True, "entity_id": new_id})
            if action == "name":
                name = body.get("name")
                if name is not None and not isinstance(name, str):
                    raise ValueError("name must be a string or null")
                reg.async_update_entity(entity_id, name=(name.strip() or None) if name else None)
                return self.json({"ok": True, "entity_id": entity_id})
            if action == "disable":
                reg.async_update_entity(entity_id, disabled_by=er.RegistryEntryDisabler.USER)
                return self.json({"ok": True, "entity_id": entity_id})
            if action == "enable":
                reg.async_update_entity(entity_id, disabled_by=None)
                return self.json({"ok": True, "entity_id": entity_id, "note": "the integration adds it back at its next reload/restart"})
            if action == "delete":
                reg.async_remove(entity_id)
                return self.json({"ok": True, "entity_id": entity_id})
            if action in ("mqtt_exclude", "mqtt_include", "mqtt_name"):
                # only what is PUBLISHED changes; the entity itself is untouched
                rules = self.publisher.rules
                if action == "mqtt_name":
                    name = body.get("name")
                    if name is not None and not isinstance(name, str):
                        raise ValueError("name must be a string or null")
                # mutate on the loop (the publisher reads the rules there), copied there and written by the ordered writer
                if action == "mqtt_name":
                    rule = rules.set(entity_id, name=(name.strip() or None) if name else None)
                elif action == "mqtt_exclude":
                    rule = rules.set(entity_id, exclude=True)
                else:
                    # a glob may still exclude it: then store an explicit exclude=false override
                    rule = rules.set(entity_id, exclude=None)
                    if rules.for_entity(entity_id).get("exclude"):
                        rule = rules.set(entity_id, exclude=False)
                await rules.async_save()
                res = await self.publisher.async_apply_rules()
                return self.json({"ok": True, "entity_id": entity_id, "mqtt_rule": rule, **res})
        except ValueError as err:
            return self.json({"ok": False, "error": str(err)})
        except Exception as err:  # noqa: BLE001 - registry raises plain exceptions on conflicts
            return self.json({"ok": False, "error": f"{type(err).__name__}: {err}"})
        return self.json_message("unknown action", status_code=400)
