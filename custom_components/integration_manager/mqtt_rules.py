"""Per-entity MQTT translation rules, ``integration_manager/mqtt_rules.json``:

    {"rules": {"<entity_id or glob>": {"exclude": true,
                                       "name": "Living room",
                                       "enabled_by_default": false,
                                       "entity_category": "diagnostic",
                                       "device_class": "temperature",
                                       "icon": "mdi:radiator"}}}

They change only what is published: the entity stays as it is in this
container's HA.  Exact entity ids win over globs; globs apply in sorted
order (later overrides earlier).

A device class must be one the main Home Assistant's MQTT platform takes for
the entity, with its unit: the main HA refuses the whole device config
otherwise, and every entity of that device loses its update."""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
from functools import lru_cache
from typing import Any, Callable, Iterable

from . import writer

FIELDS = ("exclude", "name", "enabled_by_default", "entity_category", "device_class", "icon")

_LOGGER = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _device_classes() -> dict[str, frozenset[str]]:
    """The device classes of every MQTT platform that takes one (homeassistant/components/mqtt/<platform>.py)."""
    from homeassistant.components.binary_sensor import BinarySensorDeviceClass
    from homeassistant.components.button import ButtonDeviceClass
    from homeassistant.components.cover import CoverDeviceClass
    from homeassistant.components.event import EventDeviceClass
    from homeassistant.components.humidifier import HumidifierDeviceClass
    from homeassistant.components.number.const import NumberDeviceClass
    from homeassistant.components.sensor.const import SensorDeviceClass
    from homeassistant.components.switch import SwitchDeviceClass
    from homeassistant.components.update import UpdateDeviceClass
    from homeassistant.components.valve import ValveDeviceClass

    enums = {"binary_sensor": BinarySensorDeviceClass, "button": ButtonDeviceClass, "cover": CoverDeviceClass,
             "event": EventDeviceClass, "humidifier": HumidifierDeviceClass, "number": NumberDeviceClass,
             "sensor": SensorDeviceClass, "switch": SwitchDeviceClass, "update": UpdateDeviceClass, "valve": ValveDeviceClass}
    return {platform: frozenset(member.value for member in enum) for platform, enum in enums.items()}


def device_class_problem(comp: dict[str, Any], device_class: str) -> str | None:
    """Why the main Home Assistant would refuse (or break on) this component with that device class, None when it fits."""
    platform = comp.get("platform")
    classes = _device_classes().get(platform)
    if classes is None:
        return f"a {platform} on the main Home Assistant takes no device class"
    if device_class not in classes:
        return f"{device_class} is not a device class of a {platform}"
    if platform != "sensor":
        return None
    from homeassistant.components.sensor.const import AMBIGUOUS_UNITS, DEVICE_CLASS_UNITS

    unit = comp.get("unit_of_measurement")
    unit = AMBIGUOUS_UNITS.get(unit, unit)
    if comp.get("options") and device_class != "enum":
        return "a sensor with a list of options keeps device class enum"
    if device_class == "enum" and (unit or comp.get("state_class")):
        return f"device class enum takes no unit or state class (this sensor has {unit or comp.get('state_class')})"
    if unit and device_class in DEVICE_CLASS_UNITS and unit not in DEVICE_CLASS_UNITS[device_class]:
        return f"its unit {unit} does not fit device class {device_class}"
    return None


# Home Assistant refuses these two read-only platforms an entity category of config (2026.9.3:
# sensor/__init__.py "cannot be added as the entity category is set to config", binary_sensor/__init__.py
# the same).  The entity is then never created there, which parity reports as permanently missing.
CONFIG_CATEGORY_REFUSED = frozenset({"sensor", "binary_sensor"})


def entity_category_problem(comp: dict[str, Any], category: str) -> str | None:
    """Why the main Home Assistant would refuse this component with that entity category, None when it fits."""
    if category == "config" and comp.get("platform") in CONFIG_CATEGORY_REFUSED:
        return f"the main Home Assistant refuses a {comp['platform']} whose entity category is config"
    return None


def matches(pattern: str, entity_id: str) -> bool:
    return pattern == entity_id or (any(ch in pattern for ch in "*?[") and fnmatch.fnmatchcase(entity_id, pattern))


class MqttRules:
    def __init__(self, path: str) -> None:
        self.path = path
        self.rules: dict[str, dict[str, Any]] = {}
        # (the rules dict they were taken from, its glob patterns in the order they apply): for_entity runs for every
        # entity on every republish, and sorting the patterns each time costs more than the matching
        self._globs: tuple[dict[str, dict[str, Any]], list[str]] | None = None
        # set by the publisher: the components of the entities a pattern matches, as published without rules
        self.components: Callable[[str], Iterable[tuple[str, dict[str, Any]]]] | None = None
        self._class_warned: set[tuple[str, str]] = set()
        self._category_warned: set[tuple[str, str]] = set()
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
            if not isinstance(v, dict):
                continue
            try:
                out[str(k)] = self.clean(v)
            except ValueError as err:
                if v.get("device_class") is not None:
                    try:  # a device class written before it was checked: the rest of the rule (an exclusion) still holds
                        out[str(k)] = self.clean({f: x for f, x in v.items() if f != "device_class"})
                        _LOGGER.error("mqtt rule %r: device_class ignored: %s", k, err)
                        continue
                    except ValueError:
                        pass
                _LOGGER.error("mqtt rule %r ignored: %s", k, err)
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
            if k == "device_class" and not any(v in classes for classes in _device_classes().values()):
                raise ValueError(f"device_class {v} is not a device class of Home Assistant")
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
                if cleaned.get("device_class") and cleaned.get("device_class") != (self.rules.get(pattern) or {}).get("device_class"):
                    self.check_device_class(pattern, cleaned["device_class"])
                if cleaned.get("entity_category") and cleaned.get("entity_category") != (self.rules.get(pattern) or {}).get("entity_category"):
                    self.check_entity_category(pattern, cleaned["entity_category"])
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
        if cur.get("device_class") and changes.get("device_class") is not None:
            self.check_device_class(entity_id, cur["device_class"])
        if cur.get("entity_category") and changes.get("entity_category") is not None:
            self.check_entity_category(entity_id, cur["entity_category"])
        if cur:
            self.rules[entity_id] = cur
        else:
            self.rules.pop(entity_id, None)
        self._globs = None  # changed in place: the identity check in for_entity does not see it
        return cur

    def check_device_class(self, pattern: str, device_class: str) -> None:
        """Refuses a device class that does not fit an entity the pattern matches now (an entity added later that it does
        not fit is published without it: apply_component)."""
        for entity_id, comp in (self.components(pattern) if self.components else ()):
            if problem := device_class_problem(comp, device_class):
                raise ValueError(f"device_class {device_class} does not fit {entity_id}: {problem}")

    def check_entity_category(self, pattern: str, category: str) -> None:
        """Refuses an entity category the main Home Assistant would not take for an entity the pattern matches now,
        the way an unfit device class is refused (one added later is published without it: apply_component)."""
        for entity_id, comp in (self.components(pattern) if self.components else ()):
            if problem := entity_category_problem(comp, category):
                raise ValueError(f"entity_category {category} does not fit {entity_id}: {problem}")

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
        if category := rule.get("entity_category"):
            # a glob matching an entity it does not fit: the main HA refuses a sensor whose entity category is
            # config outright, so the entity never appears there and parity reports it missing for good
            if (problem := entity_category_problem(comp, category)) is None:
                comp["entity_category"] = category
            elif (key := (str(comp.get("unique_id")), category)) not in self._category_warned:
                self._category_warned.add(key)
                _LOGGER.warning("MQTT rule: entity_category %s not applied to %s: %s", category, comp.get("unique_id"), problem)
        if rule.get("icon"):
            comp["icon"] = rule["icon"]
        if dc := rule.get("device_class"):
            # a glob matching an entity it does not fit: the main HA would refuse the whole device config, and the
            # rest of the rule (and every other entity of the device) with it
            if (problem := device_class_problem(comp, dc)) is None:
                comp["device_class"] = dc
            elif (key := (str(comp.get("unique_id")), dc)) not in self._class_warned:
                self._class_warned.add(key)
                _LOGGER.warning("MQTT rule: device_class %s not applied to %s: %s", dc, comp.get("unique_id"), problem)
        return comp
