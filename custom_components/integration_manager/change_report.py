"""What a version switch changed for the consuming side.

When the running integration switches versions, ``snapshot`` records its
entities and services just before the switch (installer.start).  Once the
new version runs, after a passing smoke test (or DELAY_S after the start
when the smoke test is off), ``build`` compares that with what the new
version provides: entities added, removed or renamed (same unique id, new
entity id), entities whose unit, device class, state class or category
changed, services and service fields added or removed.  Removed, renamed or
changed entities and removed services or fields are what break automations
on the consuming side: they raise a notification.  The last MAX_REPORTS
reports are kept in ``integration_manager/change_reports.json`` and shown on
the Integration page (``GET /api/change_reports``); each one is in the
timeline.
"""

from __future__ import annotations

import os
import time
from typing import Any

from aiohttp import web
from homeassistant.components import persistent_notification as pn
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import async_get_platforms
from jsonio import read_json, write_json

from .http_util import ManagerView

FILE = "change_reports.json"
MAX_REPORTS = 10
DELAY_S = 120
ENTITY_KEYS = ("unit_of_measurement", "device_class", "state_class", "entity_category")


TARGET_KEYS = frozenset({"entity_id", "device_id", "area_id", "floor_id", "label_id", "metadata"})  # added by Home Assistant to entity services


def _schema_keys(schema: Any, depth: int = 0) -> list[str]:
    """The service's own field names: cv.make_entity_service_schema nests
    Schema(All(Schema({fields}), check)), plain services use Schema({fields})."""
    if schema is None or depth > 6:
        return []
    keys: set[str] = set()
    inner = getattr(schema, "schema", None)
    if isinstance(inner, dict):
        keys.update(str(getattr(k, "schema", k)) for k in inner)
    elif inner is not None and inner is not schema:
        keys.update(_schema_keys(inner, depth + 1))
    for validator in getattr(schema, "validators", None) or ():
        keys.update(_schema_keys(validator, depth + 1))
    return sorted(keys - TARGET_KEYS)


def snapshot(hass: HomeAssistant, domain: str) -> dict[str, Any]:
    """The entities and services ``domain`` provides right now (event loop)."""
    entities: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for entry in er.async_get(hass).entities.values():
        if entry.platform != domain:
            continue
        state = hass.states.get(entry.entity_id)
        if state is None or state.attributes.get("restored"):
            continue  # disabled, or a registry entry the running code no longer provides
        entities[f"uid:{entry.unique_id}"] = {
            "entity_id": entry.entity_id,
            "name": entry.name or entry.original_name or state.attributes.get("friendly_name"),
            "unit_of_measurement": state.attributes.get("unit_of_measurement"),
            "device_class": entry.device_class or entry.original_device_class or state.attributes.get("device_class"),
            "state_class": state.attributes.get("state_class"),
            "entity_category": entry.entity_category.value if entry.entity_category else None,
        }
        seen.add(entry.entity_id)
    for platform in async_get_platforms(hass, domain):
        for entity_id in platform.entities:
            state = hass.states.get(entity_id)
            if entity_id in seen or state is None:
                continue
            entities[f"eid:{entity_id}"] = {"entity_id": entity_id, "name": state.attributes.get("friendly_name"),
                                            **{k: state.attributes.get(k) for k in ENTITY_KEYS}}
            seen.add(entity_id)
    services = {name: _schema_keys(getattr(svc, "schema", None)) for name, svc in hass.services.async_services_for_domain(domain).items()}
    return {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "entities": entities, "services": services}


def build(pending: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before = pending.get("before") or {}
    b, a = before.get("entities") or {}, after.get("entities") or {}
    bs, as_ = before.get("services") or {}, after.get("services") or {}
    both = sorted(set(a) & set(b))
    changed = []
    for key in both:
        diffs = {k: [b[key].get(k), a[key].get(k)] for k in ENTITY_KEYS if b[key].get(k) != a[key].get(k)}
        if diffs:
            changed.append({"entity_id": a[key]["entity_id"], "changes": diffs})
    common = sorted(set(as_) & set(bs))
    report = {
        "domain": pending.get("domain"), "from_tag": pending.get("from_tag"), "to_tag": pending.get("to_tag"),
        "switched_at": pending.get("at"), "at": after.get("at"),
        "entities_before": len(b), "entities_after": len(a), "services_before": len(bs), "services_after": len(as_),
        "entities_added": sorted((a[k] for k in set(a) - set(b)), key=lambda e: e["entity_id"]),
        "entities_removed": sorted((b[k] for k in set(b) - set(a)), key=lambda e: e["entity_id"]),
        "entities_renamed": [{"from": b[k]["entity_id"], "to": a[k]["entity_id"]} for k in both if b[k]["entity_id"] != a[k]["entity_id"]],
        "entities_changed": changed,
        "services_added": sorted(set(as_) - set(bs)),
        "services_removed": sorted(set(bs) - set(as_)),
        "fields_added": [{"service": s, "fields": sorted(set(as_[s]) - set(bs[s]))} for s in common if set(as_[s]) - set(bs[s])],
        "fields_removed": [{"service": s, "fields": sorted(set(bs[s]) - set(as_[s]))} for s in common if set(bs[s]) - set(as_[s])],
    }
    report["breaking"] = bool(report["entities_removed"] or report["entities_renamed"] or changed
                              or report["services_removed"] or report["fields_removed"])
    return report


def summary(r: dict[str, Any]) -> str:
    entities = ", ".join(f"{len(r[k])} {label}" for k, label in (("entities_added", "added"), ("entities_removed", "removed"),
                                                                  ("entities_renamed", "renamed"), ("entities_changed", "changed")))
    services = f"services +{len(r['services_added'])} -{len(r['services_removed'])}"
    if r["fields_removed"]:
        services += f", fields removed in {len(r['fields_removed'])}"
    return f"{r['domain']} {r['from_tag']} → {r['to_tag']}: entities {entities}; {services}"


def _path(state_dir: str) -> str:
    return os.path.join(state_dir, FILE)


def load(state_dir: str) -> list[dict[str, Any]]:
    data = read_json(_path(state_dir), [])
    return data if isinstance(data, list) else []


def store(state_dir: str, report: dict[str, Any]) -> None:
    write_json(_path(state_dir), [report, *load(state_dir)][:MAX_REPORTS], fsync=False)


def notify(hass: HomeAssistant, report: dict[str, Any]) -> None:
    nid = f"integration_manager_changes_{report['domain']}"
    if not report["breaking"]:
        pn.async_dismiss(hass, nid)
        return
    lines = []
    for key, label in (("entities_removed", "removed"), ("entities_renamed", "renamed"), ("entities_changed", "changed")):
        items = report[key]
        if items:
            names = [i.get("entity_id") or f"{i['from']} → {i['to']}" for i in items[:5]]
            lines.append(f"- {len(items)} entities {label}: {', '.join(names)}" + (", …" if len(items) > 5 else ""))
    if report["services_removed"]:
        lines.append(f"- services removed: {', '.join(report['services_removed'][:5])}")
    if report["fields_removed"]:
        lines.append("- service fields removed: " + "; ".join(f"{f['service']} ({', '.join(f['fields'])})" for f in report["fields_removed"][:5]))
    pn.async_create(hass, f"{report['domain']} {report['from_tag']} → {report['to_tag']}\n" + "\n".join(lines)
                    + "\n\nAutomations in the consuming Home Assistant may use these; the Integration page lists every change.",
                    title=f"{report['domain']}: entities or services changed", notification_id=nid)


class ChangeReportsView(ManagerView):
    url = "/api/change_reports"

    def __init__(self, hass: HomeAssistant, installer: Any) -> None:
        self.hass = hass
        self.installer = installer

    async def get(self, request: web.Request) -> web.Response:
        reports = await self.hass.async_add_executor_job(load, self.installer.state_dir)
        pend = self.installer.state.pending_change
        return self.json({"reports": reports,
                          "pending": {k: pend.get(k) for k in ("domain", "from_tag", "to_tag", "at")} if isinstance(pend, dict) else None})
