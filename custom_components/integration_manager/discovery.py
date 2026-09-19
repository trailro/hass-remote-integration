"""Home Assistant MQTT discovery payloads for the translator.

Device-based discovery (HA 2024.11+): one retained config per HA device at
``<prefix>/device/<discovery_id>/config`` whose ``components`` hold every
entity of that device.  Each component points at the entity's raw
document topic (``<base>/<integration>/<domain>/<object_id>``) through
value templates, so the documents published by ``MqttPublisher`` are the
single source of state.  ``default_entity_id`` is the original entity id,
so the consuming HA creates the same ids (``climate.01_256364_05``) once
the original integration is gone from it.

Universal by construction: every HA domain that has an MQTT platform is
mapped natively (state + commands); any other domain is mirrored as a
read-only ``sensor`` carrying the state and all attributes, so nothing
the integration exposes is lost on the consuming side.  Disabled
registry entries are published with ``enabled_by_default: false``.

Commands arrive on ``<base>/cmd/<domain>/<object_id>/<field>`` and are
mapped to service calls by :func:`command_to_service`; arbitrary service
calls (any integration) arrive on ``<base>/call/<domain>/<service>``.
"""

from __future__ import annotations

import logging
import math
import os
import re

import json
from typing import Any

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

ORIGIN = {
    "name": "hass-remote-integration",
    "support_url": "https://github.com/trailro/hass-remote-integration",
}


def origin(prefix: str) -> dict[str, Any]:
    """Origin block naming the instance (prefix = instance key + '_')."""
    return {**ORIGIN, "name": f"{ORIGIN['name']} ({prefix.rstrip('_')})"}

# Domains with a native MQTT platform on the consuming HA.
NATIVE = {
    "alarm_control_panel", "binary_sensor", "button", "climate", "cover", "date",
    "datetime", "device_tracker", "event", "fan", "humidifier", "lawn_mower",
    "light", "lock", "notify", "number", "scene", "select", "sensor", "siren",
    "switch", "text", "time", "update", "vacuum", "valve", "water_heater",
}

# Platforms that never get a state topic (command-only).
_COMMAND_ONLY = {"button", "scene", "notify"}


# ----- what the main Home Assistant understands -------------------------
# The main HA validates a discovery payload strictly: it ignores a key it does
# not know, but an unknown platform or an unknown device class fails validation
# and it then throws away the WHOLE device payload - every entity of that
# device is gone there, and only its own log says why.  Discovery is one-way
# and its birth message carries no version, so the operator declares the main
# HA's version (MqttConfig.main_ha_version) and what that version cannot parse
# is left out here.  ha_compat.json says when each platform and each device
# class appeared; tools/gen_ha_compat.py generates it from the wheels.

COMPAT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ha_compat.json")

# 2026.8, 2026.8.3, 2026.9.0b0: a Home Assistant calendar version, patch and pre-release suffix optional.
_VERSION_RE = re.compile(r"^(\d{4})\.(\d{1,2})(?:\.\d+(?:[ab]\d+)?)?$")

_COMPAT_TABLE: dict[str, Any] | None = None


def parse_ha_version(value: Any) -> tuple[int, int] | None:
    """(year, month) of a Home Assistant version string, None for anything
    that is not one: the table is keyed by release, patches never differ."""
    if not isinstance(value, str):
        return None
    match = _VERSION_RE.match(value.strip())
    return (int(match.group(1)), int(match.group(2))) if match else None


def compat_table() -> dict[str, Any]:
    """ha_compat.json, read once.  A missing or damaged file means no table,
    which means no filtering: the payload is what it has always been."""
    global _COMPAT_TABLE
    if _COMPAT_TABLE is None:
        try:
            with open(COMPAT_FILE, encoding="utf-8") as handle:
                table = json.load(handle)
            if not isinstance(table, dict) or "platforms" not in table:
                raise ValueError("no platform table")
        except (OSError, ValueError) as err:
            _LOGGER.warning("MQTT discovery: %s is unusable (%s): nothing is filtered for an older main Home Assistant",
                            COMPAT_FILE, err)
            table = {"generated": {}, "platforms": {}, "device_classes": {}, "removed": {}}
        _COMPAT_TABLE = table
    return _COMPAT_TABLE


class Compat:
    """One discovery pass against one declared main Home Assistant: answers
    what that version knows, and counts what was left out because of it."""

    def __init__(self, release: tuple[int, int], table: dict[str, Any]) -> None:
        self.release = release
        self._platforms = table.get("platforms") or {}
        self._device_classes = table.get("device_classes") or {}
        removed = table.get("removed") or {}
        self._platforms_removed = removed.get("platforms") or {}
        self._device_classes_removed = removed.get("device_classes") or {}
        # "sensor.radon" / "date" -> how many entities it cost this pass
        self.dropped_device_classes: dict[str, int] = {}
        self.mirrored_platforms: dict[str, int] = {}

    @property
    def device_class_drops(self) -> int:
        return sum(self.dropped_device_classes.values())

    @property
    def platform_drops(self) -> int:
        return sum(self.mirrored_platforms.values())

    def _known(self, first: Any, removed: Any) -> bool:
        since = parse_ha_version(first)
        if since is None or self.release < since:
            return False
        gone = parse_ha_version(removed)
        return gone is None or self.release < gone

    def knows_platform(self, domain: str) -> bool:
        return self._known(self._platforms.get(domain), self._platforms_removed.get(domain))

    def knows_device_class(self, domain: str, value: str) -> bool:
        """A domain the table has nothing for is left alone (no data is not
        "unknown"); inside a domain it has, a name that is not there is one no
        scanned Home Assistant up to the declared version has ever had."""
        known = self._device_classes.get(domain)
        if known is None:
            return True
        return self._known(known.get(value), (self._device_classes_removed.get(domain) or {}).get(value))

    def note_platform(self, domain: str) -> None:
        self.mirrored_platforms[domain] = self.mirrored_platforms.get(domain, 0) + 1

    def note_device_class(self, domain: str, value: str) -> None:
        key = f"{domain}.{value}"
        self.dropped_device_classes[key] = self.dropped_device_classes.get(key, 0) + 1


def compat_for(version: Any) -> Compat | None:
    """The filter for a declared main Home Assistant version, or None when
    nothing has to be filtered - no version declared (the default: assume the
    main HA is current), a value that is not a version, or a version at or
    above the newest release the table was generated from, which knows
    everything the table knows about.  None means the payload is built exactly
    as it was before this setting existed."""
    release = parse_ha_version(version)
    if release is None:
        return None
    table = compat_table()
    newest = parse_ha_version((table.get("generated") or {}).get("newest"))
    if newest is None or release >= newest:
        return None
    oldest = parse_ha_version((table.get("generated") or {}).get("oldest"))
    if oldest is not None and release < oldest:
        # Below the table's floor there is no data.  Device-based discovery
        # needs 2024.11 anyway, so such a main HA receives nothing at all;
        # the floor of the table is the closest honest answer.
        release = oldest
    return Compat(release, table)


def _tpl(expr: str) -> str:
    return "{{ " + expr + " }}"


def _num(value: Any, default: float) -> float:
    """MQTT discovery wants real floats; a climate entity may report
    min/max_temp as None, which HA rejects for the whole component."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float(default)
    return out if math.isfinite(out) else float(default)  # NaN/inf would make the whole device config invalid JSON


def _speed_count(step: Any) -> int | None:
    """Number of speeds of a fan from its percentage_step (Home Assistant: step = 100 / speed_count); None for a
    fan with one speed or 100 (the MQTT default range already is 1..100)."""
    try:
        count = round(100 / float(step))
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None
    return count if 1 < count < 100 else None


def _onoff(expr: str) -> str:
    """ON/OFF for the on/off platforms; 'None' (their "unknown") for unknown and unavailable, never a made-up OFF."""
    return _tpl(f"'None' if {expr} in ['unavailable', 'unknown'] else ('ON' if {expr} == 'on' else 'OFF')")


def _attr(name: str) -> str:
    """Attribute template: missing key or unavailable source -> 'None', which
    the MQTT platforms treat as 'no value' instead of an invalid string."""
    return _tpl(f"value_json.attributes.get('{name}', None)")


def _attr_or_empty(name: str) -> str:
    """For handlers with no 'None' payload (light brightness and colour temperature, cover position and tilt):
    they parse 'None' as a number and log a traceback or a warning, and skip an empty payload silently."""
    return _tpl(f"value_json.attributes.get('{name}') if value_json.attributes.get('{name}') is not none else ''")


# 'unavailable'/'unknown' as a raw state would be an invalid value for
# numeric sensors and enum-like platforms; 'None' is the documented no-value
# payload (PAYLOAD_NONE) and per-entity availability marks the entity offline.
_STATE_TPL = _tpl("'None' if value_json.state in ['unavailable', 'unknown'] else value_json.state")

# A device_tracker state for the main Home Assistant: the reset payload ("None", which clears the
# location name) whenever the document carries coordinates for it to place itself with, or when there
# is nothing to say; otherwise the source's own home/not_home.  See the device_tracker branch of
# build_component for why nothing else may be rendered here.
_DEVICE_TRACKER_TPL = _tpl(
    "'None' if (value_json.attributes.get('latitude') is number and value_json.attributes.get('longitude') is number)"
    " or value_json.state in ['unavailable', 'unknown']"
    " else ('home' if value_json.state == 'home' else 'not_home')")

# The main HA's MQTT update validates the rendered JSON as a whole and throws ALL of it away
# for one field it refuses - a null anywhere, a release_url its cv.url rejects (a relative one,
# which is what an integration serving its notes from /local or /api reports), an
# update_percentage that is not a number in 0..100 - and the entity keeps nothing, not even the
# version it had.  So only the fields that carry a value that platform accepts go out.
# update_percentage is the one field that always goes out: null is what clears the progress bar
# there, and a field left out keeps the value it had.
_UPDATE_FIELDS = ["installed_version", "latest_version", "title", "release_summary", "in_progress"]
_UPDATE_TPL = _tpl(
    "dict("
    f"(value_json.attributes.items() | selectattr('0', 'in', {_UPDATE_FIELDS!r}) | rejectattr('1', 'none') | list)"
    " + ([('release_url', value_json.attributes.release_url)]"
    " if value_json.attributes.get('release_url') is string"
    " and (value_json.attributes.release_url.startswith('http://')"
    " or value_json.attributes.release_url.startswith('https://')) else [])"
    " + [('update_percentage', value_json.attributes.get('update_percentage')"
    " if value_json.attributes.get('update_percentage') is number"
    " and value_json.attributes.get('update_percentage') >= 0"
    " and value_json.attributes.get('update_percentage') <= 100 else None)]"
    ") | to_json"
)

# Availability on the entity's own document: 'unavailable' at the source is offline on the consumer.
_AVAILABILITY_TPL = _tpl("'offline' if value_json.state == 'unavailable' else 'online'")
# Same, for a platform with no no-value payload: an unknown state has nothing honest to show either.
_AVAILABILITY_UNKNOWN_TPL = _tpl("'offline' if value_json.state in ['unavailable', 'unknown'] else 'online'")


def _offline_when_unknown(comp: dict[str, Any]) -> None:
    """MQTT text takes the payload as the value, whatever it is: it has no PAYLOAD_NONE, so the 'None' every other
    platform reads as "no value" would show as that word.  An unknown source state makes the entity unavailable on
    the consumer instead, the way an unavailable one already does; a value that really is 'None' (or empty) is
    untouched, because it is the source state that decides, not the payload."""
    for avail in comp.get("availability", ()):
        if avail.get("value_template") == _AVAILABILITY_TPL:
            avail["value_template"] = _AVAILABILITY_UNKNOWN_TPL


def _humidity_range(attrs: dict[str, Any], low_key: str, high_key: str) -> tuple[float, float]:
    """(min, max) a target-humidity option may carry: the main HA refuses the
    WHOLE device payload for a negative minimum, a maximum above 100 or a range
    that does not grow, and a source is free to report any of the three."""
    low, high = _num(attrs.get(low_key), 0), _num(attrs.get(high_key), 100)
    return (low, high) if 0 <= low < high <= 100 else (0.0, 100.0)


def _device_name(hass: HomeAssistant, entry: er.RegistryEntry | None) -> str | None:
    """The name :func:`device_block` will publish for this entity's device, or
    None when the entity has no device (it lands in the per-integration bucket,
    whose name is no entity's own)."""
    device_id = getattr(entry, "device_id", None) if entry is not None else None
    if not device_id:
        return None
    dev = dr.async_get(hass).async_get(device_id)
    return (dev.name_by_user or dev.name or dev.id) if dev else None


def _unprefixed(name: str, device_name: str) -> str | None:
    """`name` without the device's name in front of it, or `name` unchanged
    when the device's name is not a prefix of it.  Home Assistant's own rule
    (entity_registry._async_strip_prefix_from_entity_name): case-insensitive,
    a separator has to follow, and a lower-case first word is capitalised."""
    head, rest = name[:len(device_name)], name[len(device_name):]
    if head.casefold() != device_name.casefold():
        return name
    stripped = rest.lstrip(" -:")
    if not stripped or stripped == rest:  # "Hall Lamplight" only starts like "Hall Lamp"
        return name
    first = stripped.partition(" ")[0]
    return stripped if not first.islower() else stripped[0].upper() + stripped[1:]


def _entity_name(entry: er.RegistryEntry | None, state: State, device_name: str | None) -> str | None:
    """The component's name, as the main Home Assistant will use it.

    Every MQTT entity has ``has_entity_name`` there, so what it shows is
    ``"<device name> <component name>"`` and a component that carries the
    device's name reads "Hall Lamp Hall Lamp".  The entity that IS the device
    has no name of its own there (``None``), and the others carry only their
    own part - which is what the source shows too, by the same rule.

    A source entity with ``has_entity_name`` already holds exactly that part in
    the registry, ``None`` included.  For everything else (a legacy entity, or
    one with no registry entry at all) the name in hand is the composed one, so
    the device's name comes off the front of it the way Home Assistant does it.
    """
    own = (entry.name or entry.original_name) if entry else None
    if entry is not None and getattr(entry, "has_entity_name", False) and device_name:
        return own or None
    full = own or state.attributes.get("friendly_name") or state.entity_id.split(".", 1)[1]
    if not device_name:
        return full
    if full.casefold() == device_name.casefold():
        return None
    return _unprefixed(full, device_name)


def _common(entry: er.RegistryEntry | None, state: State, doc_topic: str, prefix: str,
            device_name: str | None = None) -> dict[str, Any]:
    domain, object_id = state.entity_id.split(".", 1)
    name = _entity_name(entry, state, device_name)
    status_topic = doc_topic.split("/", 1)[0] + "/status"
    comp: dict[str, Any] = {
        "platform": domain,
        "name": name,
        "unique_id": f"{prefix}{state.entity_id}",
        # HA 2026.x: `object_id` is gone from the MQTT schema (ignored);
        # `default_entity_id` sets the entity id at creation time.
        "default_entity_id": state.entity_id,
        "state_topic": doc_topic,
        "json_attributes_topic": doc_topic,
        "json_attributes_template": _tpl("value_json.attributes | to_json"),
        # offline when this instance is down OR the source entity itself is
        # unavailable; the consumer then shows unavailable, not stale values
        "availability": [
            {"topic": status_topic},
            {"topic": doc_topic, "value_template": _AVAILABILITY_TPL},
        ],
        "availability_mode": "all",
        "payload_available": "online",
        "payload_not_available": "offline",
    }
    if entry:
        if entry.icon or entry.original_icon:
            comp["icon"] = entry.icon or entry.original_icon
        if entry.entity_category:
            comp["entity_category"] = entry.entity_category.value
        if entry.disabled:
            comp["enabled_by_default"] = False
    return comp


def _declares(attrs: dict[str, Any], bits: int) -> bool:
    """The source entity declares at least one of these ``supported_features`` bits.

    A capability attribute is only in the state while the entity has a value for it - an entity that
    is ``unavailable`` when its config goes out has no attributes at all - so a topic announced on the
    strength of a value alone comes and goes with that value.  The feature bits do not: they say what
    the entity can do whatever it is doing now.  Unknown features (no integer) are NOT a yes here: a
    topic invented for a feature nobody declared is a control that fails on the main Home Assistant.
    """
    features = attrs.get("supported_features")
    return isinstance(features, int) and bool(features & bits)


def _valid_regex(value: Any) -> str | None:
    """A code format the main Home Assistant can compile (its `code_format` is `cv.is_regex`), or None:
    an invalid pattern fails the whole device payload there, and the lock is better off without a code
    box than the device is without its entities."""
    if not isinstance(value, str) or not value:
        return None
    try:
        re.compile(value)
    except re.error:
        return None
    return value


def _device_class(entry: er.RegistryEntry | None, attrs: dict[str, Any],
                  domain: str | None = None, compat: Compat | None = None) -> str | None:
    """The source entity's device class, or None when the declared main Home
    Assistant does not know it: the entity is still announced, without a class,
    instead of the unknown value costing the whole device its payload."""
    if entry and (entry.device_class or entry.original_device_class):
        value = entry.device_class or entry.original_device_class
    else:
        value = attrs.get("device_class")
    if value and compat is not None and domain and not compat.knows_device_class(domain, value):
        compat.note_device_class(domain, value)
        return None
    return value


def build_component(
    hass: HomeAssistant, state: State, doc_topic: str, cmd_base: str, prefix: str, compat: Compat | None = None
) -> dict[str, Any]:
    """Return the discovery component for one entity (never None: unmapped
    domains fall back to a read-only sensor mirror).  `compat` is the declared
    main Home Assistant (:func:`compat_for`); None filters nothing."""
    domain, object_id = state.entity_id.split(".", 1)
    entry = er.async_get(hass).async_get(state.entity_id)
    attrs = dict(state.attributes)
    if entry is not None:
        # Discovery is shaped by the attributes, and an entity that is `unavailable` at this moment has
        # none at all: a cover would lose its position, a fan its speed, a select its options, for as
        # long as the retained config lives.  The registry keeps what does not depend on the moment -
        # the capability attributes and the feature bits - so they stand in for what the state is not
        # carrying right now.  A value the state DOES have always wins (setdefault).
        for key, value in (entry.capabilities or {}).items():
            attrs.setdefault(key, value)
        if isinstance(getattr(entry, "supported_features", None), int):
            attrs.setdefault("supported_features", entry.supported_features)
    cmd = f"{cmd_base}/{domain}/{object_id}"
    device_name = _device_name(hass, entry)
    features = attrs.get("supported_features")

    def supports(bits: int) -> bool:
        """_declares, plus the benefit of the doubt when the source declares nothing at all.

        The two differ only there, and deliberately: _declares decides the component's *shape* - a topic
        invented for a feature nobody declared is a control that fails on the main Home Assistant - while
        this decides which features of a control to announce.  A registry entry from an integration that
        never loaded has no state and no features, and announcing none of them would leave a stub the
        operator cannot use at all.
        """
        return _declares(attrs, bits) or not isinstance(features, int)

    if domain not in NATIVE:
        return _mirror_as_sensor(entry, state, doc_topic, prefix, device_name)
    if compat is not None and not compat.knows_platform(domain):
        # date/time/datetime got their MQTT platforms in 2026.5: on an older
        # main HA the platform name alone invalidates the device payload.  The
        # sensor mirror is what that path is for - the entity still arrives,
        # read-only.
        compat.note_platform(domain)
        return _mirror_as_sensor(entry, state, doc_topic, prefix, device_name)

    comp = _common(entry, state, doc_topic, prefix, device_name)
    if domain in _COMMAND_ONLY:
        # no state to mirror, but availability is its own topic on these
        # platforms: keep both entries, or a button stays pressable while the
        # source entity behind it is unavailable
        comp.pop("state_topic", None)

    if domain == "sensor":
        comp["value_template"] = _STATE_TPL
        for key in ("unit_of_measurement", "state_class"):
            if attrs.get(key) is not None:
                comp[key] = attrs[key]
        if entry and entry.unit_of_measurement:
            comp["unit_of_measurement"] = entry.unit_of_measurement
        if dc := _device_class(entry, attrs, domain, compat):
            comp["device_class"] = dc
        if dc == "enum" and attrs.get("options"):
            comp["options"] = list(attrs["options"])
        if attrs.get("state_class") == "total":
            # `total` is the one state class whose reset the main Home Assistant cannot
            # infer: its statistics engine starts a new cycle only when last_reset changes
            # (`total_increasing` has the value-drop heuristic instead), and MQTT blocks
            # last_reset from the JSON attributes, so without this template a meter reset
            # is booked as a negative delta and the energy dashboard loses the cycle.
            # The platform refuses the template with any other state class.
            comp["last_reset_value_template"] = _attr_or_empty("last_reset")

    elif domain == "binary_sensor":
        comp.update({"value_template": _STATE_TPL, "payload_on": "on", "payload_off": "off"})
        if dc := _device_class(entry, attrs, domain, compat):
            comp["device_class"] = dc

    elif domain == "climate":
        comp.pop("state_topic", None)
        comp.update(
            {
                "modes": list(attrs.get("hvac_modes") or ["off", "heat"]),
                "mode_state_topic": doc_topic,
                "mode_state_template": _STATE_TPL,
                "mode_command_topic": f"{cmd}/mode",
                "current_temperature_topic": doc_topic,
                "current_temperature_template": _attr('current_temperature'),
                "temperature_state_topic": doc_topic,
                "temperature_state_template": _attr('temperature'),
                "temperature_command_topic": f"{cmd}/temperature",
                "action_topic": doc_topic,
                "action_template": _attr('hvac_action'),
                "min_temp": _num(attrs.get("min_temp"), 5),
                "max_temp": _num(attrs.get("max_temp"), 35),
                "temp_step": _num(attrs.get("target_temp_step"), 0.5),
                "precision": 0.1,
                "temperature_unit": "C",
            }
        )
        # TARGET_TEMPERATURE_RANGE (2), not the two values: a thermostat only carries them while it is in
        # a range mode, so one published in `heat` never got the high/low topics and could not be put in
        # `heat_cool` from the main HA at all.  The values still answer for a source with no feature bits.
        if (_declares(attrs, 2) or attrs.get("target_temp_high") is not None
                or attrs.get("target_temp_low") is not None):
            comp.update(
                {
                    "temperature_high_state_topic": doc_topic,
                    "temperature_high_state_template": _attr('target_temp_high'),
                    "temperature_high_command_topic": f"{cmd}/temperature_high",
                    "temperature_low_state_topic": doc_topic,
                    "temperature_low_state_template": _attr('target_temp_low'),
                    "temperature_low_command_topic": f"{cmd}/temperature_low",
                }
            )
        # MQTT climate reserves "none" as the implicit no-preset value and
        # rejects the whole component if it appears in the list.
        presets = [p for p in (attrs.get("preset_modes") or []) if str(p).lower() != "none"]
        if presets:
            comp.update(
                {
                    "preset_modes": presets,
                    "preset_mode_state_topic": doc_topic,
                    "preset_mode_value_template": _attr('preset_mode'),
                    "preset_mode_command_topic": f"{cmd}/preset_mode",
                }
            )
        if attrs.get("fan_modes"):
            comp.update(
                {
                    "fan_modes": list(attrs["fan_modes"]),
                    "fan_mode_state_topic": doc_topic,
                    "fan_mode_state_template": _attr('fan_mode'),
                    "fan_mode_command_topic": f"{cmd}/fan_mode",
                }
            )
        if attrs.get("swing_modes"):
            comp.update(
                {
                    "swing_modes": list(attrs["swing_modes"]),
                    "swing_mode_state_topic": doc_topic,
                    "swing_mode_state_template": _attr('swing_mode'),
                    "swing_mode_command_topic": f"{cmd}/swing_mode",
                }
            )
        if attrs.get("swing_horizontal_modes"):
            comp.update(
                {
                    "swing_horizontal_modes": list(attrs["swing_horizontal_modes"]),
                    "swing_horizontal_mode_state_topic": doc_topic,
                    "swing_horizontal_mode_state_template": _attr('swing_horizontal_mode'),
                    "swing_horizontal_mode_command_topic": f"{cmd}/swing_horizontal_mode",
                }
            )
        if attrs.get("current_humidity") is not None:
            comp.update({"current_humidity_topic": doc_topic, "current_humidity_template": _attr('current_humidity')})
        if attrs.get("humidity") is not None or (isinstance(features, int) and features & 4):  # TARGET_HUMIDITY
            low, high = _humidity_range(attrs, "min_humidity", "max_humidity")
            comp.update(
                {
                    "target_humidity_state_topic": doc_topic,
                    "target_humidity_state_template": _attr('humidity'),
                    "target_humidity_command_topic": f"{cmd}/humidity",
                    "min_humidity": low,
                    "max_humidity": high,
                }
            )
        if isinstance(features, int) and features & 128 and features & 256:  # TURN_OFF and TURN_ON
            # The main HA's MQTT climate announces turn on/off whatever it was given; without
            # this topic both fall back to writing an hvac mode, which is a different thing and
            # loses the source's own on/off.  One topic serves both, so it needs both features.
            comp["power_command_topic"] = f"{cmd}/power"

    elif domain == "water_heater":
        comp.pop("state_topic", None)
        comp.update(
            {
                "modes": list(attrs.get("operation_list") or ["off", "eco", "heat_pump"]),
                "mode_state_topic": doc_topic,
                "mode_state_template": _STATE_TPL,
                "mode_command_topic": f"{cmd}/mode",
                "current_temperature_topic": doc_topic,
                "current_temperature_template": _attr('current_temperature'),
                "temperature_state_topic": doc_topic,
                "temperature_state_template": _attr('temperature'),
                "temperature_command_topic": f"{cmd}/temperature",
                "min_temp": _num(attrs.get("min_temp"), 30),
                "max_temp": _num(attrs.get("max_temp"), 70),
                "temperature_unit": "C",
            }
        )
        if isinstance(features, int) and features & 8:  # WaterHeaterEntityFeature.ON_OFF; away mode has no MQTT option
            comp["power_command_topic"] = f"{cmd}/power"

    elif domain == "switch":
        comp.update(
            {"value_template": _STATE_TPL, "payload_on": "on", "payload_off": "off",
             "state_on": "on", "state_off": "off", "command_topic": f"{cmd}/state"}
        )
        if dc := _device_class(entry, attrs, domain, compat):
            comp["device_class"] = dc

    elif domain == "select":
        comp.update({"value_template": _STATE_TPL, "options": list(attrs.get("options") or []), "command_topic": f"{cmd}/option"})

    elif domain == "number":
        comp.update(
            {"value_template": _STATE_TPL, "command_topic": f"{cmd}/value",
             "min": _num(attrs.get("min"), 0), "max": _num(attrs.get("max"), 100), "step": _num(attrs.get("step"), 1)}
        )
        if attrs.get("unit_of_measurement"):
            comp["unit_of_measurement"] = attrs["unit_of_measurement"]
        if attrs.get("mode") in ("box", "slider", "auto"):
            comp["mode"] = attrs["mode"]
        if dc := _device_class(entry, attrs, domain, compat):
            comp["device_class"] = dc

    elif domain == "light":
        comp.update(
            {"state_value_template": _onoff("value_json.state"), "command_topic": f"{cmd}/state",
             "payload_on": "ON", "payload_off": "OFF"}
        )
        modes = set(attrs.get("supported_color_modes") or [])
        if modes & {"brightness", "color_temp", "hs", "rgb", "rgbw", "rgbww", "xy", "white"}:
            comp.update(
                {"brightness_state_topic": doc_topic, "brightness_value_template": _attr_or_empty('brightness'),
                 "brightness_command_topic": f"{cmd}/brightness", "brightness_scale": 255}
            )
        if "color_temp" in modes:
            comp.update(
                {"color_temp_kelvin": True, "color_temp_state_topic": doc_topic,
                 "color_temp_value_template": _attr_or_empty('color_temp_kelvin'),
                 "color_temp_command_topic": f"{cmd}/color_temp"}
            )
            if attrs.get("min_color_temp_kelvin"):
                comp["min_kelvin"] = int(attrs["min_color_temp_kelvin"])
            if attrs.get("max_color_temp_kelvin"):
                comp["max_kelvin"] = int(attrs["max_color_temp_kelvin"])
        # Exactly one colour topic: the main HA reads the colour mode off the topics it was
        # given, so an rgbw light announced on the rgb topic becomes a plain rgb light there
        # and its white channel is gone.  hs and xy have no MQTT topic of their own here and
        # keep travelling as the rgb the source computes for them.
        colour = ("rgbww" if "rgbww" in modes else
                  "rgbw" if "rgbw" in modes else
                  "rgb" if modes & {"rgb", "hs", "xy"} else None)
        if colour:
            comp.update(
                {f"{colour}_state_topic": doc_topic,
                 f"{colour}_value_template": _tpl(f"(value_json.attributes.get('{colour}_color') or []) | join(',')"),
                 f"{colour}_command_topic": f"{cmd}/{colour}"}
            )
        if attrs.get("effect_list"):
            comp.update(
                {"effect_list": list(attrs["effect_list"]), "effect_state_topic": doc_topic,
                 # the main HA takes the payload as the effect name, so the 'None' every other
                 # attribute travels as would show as an effect called "None"; an empty payload
                 # is the one it ignores.  A source whose effect really is the string "None"
                 # still sends it, because it is the value that decides, not the absence of one.
                 "effect_value_template": _attr_or_empty('effect'), "effect_command_topic": f"{cmd}/effect"}
            )

    elif domain == "cover":
        # MQTT cover derives its features from the topics and payloads: announce only what the source supports
        # (CoverEntityFeature bits), or a tilt-only cover gets open/close/stop buttons that fail here
        comp.update(
            {"value_template": _STATE_TPL,
             "state_open": "open", "state_closed": "closed", "state_opening": "opening",
             "state_closing": "closing", "state_stopped": "stopped"}
        )
        moves = {"payload_open": ("OPEN", 1), "payload_close": ("CLOSE", 2), "payload_stop": ("STOP", 8)}
        if supports(1 | 2 | 8):
            comp["command_topic"] = f"{cmd}/command"
            comp.update({key: payload if supports(bit) else None for key, (payload, bit) in moves.items()})
        if dc := _device_class(entry, attrs, domain, compat):
            comp["device_class"] = dc
        # SET_POSITION (4) / the tilt bits announce the topics even while the source has no value for them
        # (it is `unavailable`, or it has not reported yet): the value alone would take the position and
        # tilt controls off the main HA until the next full republish, an hour away by default.
        if attrs.get("current_position") is not None or _declares(attrs, 4):
            comp.update({"position_topic": doc_topic, "position_template": _attr_or_empty('current_position')})
            if supports(4):
                comp["set_position_topic"] = f"{cmd}/position"
        if attrs.get("current_tilt_position") is not None or _declares(attrs, 16 | 32 | 64 | 128):
            comp.update({"tilt_status_topic": doc_topic, "tilt_status_template": _attr_or_empty('current_tilt_position')})
            if supports(16 | 32 | 64 | 128):  # the main HA turns a tilt command topic into all four tilt features
                comp["tilt_command_topic"] = f"{cmd}/tilt"
                if not supports(128):
                    # Open/close tilt travel as tilt_opened_value (100) and tilt_closed_value (0) on this topic;
                    # a source that cannot set a tilt position refuses those, so the two boundaries become
                    # open_cover_tilt / close_cover_tilt instead (stop already sends its own payload_stop_tilt).
                    comp["tilt_command_template"] = _tpl(
                        "'OPEN' if value | int(-1) == 100 else ('CLOSE' if value | int(-1) == 0 else value)")

    elif domain == "valve":
        # The main HA derives the valve's features from the payloads it was given
        # (ValveEntityFeature bits): announce what the source says it can do, or a valve
        # that cannot be stopped gets a stop button that fails here.
        comp.update({"value_template": _STATE_TPL, "command_topic": f"{cmd}/command"})
        for key, (payload, bit) in {"payload_open": ("OPEN", 1), "payload_close": ("CLOSE", 2)}.items():
            comp[key] = payload if supports(bit) else None
        if supports(8):
            comp["payload_stop"] = "STOP"
        if dc := _device_class(entry, attrs, domain, compat):
            comp["device_class"] = dc
        # ValveEntityFeature.SET_POSITION.  Declared: announce it whatever the current value is, so an
        # unavailable valve does not lose its position topics.  Declared *without* it while a position is
        # reported: announce nothing - `reports_position` is what puts a slider on the main Home Assistant,
        # and a valve that only tells its position would refuse it.  Declaring nothing at all is the one
        # case the value decides (see `supports`).  A position valve carries no open/close payloads: the
        # main HA refuses the component for the keys themselves.
        if supports(4) and (_declares(attrs, 4) or attrs.get("current_position") is not None):
            for k in ("payload_open", "payload_close"):
                comp.pop(k, None)
            comp.update(
                {"reports_position": True, "value_template": _attr('current_position'),
                 "command_topic": f"{cmd}/position"}
            )

    elif domain == "fan":
        comp.update({"state_value_template": _onoff("value_json.state"), "command_topic": f"{cmd}/state", "payload_on": "ON", "payload_off": "OFF"})
        # FanEntityFeature: SET_SPEED (1), OSCILLATE (2), DIRECTION (4).  A fan that is off or
        # unavailable carries no percentage, no oscillating and no direction, and the config it got
        # announced with then had none of those controls on the main Home Assistant.
        if attrs.get("percentage") is not None or attrs.get("percentage_step") is not None or _declares(attrs, 1):
            comp.update(
                {"percentage_state_topic": doc_topic, "percentage_value_template": _attr('percentage'),
                 "percentage_command_topic": f"{cmd}/percentage"}
            )
            if speeds := _speed_count(attrs.get("percentage_step")):
                # the main HA offers the source's steps only through a speed range: the state goes out as the speed
                # (1..speeds, 0 for off) and a speed comes back here as the percentage Home Assistant uses for it
                comp.update(
                    {"speed_range_min": 1, "speed_range_max": speeds,
                     "percentage_value_template": _tpl(
                         "'None' if value_json.attributes.get('percentage') is none"
                         f" else (value_json.attributes.percentage * {speeds} / 100) | round(0, 'ceil') | int"),
                     "percentage_command_template": _tpl(f"value * 100 // {speeds}")}
                )
        if attrs.get("preset_modes"):
            comp.update(
                {"preset_modes": list(attrs["preset_modes"]), "preset_mode_state_topic": doc_topic,
                 "preset_mode_value_template": _attr('preset_mode'), "preset_mode_command_topic": f"{cmd}/preset_mode"}
            )
        if attrs.get("oscillating") is not None or _declares(attrs, 2):
            comp.update(
                {"oscillation_state_topic": doc_topic,
                 "oscillation_value_template": _tpl("'oscillate_on' if value_json.attributes.get('oscillating') else 'oscillate_off'"),
                 "oscillation_command_topic": f"{cmd}/oscillate"}
            )
        if attrs.get("direction") is not None or _declares(attrs, 4):
            comp.update(
                {"direction_state_topic": doc_topic, "direction_value_template": _attr('direction'),
                 "direction_command_topic": f"{cmd}/direction"}
            )

    elif domain == "lock":
        comp.update(
            # like the alarm panel: the code typed on the consuming side travels with the action and the
            # source lock checks it.  Without the template MQTT lock sends the bare payload, the code the
            # user typed stays there, and a code-protected lock refuses every command from the main HA.
            # Every LockState the source can report, `opening` included: the main HA maps only the states
            # it was given a payload for and shows the rest as unknown.
            {"value_template": _STATE_TPL, "state_locked": "locked", "state_unlocked": "unlocked",
             "state_locking": "locking", "state_unlocking": "unlocking", "state_jammed": "jammed",
             "state_open": "open", "state_opening": "opening",
             "command_topic": f"{cmd}/command", "payload_lock": "LOCK", "payload_unlock": "UNLOCK",
             "command_template": '{"action": "{{ value }}", "code": {{ code | to_json }}}'}
        )
        if supports(1):  # LockEntityFeature.OPEN; the payload alone is what announces it there
            comp["payload_open"] = "OPEN"
        # MQTT lock's code_format is the regex the main HA validates the typed code against (the alarm's
        # is a `number`/`text` keyword instead); the source lock's `code_format` is that same regex.
        if code_format := _valid_regex(attrs.get("code_format")):
            comp["code_format"] = code_format

    elif domain == "button":
        comp.update({"command_topic": f"{cmd}/press", "payload_press": "PRESS"})
        if dc := _device_class(entry, attrs, domain, compat):
            comp["device_class"] = dc

    elif domain == "scene":
        comp.update({"command_topic": f"{cmd}/activate", "payload_on": "ON"})

    elif domain == "notify":
        comp.update({"command_topic": f"{cmd}/message"})

    elif domain == "event":
        # A retained document would replay the last event on every (re)connect,
        # so events use a dedicated non-retained topic (see MqttPublisher).
        comp["state_topic"] = event_stream_topic(doc_topic)
        comp.update(
            {"event_types": list(attrs.get("event_types") or ["unknown"]),
             "value_template": _tpl("{'event_type': value_json.attributes.get('event_type')} | to_json")}
        )
        if dc := _device_class(entry, attrs, domain, compat):
            comp["device_class"] = dc

    elif domain == "text":
        comp.update({"value_template": _STATE_TPL, "command_topic": f"{cmd}/value"})
        _offline_when_unknown(comp)  # no PAYLOAD_NONE here: 'None' would be the value shown
        if attrs.get("min") is not None:
            comp["min"] = int(attrs["min"])
        if attrs.get("max") is not None:
            comp["max"] = min(int(attrs["max"]), 255)
        if attrs.get("mode") in ("text", "password"):
            comp["mode"] = attrs["mode"]

    elif domain in ("date", "time", "datetime"):
        comp.update({"value_template": _STATE_TPL, "command_topic": f"{cmd}/value"})

    elif domain == "siren":
        comp.update(
            {"state_value_template": _onoff("value_json.state"), "command_topic": f"{cmd}/state",
             "payload_on": "ON", "payload_off": "OFF", "state_on": "ON", "state_off": "OFF"}
        )
        if attrs.get("available_tones"):
            comp["available_tones"] = list(attrs["available_tones"])

    elif domain == "humidifier":
        comp.update(
            {
                "state_value_template": _onoff("value_json.state"),
                "command_topic": f"{cmd}/state", "payload_on": "ON", "payload_off": "OFF",
                "target_humidity_state_topic": doc_topic,
                "target_humidity_state_template": _attr('humidity'),
                "target_humidity_command_topic": f"{cmd}/humidity",
                "current_humidity_topic": doc_topic,
                "current_humidity_template": _attr('current_humidity'),
                # what the source is doing right now (humidifying / drying / idle / off); an
                # absent action travels as 'None', which the main HA reads as "no action"
                "action_topic": doc_topic,
                "action_template": _attr('action'),
                **dict(zip(("min_humidity", "max_humidity"), _humidity_range(attrs, "min_humidity", "max_humidity"))),
            }
        )
        if dc := _device_class(entry, attrs, domain, compat):
            comp["device_class"] = dc
        if attrs.get("available_modes"):
            comp.update(
                {"modes": list(attrs["available_modes"]), "mode_state_topic": doc_topic,
                 "mode_state_template": _attr('mode'), "mode_command_topic": f"{cmd}/mode"}
            )

    elif domain == "alarm_control_panel":
        code_format = attrs.get("code_format")
        comp.update(
            # the code typed on the consuming side travels with the action; the source panel checks it
            {"value_template": _STATE_TPL, "command_topic": f"{cmd}/command",
             "command_template": '{"action": "{{ action }}", "code": {{ code | to_json }}}',
             "code_arm_required": bool(code_format) and bool(attrs.get("code_arm_required", True)),
             "code_disarm_required": bool(code_format), "code_trigger_required": bool(code_format),
             "payload_arm_home": "ARM_HOME", "payload_arm_away": "ARM_AWAY", "payload_arm_night": "ARM_NIGHT",
             "payload_arm_vacation": "ARM_VACATION", "payload_arm_custom_bypass": "ARM_CUSTOM_BYPASS",
             "payload_disarm": "DISARM", "payload_trigger": "TRIGGER"}
        )
        if isinstance(features, int):
            # the main HA announces every arm mode it knows unless it is told which ones;
            # a mode the source cannot arm is a button there that fails here
            comp["supported_features"] = [name for name, bit in _ALARM_FEATURES.items() if features & bit]
        if code_format:
            comp["code"] = "REMOTE_CODE" if str(code_format).lower() == "number" else "REMOTE_CODE_TEXT"

    elif domain == "update":
        comp["value_template"] = _UPDATE_TPL
        if supports(1):  # UpdateEntityFeature.INSTALL; the command topic is what announces it there
            comp.update({"command_topic": f"{cmd}/install", "payload_install": "install"})
        if dc := _device_class(entry, attrs, domain, compat):
            comp["device_class"] = dc

    elif domain == "device_tracker":
        # MQTT device_tracker reads latitude/longitude/gps_accuracy from the JSON attributes, but what
        # comes in on the STATE topic becomes `location_name`, and a tracker with a location name never
        # looks at a zone (device_tracker/entity.py: `state` returns it before it evaluates anything).
        # The home/not_home this container computes is not an answer about the main Home Assistant: it
        # is headless, its `zone.home` sits at 0,0, so a phone standing in the user's kitchen was
        # `not_home` there.  The zones that matter are the main instance's, so whenever the document
        # carries real coordinates the state goes out as the reset payload and the main HA places the
        # device in its own zones.  A router or bluetooth tracker has no coordinates and its
        # home/not_home is the only truth there is: that one goes out as it stands.
        # Only those three payloads may ever go out - for anything else MQTT device_tracker takes the
        # RAW message as the location name, so a state like "Work" made the whole document the state.
        comp.update(
            {"value_template": _DEVICE_TRACKER_TPL,
             "payload_home": "home", "payload_not_home": "not_home", "payload_reset": "None"}
        )
        if attrs.get("source_type"):
            comp["source_type"] = attrs["source_type"]

    elif domain == "vacuum":
        # MQTT vacuum has no value template: it reads `state` and `fan_speed` from the top level of the
        # document (see document_extras); a battery level stays an attribute (the platform has none)
        comp.update(
            {
                "command_topic": f"{cmd}/command",
                "payload_start": "start", "payload_pause": "pause", "payload_stop": "stop",
                "payload_return_to_base": "return_to_base", "payload_clean_spot": "clean_spot", "payload_locate": "locate",
                "supported_features": list(_VACUUM_FEATURES) if not isinstance(features, int)
                else [name for name, bit in _VACUUM_FEATURES.items() if features & bit],
                "send_command_topic": f"{cmd}/send_command",
            }
        )
        if attrs.get("fan_speed_list"):
            comp.update({"fan_speed_list": list(attrs["fan_speed_list"]), "set_fan_speed_topic": f"{cmd}/fan_speed"})

    elif domain == "lawn_mower":
        comp.pop("state_topic", None)
        comp.update({"activity_state_topic": doc_topic, "activity_value_template": _STATE_TPL})
        # one command topic per LawnMowerEntityFeature bit: the main HA announces the
        # feature for every command topic it was given, and nothing for the ones it wasn't
        for action, bit in (("start_mowing", 1), ("pause", 2), ("dock", 4)):
            if supports(bit):
                comp.update({f"{action}_command_topic": f"{cmd}/command", f"{action}_command_template": action})

    return comp


# What the main HA's MQTT vacuum accepts as a state; anything else is dropped there, silently
_VACUUM_ACTIVITIES = frozenset({"cleaning", "docked", "idle", "paused", "returning", "error"})

# MQTT vacuum feature names and the VacuumEntityFeature bits they stand for
_VACUUM_FEATURES = {"start": 8192, "pause": 4, "stop": 8, "return_home": 16, "status": 128, "locate": 512,
                    "clean_spot": 1024, "fan_speed": 32, "send_command": 256}

# Same, for the MQTT alarm panel: the names its supported_features option takes and the
# AlarmControlPanelEntityFeature bits behind them.  Disarm is not a feature, every panel has it.
_ALARM_FEATURES = {"arm_home": 1, "arm_away": 2, "arm_night": 4, "trigger": 8,
                   "arm_custom_bypass": 16, "arm_vacation": 32}


def document_extras(state: State) -> dict[str, Any]:
    """Top-level keys of the entity document for an MQTT platform that reads the document without a value
    template: MQTT vacuum takes `fan_speed` and `state` from the top level (fan_speed is always present, so
    a speed that goes away clears on the main HA instead of keeping the last one).  The same values stay
    under `attributes`, and `state` here replaces the document's own.

    A vacuum is the one platform with no value template, so `_STATE_TPL` cannot turn an unknown state into
    the no-value payload: MQTT vacuum drops any state that is not one of its six activities and keeps the
    one it had, which left a stale activity on the main HA.  `null` is what that platform reads as "no
    activity", which is what `unknown` means here; `unavailable` takes the entity offline through its
    availability topic either way, and sending null with it keeps a stale activity from reappearing when
    it comes back."""
    if state.entity_id.startswith("vacuum."):
        return {"fan_speed": state.attributes.get("fan_speed"),
                "state": state.state if state.state in _VACUUM_ACTIVITIES else None}
    return {}


def event_stream_topic(doc_topic: str) -> str:
    """Non-retained companion topic of an event entity's document."""
    base, rest = doc_topic.split("/event/", 1)
    return f"{base}/event_stream/{rest}"


def _mirror_as_sensor(entry: er.RegistryEntry | None, state: State, doc_topic: str, prefix: str,
                      device_name: str | None = None) -> dict[str, Any]:
    """Read-only mirror for domains without an MQTT platform (camera,
    media_player, weather, remote, todo, calendar, ...): a sensor whose
    state is the entity state and whose attributes are the full attribute
    set, so automations on the consuming side still see everything."""
    domain, object_id = state.entity_id.split(".", 1)
    comp = _common(entry, state, doc_topic, prefix, device_name)
    comp.update(
        {
            "platform": "sensor",
            # a mirror of the entity that IS its device keeps no name of its own either:
            # the main HA shows the device's name and this suffix, not "None (camera)"
            "name": f"({domain})" if comp["name"] is None else f"{comp['name']} ({domain})",
            "default_entity_id": f"sensor.{domain}_{object_id}",
            "value_template": _STATE_TPL,
        }
    )
    comp.pop("icon", None)
    if comp.get("entity_category") == "config":
        # A date/time/datetime entity usually carries the config category, and the main Home Assistant refuses a
        # SENSOR that has it ("cannot be added as the entity category is set to config"): the entity would never
        # appear there and would show as permanently missing in parity.  The mirror is read-only whatever the
        # source is, which is what diagnostic says.
        comp["entity_category"] = "diagnostic"
    return comp


def build_component_from_entry(
    hass: HomeAssistant, entry: er.RegistryEntry, doc_topic: str, cmd_base: str, prefix: str,
    compat: Compat | None = None,
) -> dict[str, Any]:
    """Component for a registry entry that has no state (disabled, or not
    yet added by its integration): built from the registry's capabilities
    so the consuming HA creates it, disabled, with the right shape."""
    attrs: dict[str, Any] = dict(entry.capabilities or {})
    if isinstance(getattr(entry, "supported_features", None), int):
        attrs["supported_features"] = entry.supported_features  # the features to announce, as a state would have them
    if entry.unit_of_measurement:
        attrs["unit_of_measurement"] = entry.unit_of_measurement
    state = State(entry.entity_id, "unknown", attrs, validate_entity_id=False)
    comp = build_component(hass, state, doc_topic, cmd_base, prefix, compat)
    # "no state yet" is not "disabled": the registry loads before the
    # integration adds its entities, and the consumer only honours
    # enabled_by_default at first creation.
    comp["enabled_by_default"] = not entry.disabled
    return comp


def is_child_device(dev: Any) -> bool:
    """HA 2026.9+ ChildDeviceEntry (has parent_device_id, lacks manufacturer
    & co.; touching those attributes on it logs a deprecation)."""
    child_cls = getattr(dr, "ChildDeviceEntry", None)
    return child_cls is not None and isinstance(dev, child_cls)


def device_block(hass: HomeAssistant, device_id: str | None, integration: str, prefix: str) -> tuple[str, dict[str, Any]]:
    """Return (discovery_id, device dict) for a device or a per-integration bucket."""
    if device_id:
        dev = dr.async_get(hass).async_get(device_id)
        if dev:
            block: dict[str, Any] = {
                "identifiers": [f"{prefix}{dev.id}"],
                "name": dev.name_by_user or dev.name or dev.id,
            }
            if is_child_device(dev):
                # HA 2026.9 child device (e.g. a zone under its
                # controller): no manufacturer/model of its own; MQTT
                # discovery has no child concept, via_device keeps the grouping
                block["via_device"] = f"{prefix}{dev.parent_device_id}"
                return f"{prefix}{dev.id}", block
            for src, dst in (("manufacturer", "manufacturer"), ("model", "model"), ("sw_version", "sw_version"),
                             ("hw_version", "hw_version")):
                if getattr(dev, src, None):
                    block[dst] = getattr(dev, src)
            if getattr(dev, "via_device_id", None):
                block["via_device"] = f"{prefix}{dev.via_device_id}"
            return f"{prefix}{dev.id}", block
    return (
        f"{prefix}{integration}_nodevice",
        {"identifiers": [f"{prefix}{integration}_nodevice"], "name": f"{integration} (no device)"},
    )


# Manager actions on <base>/manager/cmd/<action> and the one payload each
# accepts: what the consuming HA sends from the update entity or the button.
# Anything else (an empty payload that clears a retained command, a typo) is refused.
MANAGER_ACTIONS = {"install_integration": "install", "install_home_assistant": "install", "restart": "restart",
                   "backup": "backup", "check_updates": "check"}
HEALTH_INTERVAL_S = 60
HEALTH_EXPIRE_AFTER_S = 3 * HEALTH_INTERVAL_S


def manager_device(key: str, prefix: str, topics: dict[str, str], integration: str | None, version: str,
                   commands: bool) -> tuple[str, dict[str, Any], dict[str, dict[str, Any]]]:
    """The manager as a device on the consuming HA: (discovery_id, device,
    components).  ``key`` is the base topic (hass_<domain>); ``topics`` holds
    its ``status``, ``health`` and ``manager`` topics and the manager
    command base ``cmd``.  Connectivity and health come from the health
    document; the update entities and resource sensors from the manager
    document (manager_device.py); the install actions and the buttons only
    with ``commands``.  Everything goes unavailable with the LWT."""
    integ = integration or "none"
    common = {"availability": [{"topic": topics["status"]}], "payload_available": "online", "payload_not_available": "offline"}
    health, mgr, cmd = topics["health"], topics["manager"], topics["cmd"]
    # health is republished every HEALTH_INTERVAL_S: a loop that stopped checking keeps the connection (and the
    # retained "ok") alive, so both health entities go unavailable when three publications in a row are missing
    expire = {"expire_after": HEALTH_EXPIRE_AFTER_S}
    comps: dict[str, dict[str, Any]] = {
        f"binary_sensor.{key}_integration": {**common, **expire, "platform": "binary_sensor", "name": f"{integ} integration", "device_class": "connectivity",
                                              "entity_category": "diagnostic", "json_attributes_topic": health,
                                              "unique_id": f"{prefix}health_online", "default_entity_id": f"binary_sensor.{key}_integration",
                                              "state_topic": health, "value_template": _tpl("'ON' if value_json.state in ['ok', 'degraded'] else 'OFF'")},
        f"sensor.{key}_health": {**common, **expire, "platform": "sensor", "name": f"{integ} health", "icon": "mdi:heart-pulse",
                                 "entity_category": "diagnostic", "json_attributes_topic": health,
                                 "unique_id": f"{prefix}health_state", "default_entity_id": f"sensor.{key}_health",
                                 "state_topic": health, "value_template": _tpl("value_json.state")},
    }

    def add(platform: str, suffix: str, name: str, extra: dict[str, Any], category: str | None = "diagnostic") -> None:
        entity_id = f"{platform}.{key}_{suffix}"
        comps[entity_id] = {**common, "platform": platform, "name": name, "unique_id": f"{prefix}manager_{suffix}",
                            "default_entity_id": entity_id, **({"entity_category": category} if category else {}), **extra}

    updates = [("home_assistant", "Home Assistant update"), ("manager", "hass-remote-integration update")]
    if integration:
        updates.insert(0, ("integration", f"{integ} update"))
    for part, name in updates:
        extra: dict[str, Any] = {"state_topic": mgr, "value_template": _tpl(f"value_json.updates.{part} | to_json")}
        if commands and part != "manager":  # the manager itself is updated by pulling a new image
            extra.update({"command_topic": f"{cmd}/install_{part}", "payload_install": MANAGER_ACTIONS[f"install_{part}"]})
        add("update", f"{part}_update", name, extra, category=None)
    measure = {"state_topic": mgr, "state_class": "measurement"}
    add("sensor", "memory", "Memory", {**measure, "value_template": _tpl("value_json.resources.memory_mb"), "device_class": "data_size",
                                       "unit_of_measurement": "MiB", "suggested_display_precision": 0})
    add("sensor", "cpu", "CPU", {**measure, "value_template": _tpl("value_json.resources.cpu_pct"), "unit_of_measurement": "%",
                                 "icon": "mdi:cpu-64-bit", "suggested_display_precision": 1})
    add("sensor", "loop_lag", "Event loop lag", {**measure, "value_template": _tpl("value_json.resources.loop_lag_max_ms"),
                                                 "device_class": "duration", "unit_of_measurement": "ms", "suggested_display_precision": 0})
    add("sensor", "volume_used", "Volume used", {**measure, "value_template": _tpl("value_json.resources.volume_used_pct"),
                                                 "unit_of_measurement": "%", "icon": "mdi:harddisk", "suggested_display_precision": 0})
    add("sensor", "patches", "Patches", {"state_topic": mgr, "value_template": _tpl("value_json.patches"), "icon": "mdi:bandage"})
    if commands:
        add("button", "restart", "Restart", {"command_topic": f"{cmd}/restart", "payload_press": MANAGER_ACTIONS["restart"], "device_class": "restart"},
            category="config")
        add("button", "backup", "Back up now", {"command_topic": f"{cmd}/backup", "payload_press": MANAGER_ACTIONS["backup"], "icon": "mdi:content-save"},
            category="config")
        add("button", "check_updates", "Check for updates", {"command_topic": f"{cmd}/check_updates", "payload_press": MANAGER_ACTIONS["check_updates"],
                                                             "icon": "mdi:update"}, category="config")
    block = {"identifiers": [f"{key}_manager"], "name": f"hass-remote-integration ({key})", "manufacturer": "hass-remote-integration",
             "model": f"integration manager, running {integ}", "sw_version": version}
    return f"{key}_manager", block, comps


def manager_entity_id(key: str, unique_id_suffix: str) -> str | None:
    """The entity id of the manager component whose unique id is ``<prefix><unique_id_suffix>``, or None when
    no component of the manager device has that unique id.

    The manager's components are named after what they do, not after their entity id (unique id
    ``manager_restart`` is ``button.<key>_restart``, ``health_online`` is
    ``binary_sensor.<key>_integration``), so a caller holding only the unique id - parity, which reads it off
    the main Home Assistant - cannot derive the component key: it is looked up in the device itself.  Every
    component is built, commands and an integration included, so one announced by an earlier configuration is
    found too."""
    _id, _block, comps = manager_device(key, "", dict.fromkeys(("status", "health", "manager", "cmd"), "t"),
                                        "x", "", True)
    return next((entity_id for entity_id, comp in comps.items() if comp["unique_id"] == unique_id_suffix), None)


def _json_or_text(payload: str) -> Any:
    try:
        return json.loads(payload)
    except (ValueError, RecursionError):
        return payload


def _finite(p: str) -> float:
    """A command value: NaN and infinity pass float() and every min/max comparison, so refuse them here."""
    value = float(p)
    if not math.isfinite(value):
        raise ValueError(f"{p!r} is not a finite number")
    return value


_ON_PAYLOADS, _OFF_PAYLOADS = frozenset({"ON", "TRUE", "1"}), frozenset({"OFF", "FALSE", "0"})


def _on_off(p: str) -> bool:
    """A state payload is one of the two token sets and nothing else: read as a plain "not on",
    a typo or a TOGGLE the consumer sends would silently turn the device off."""
    token = p.strip().upper()
    if token in _ON_PAYLOADS:
        return True
    if token in _OFF_PAYLOADS:
        return False
    raise ValueError(f"{p!r} is neither an on nor an off payload")


def _service_for(p: str, table: dict[str, Any]) -> Any:
    """What a command token stands for - a service name, or the value a service takes - in any case and with
    surrounding spaces ignored, like the on/off payloads.  The error names the accepted tokens but not the
    payload: it may carry a code (the log line quotes the payload, masked)."""
    token = p.strip().upper()
    if token not in table:
        raise ValueError(f"unknown command, expected one of: {', '.join(t.lower() for t in table)}")
    return table[token]


def _action_and_code(p: str) -> tuple[str, Any]:
    """An action payload of a code-protected platform (alarm panel, lock): the command template sends
    {"action": ..., "code": ...}, and a bare action (an older config, or a script publishing by hand)
    still works.  The payload may carry a code, so nothing of it is put in an error message."""
    if not p.strip().startswith("{"):
        return p, None
    try:
        body = json.loads(p)
    except (ValueError, RecursionError):
        body = {}
    if not isinstance(body, dict):
        return p, None
    return str(body.get("action") or ""), body.get("code")


def _with_code(t: dict[str, Any], code: Any) -> dict[str, Any]:
    """Service data with the code the user typed on the consuming side, when there is one."""
    return {**t, "code": str(code)} if code not in (None, "") else t


_TARGET_KEYS = ("entity_id", "device_id", "area_id", "floor_id", "label_id")


def _pick(field: str, table: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    """Only the chosen field's conversion runs: a literal table would call
    float("heat") while building the entry for "temperature"."""
    fn = table.get(field)
    return fn() if fn else None


def command_to_service(domain: str, object_id: str, field: str, payload: str) -> tuple[str, str, dict[str, Any]] | None:
    """Map an incoming entity command topic to (domain, service, data)."""
    entity_id = f"{domain}.{object_id}"
    p = payload.strip()  # protocol tokens and numbers; literal values (text, message, option) use the payload as sent
    t = {"entity_id": entity_id}

    if domain == "climate":
        return _pick(field, {
            "temperature": lambda: ("climate", "set_temperature", {**t, "temperature": _finite(p)}),
            "temperature_high": lambda: ("climate", "set_temperature", {**t, "target_temp_high": _finite(p)}),
            "temperature_low": lambda: ("climate", "set_temperature", {**t, "target_temp_low": _finite(p)}),
            "mode": lambda: ("climate", "set_hvac_mode", {**t, "hvac_mode": p}),
            "preset_mode": lambda: ("climate", "set_preset_mode", {**t, "preset_mode": p}),
            "fan_mode": lambda: ("climate", "set_fan_mode", {**t, "fan_mode": p}),
            "swing_mode": lambda: ("climate", "set_swing_mode", {**t, "swing_mode": p}),
            "swing_horizontal_mode": lambda: ("climate", "set_swing_horizontal_mode", {**t, "swing_horizontal_mode": p}),
            "humidity": lambda: ("climate", "set_humidity", {**t, "humidity": int(_finite(p))}),
            "power": lambda: ("climate", "turn_on" if _on_off(p) else "turn_off", t),
        })
    if domain == "water_heater":
        return _pick(field, {
            "temperature": lambda: ("water_heater", "set_temperature", {**t, "temperature": _finite(p)}),
            "mode": lambda: ("water_heater", "set_operation_mode", {**t, "operation_mode": p}),
            "power": lambda: ("water_heater", "turn_on" if _on_off(p) else "turn_off", t),
        })
    if domain == "switch" and field == "state":
        return "switch", "turn_on" if _on_off(p) else "turn_off", t
    if domain == "select" and field == "option":
        return "select", "select_option", {**t, "option": payload}  # options match exactly: "eco " is not "eco"
    if domain == "number" and field == "value":
        return "number", "set_value", {**t, "value": _finite(p)}
    if domain == "light":
        if field == "state":
            return "light", "turn_on" if _on_off(p) else "turn_off", t
        return _pick(field, {
            "brightness": lambda: ("light", "turn_on", {**t, "brightness": int(_finite(p))}),
            "color_temp": lambda: ("light", "turn_on", {**t, "color_temp_kelvin": int(_finite(p))}),
            "rgb": lambda: ("light", "turn_on", {**t, "rgb_color": [int(x) for x in p.split(",")]}),
            # the main HA renders these the same way it renders rgb: the channels, comma separated
            "rgbw": lambda: ("light", "turn_on", {**t, "rgbw_color": [int(x) for x in p.split(",")]}),
            "rgbww": lambda: ("light", "turn_on", {**t, "rgbww_color": [int(x) for x in p.split(",")]}),
            "effect": lambda: ("light", "turn_on", {**t, "effect": p}),
        })
    if domain == "cover":
        if field == "command":
            return "cover", _service_for(p, {"OPEN": "open_cover", "CLOSE": "close_cover", "STOP": "stop_cover"}), t
        if field == "position":
            return "cover", "set_cover_position", {**t, "position": int(_finite(p))}
        if field == "tilt":
            # MQTT cover sends its stop-tilt payload to the tilt command topic, and for a source that cannot set a
            # tilt position the tilt command template turns the open/close boundary values into these tokens.
            if p.upper() in ("OPEN", "CLOSE", "STOP"):
                return "cover", _service_for(p, {"OPEN": "open_cover_tilt", "CLOSE": "close_cover_tilt",
                                                 "STOP": "stop_cover_tilt"}), t
            return "cover", "set_cover_tilt_position", {**t, "tilt_position": int(_finite(p))}
    if domain == "valve":
        if field == "command":
            return "valve", _service_for(p, {"OPEN": "open_valve", "CLOSE": "close_valve", "STOP": "stop_valve"}), t
        if field == "position":
            if p.upper() in ("OPEN", "CLOSE", "STOP"):  # a position valve sends its stop payload to the same topic
                return "valve", _service_for(p, {"OPEN": "open_valve", "CLOSE": "close_valve", "STOP": "stop_valve"}), t
            return "valve", "set_valve_position", {**t, "position": int(_finite(p))}
    if domain == "fan":
        if field == "state":
            return "fan", "turn_on" if _on_off(p) else "turn_off", t
        return _pick(field, {
            "percentage": lambda: ("fan", "set_percentage", {**t, "percentage": int(_finite(p))}),
            "preset_mode": lambda: ("fan", "set_preset_mode", {**t, "preset_mode": p}),
            # not `p == "oscillate_on"`: that reads every other payload, a typo included, as "stop
            # oscillating" and calls the service.  The token is checked like every other command token,
            # so a malformed one is refused with the accepted list instead of moving the fan.
            "oscillate": lambda: ("fan", "oscillate",
                                  {**t, "oscillating": _service_for(p, {"OSCILLATE_ON": True, "OSCILLATE_OFF": False})}),
            "direction": lambda: ("fan", "set_direction", {**t, "direction": p}),
        })
    if domain == "lock" and field == "command":
        action, code = _action_and_code(p)
        return "lock", _service_for(action, {"LOCK": "lock", "UNLOCK": "unlock", "OPEN": "open"}), _with_code(t, code)
    if domain == "button" and field == "press":
        return "button", "press", t
    if domain == "scene" and field == "activate":
        return "scene", "turn_on", t
    if domain == "notify" and field == "message":
        return "notify", "send_message", {**t, "message": payload}
    if domain == "text" and field == "value":
        return "text", "set_value", {**t, "value": payload}  # leading/trailing spaces are part of the text
    if domain in ("date", "time", "datetime") and field == "value":
        return domain, "set_value", {**t, domain: p}
    if domain == "siren" and field == "state":
        data = _json_or_text(p)
        if isinstance(data, dict):  # MQTT siren sends {"state": "ON", "tone": ..., "duration": ...}
            extra = {k: v for k, v in data.items() if k in ("tone", "duration", "volume_level")}
            return "siren", "turn_on" if _on_off(str(data.get("state", ""))) else "turn_off", {**t, **extra}
        return "siren", "turn_on" if _on_off(p) else "turn_off", t
    if domain == "humidifier":
        if field == "state":
            return "humidifier", "turn_on" if _on_off(p) else "turn_off", t
        return _pick(field, {
            "humidity": lambda: ("humidifier", "set_humidity", {**t, "humidity": int(_finite(p))}),
            "mode": lambda: ("humidifier", "set_mode", {**t, "mode": p}),
        })
    if domain == "alarm_control_panel" and field == "command":
        action, code = _action_and_code(p)
        svc = _service_for(action, {
            "ARM_HOME": "alarm_arm_home", "ARM_AWAY": "alarm_arm_away", "ARM_NIGHT": "alarm_arm_night",
            "ARM_VACATION": "alarm_arm_vacation", "ARM_CUSTOM_BYPASS": "alarm_arm_custom_bypass",
            "DISARM": "alarm_disarm", "TRIGGER": "alarm_trigger",
        })
        return "alarm_control_panel", svc, _with_code(t, code)
    if domain == "update" and field == "install":
        return "update", "install", t
    if domain == "vacuum":
        if field == "command":
            return "vacuum", _service_for(p, {"START": "start", "PAUSE": "pause", "STOP": "stop", "RETURN_TO_BASE": "return_to_base",
                                              "CLEAN_SPOT": "clean_spot", "LOCATE": "locate"}), t
        if field == "fan_speed":
            return "vacuum", "set_fan_speed", {**t, "fan_speed": p}
        if field == "send_command":
            data = _json_or_text(p)
            if isinstance(data, dict):
                # MQTT vacuum sends {"command": ..., <params flattened>}: every other key is a parameter, except a
                # target key, which cannot retarget the command (Home Assistant would add it to the entity)
                if not isinstance(data.get("command"), str) or not data["command"].strip():
                    raise ValueError('a JSON send_command needs a "command" string')
                params = {k: v for k, v in data.items() if k != "command" and k not in _TARGET_KEYS}
                if set(params) == {"params"} and isinstance(params["params"], dict):
                    # the shape 0.17.0 and older took ({"command": ..., "params": {...}}): a script written for it
                    # keeps working, and the main HA never sends a lone parameter named "params" holding an object
                    params = params["params"]
                return "vacuum", "send_command", {**t, "command": data["command"], **({"params": params} if params else {})}
            return "vacuum", "send_command", {**t, "command": p}
    if domain == "lawn_mower" and field == "command":
        return "lawn_mower", _service_for(p, {"START_MOWING": "start_mowing", "PAUSE": "pause", "DOCK": "dock"}), t
    return None
