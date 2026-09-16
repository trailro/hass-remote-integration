"""Per-entity MQTT translation rules, ``integration_manager/mqtt_rules.json``:

    {"rules": {"<entity_id or glob>": {"exclude": true,
                                       "name": "Living room",
                                       "enabled_by_default": false,
                                       "entity_category": "diagnostic",
                                       "device_class": "temperature",
                                       "icon": "mdi:radiator"}}}

They change only what is published: the entity stays as it is in this
container's HA.  Exact entity ids win over globs; globs apply in sorted
order (later overrides earlier)."""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re

from typing import Any

from . import writer

FIELDS = ("exclude", "name", "enabled_by_default", "entity_category", "device_class", "icon")


class MqttRules:
    def __init__(self, path: str) -> None:
        self.path = path
        self.rules: dict[str, dict[str, Any]] = {}
        # (the rules dict they were taken from, its glob patterns in the order they apply): for_entity runs for every
        # entity on every republish, and sorting the patterns each time costs more than the matching
        self._globs: tuple[dict[str, dict[str, Any]], list[str]] | None = None
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
            rules = raw.get("rules") if isinstance(raw, dict) else None
        except (OSError, ValueError) as err:
            if os.path.exists(self.path):
                logging.getLogger(__name__).error("mqtt_rules.json unreadable, no rules applied: %s", err)
            self.rules = {}
            return
        out = {}
        for k, v in (rules or {}).items():
            try:
                if isinstance(v, dict):
                    out[str(k)] = self.clean(v)
            except ValueError as err:
                logging.getLogger(__name__).error("mqtt rule %r ignored: %s", k, err)
        self.rules = out

    async def async_save(self) -> None:
        """The rules as they are now (copied on the loop, where they change), written by the ordered writer."""
        await writer.async_write(self.path, {"rules": self.rules}, indent=1, sort_keys=True)

    @staticmethod
    def clean(rule: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k in FIELDS:
            if k not in rule or rule[k] is None or rule[k] == "":
                continue
            v = rule[k]
            if k in ("exclude", "enabled_by_default"):
                if not isinstance(v, bool):
                    raise ValueError(f"{k} must be true/false")
            elif not isinstance(v, str) or len(v) > 120:
                raise ValueError(f"{k} must be a short string")
            if k == "entity_category" and v not in ("config", "diagnostic"):
                raise ValueError("entity_category must be config or diagnostic")
            if k == "icon" and ":" not in v:
                raise ValueError("icon must look like mdi:name")
            if k == "device_class" and not re.fullmatch(r"[a-z_]{1,40}", v):
                raise ValueError("device_class must be a lowercase identifier")
            out[k] = v
        return out

    def replace_all(self, rules: dict[str, Any]) -> None:
        if not isinstance(rules, dict):
            raise ValueError("rules must be an object")
        new = {}
        for pattern, rule in rules.items():
            if not isinstance(pattern, str) or not pattern or len(pattern) > 200 or not isinstance(rule, dict):
                raise ValueError(f"bad rule for {pattern!r}")
            cleaned = self.clean(rule)
            if cleaned:
                new[pattern] = cleaned
        self.rules = new

    def set(self, entity_id: str, **changes: Any) -> dict[str, Any]:
        """Update the exact-id rule of one entity (None removes a field)."""
        cur = dict(self.rules.get(entity_id) or {})
        for k, v in changes.items():
            if v is None:
                cur.pop(k, None)
            else:
                cur[k] = v
        cur = self.clean(cur)
        if cur:
            self.rules[entity_id] = cur
        else:
            self.rules.pop(entity_id, None)
        self._globs = None  # changed in place: the identity check in for_entity does not see it
        return cur

    def for_entity(self, entity_id: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self._globs is None or self._globs[0] is not self.rules:
            self._globs = (self.rules, [p for p in sorted(self.rules) if any(ch in p for ch in "*?[")])
        for pattern in self._globs[1]:
            if pattern != entity_id and fnmatch.fnmatchcase(entity_id, pattern):
                out.update(self.rules[pattern])
        if entity_id in self.rules:
            out.update(self.rules[entity_id])
        return out

    def apply_component(self, comp: dict[str, Any], rule: dict[str, Any]) -> dict[str, Any]:
        if rule.get("name"):
            comp["name"] = rule["name"]
        if rule.get("enabled_by_default") is False:
            comp["enabled_by_default"] = False
        elif rule.get("enabled_by_default") is True:
            comp.pop("enabled_by_default", None)  # an exact-id rule re-enabling over a glob
        for k in ("entity_category", "device_class", "icon"):
            if rule.get(k):
                comp[k] = rule[k]
        return comp
