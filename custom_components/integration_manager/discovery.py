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

import math

import json
from typing import Any

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

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


def _common(entry: er.RegistryEntry | None, state: State, doc_topic: str, prefix: str) -> dict[str, Any]:
    domain, object_id = state.entity_id.split(".", 1)
    name = None
    if entry:
        name = entry.name or entry.original_name
    if not name:
        name = state.attributes.get("friendly_name") or object_id
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
            {"topic": doc_topic, "value_template": _tpl("'offline' if value_json.state == 'unavailable' else 'online'")},
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


def _device_class(entry: er.RegistryEntry | None, attrs: dict[str, Any]) -> str | None:
    if entry and (entry.device_class or entry.original_device_class):
        return entry.device_class or entry.original_device_class
    return attrs.get("device_class")


def build_component(
    hass: HomeAssistant, state: State, doc_topic: str, cmd_base: str, prefix: str
) -> dict[str, Any]:
    """Return the discovery component for one entity (never None: unmapped
    domains fall back to a read-only sensor mirror)."""
    domain, object_id = state.entity_id.split(".", 1)
    entry = er.async_get(hass).async_get(state.entity_id)
    attrs = dict(state.attributes)
    cmd = f"{cmd_base}/{domain}/{object_id}"

    if domain not in NATIVE:
        return _mirror_as_sensor(entry, state, doc_topic, prefix)

    comp = _common(entry, state, doc_topic, prefix)
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
        if dc := _device_class(entry, attrs):
            comp["device_class"] = dc
        if dc == "enum" and attrs.get("options"):
            comp["options"] = list(attrs["options"])

    elif domain == "binary_sensor":
        comp.update({"value_template": _STATE_TPL, "payload_on": "on", "payload_off": "off"})
        if dc := _device_class(entry, attrs):
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
        if attrs.get("target_temp_high") is not None or attrs.get("target_temp_low") is not None:
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
        if attrs.get("current_humidity") is not None:
            comp.update({"current_humidity_topic": doc_topic, "current_humidity_template": _attr('current_humidity')})

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

    elif domain == "switch":
        comp.update(
            {"value_template": _STATE_TPL, "payload_on": "on", "payload_off": "off",
             "state_on": "on", "state_off": "off", "command_topic": f"{cmd}/state"}
        )
        if dc := _device_class(entry, attrs):
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
        if dc := _device_class(entry, attrs):
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
        if modes & {"rgb", "hs", "xy", "rgbw", "rgbww"}:
            comp.update(
                {"rgb_state_topic": doc_topic, "rgb_value_template": _tpl("(value_json.attributes.get('rgb_color') or []) | join(',')"),
                 "rgb_command_topic": f"{cmd}/rgb"}
            )
        if attrs.get("effect_list"):
            comp.update(
                {"effect_list": list(attrs["effect_list"]), "effect_state_topic": doc_topic,
                 "effect_value_template": _attr('effect'), "effect_command_topic": f"{cmd}/effect"}
            )

    elif domain == "cover":
        # MQTT cover derives its features from the topics and payloads: announce only what the source supports
        # (CoverEntityFeature bits), or a tilt-only cover gets open/close/stop buttons that fail here
        features = attrs.get("supported_features")

        def supports(bits: int) -> bool:
            return not isinstance(features, int) or bool(features & bits)

        comp.update(
            {"value_template": _STATE_TPL,
             "state_open": "open", "state_closed": "closed", "state_opening": "opening",
             "state_closing": "closing", "state_stopped": "stopped"}
        )
        moves = {"payload_open": ("OPEN", 1), "payload_close": ("CLOSE", 2), "payload_stop": ("STOP", 8)}
        if supports(1 | 2 | 8):
            comp["command_topic"] = f"{cmd}/command"
            comp.update({key: payload if supports(bit) else None for key, (payload, bit) in moves.items()})
        if dc := _device_class(entry, attrs):
            comp["device_class"] = dc
        if attrs.get("current_position") is not None:
            comp.update({"position_topic": doc_topic, "position_template": _attr_or_empty('current_position')})
            if supports(4):
                comp["set_position_topic"] = f"{cmd}/position"
        if attrs.get("current_tilt_position") is not None:
            comp.update({"tilt_status_topic": doc_topic, "tilt_status_template": _attr_or_empty('current_tilt_position')})
            if supports(16 | 32 | 64 | 128):  # the main HA turns a tilt command topic into all four tilt features
                comp["tilt_command_topic"] = f"{cmd}/tilt"

    elif domain == "valve":
        comp.update(
            {"value_template": _STATE_TPL, "command_topic": f"{cmd}/command",
             "payload_open": "OPEN", "payload_close": "CLOSE", "payload_stop": "STOP"}
        )
        if dc := _device_class(entry, attrs):
            comp["device_class"] = dc
        if attrs.get("current_position") is not None:
            # a position-reporting valve may not carry open/close payloads
            for k in ("payload_open", "payload_close"):
                comp.pop(k, None)
            comp.update(
                {"reports_position": True, "value_template": _attr('current_position'),
                 "command_topic": f"{cmd}/position"}
            )

    elif domain == "fan":
        comp.update({"state_value_template": _onoff("value_json.state"), "command_topic": f"{cmd}/state", "payload_on": "ON", "payload_off": "OFF"})
        if attrs.get("percentage") is not None or attrs.get("percentage_step") is not None:
            comp.update(
                {"percentage_state_topic": doc_topic, "percentage_value_template": _attr('percentage'),
                 "percentage_command_topic": f"{cmd}/percentage"}
            )
        if attrs.get("preset_modes"):
            comp.update(
                {"preset_modes": list(attrs["preset_modes"]), "preset_mode_state_topic": doc_topic,
                 "preset_mode_value_template": _attr('preset_mode'), "preset_mode_command_topic": f"{cmd}/preset_mode"}
            )
        if attrs.get("oscillating") is not None:
            comp.update(
                {"oscillation_state_topic": doc_topic,
                 "oscillation_value_template": _tpl("'oscillate_on' if value_json.attributes.get('oscillating') else 'oscillate_off'"),
                 "oscillation_command_topic": f"{cmd}/oscillate"}
            )
        if attrs.get("direction") is not None:
            comp.update(
                {"direction_state_topic": doc_topic, "direction_value_template": _attr('direction'),
                 "direction_command_topic": f"{cmd}/direction"}
            )

    elif domain == "lock":
        comp.update(
            {"value_template": _STATE_TPL, "state_locked": "locked", "state_unlocked": "unlocked",
             "state_locking": "locking", "state_unlocking": "unlocking", "state_jammed": "jammed", "state_open": "open",
             "command_topic": f"{cmd}/command", "payload_lock": "LOCK", "payload_unlock": "UNLOCK", "payload_open": "OPEN"}
        )

    elif domain == "button":
        comp.update({"command_topic": f"{cmd}/press", "payload_press": "PRESS"})
        if dc := _device_class(entry, attrs):
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
        if dc := _device_class(entry, attrs):
            comp["device_class"] = dc

    elif domain == "text":
        comp.update({"value_template": _STATE_TPL, "command_topic": f"{cmd}/value"})
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
                "min_humidity": _num(attrs.get("min_humidity"), 0),
                "max_humidity": _num(attrs.get("max_humidity"), 100),
            }
        )
        if dc := _device_class(entry, attrs):
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
        if code_format:
            comp["code"] = "REMOTE_CODE" if str(code_format).lower() == "number" else "REMOTE_CODE_TEXT"

    elif domain == "update":
        comp.update(
            {
                # MQTT update validates the rendered JSON as a whole and drops all of it for one null field
                # (a release_url of None is enough): only the fields that have a value go out
                "value_template": _tpl(
                    "dict(value_json.attributes.items() | selectattr('0', 'in', ['installed_version', 'latest_version', 'title',"
                    " 'release_url', 'release_summary', 'in_progress']) | rejectattr('1', 'none') | list) | to_json"
                ),
                "command_topic": f"{cmd}/install",
                "payload_install": "install",
            }
        )
        if dc := _device_class(entry, attrs):
            comp["device_class"] = dc

    elif domain == "device_tracker":
        # MQTT device_tracker reads latitude/longitude/gps_accuracy from the
        # JSON attributes and the zone/home state from the state topic.
        comp["value_template"] = _STATE_TPL
        if attrs.get("source_type"):
            comp["source_type"] = attrs["source_type"]

    elif domain == "vacuum":
        # MQTT vacuum has no value template: it reads `state` and `fan_speed` from the top level of the
        # document (see document_extras); a battery level stays an attribute (the platform has none)
        features = attrs.get("supported_features")
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
        comp.update(
            {"activity_state_topic": doc_topic, "activity_value_template": _STATE_TPL,
             "start_mowing_command_topic": f"{cmd}/command", "start_mowing_command_template": "start_mowing",
             "pause_command_topic": f"{cmd}/command", "pause_command_template": "pause",
             "dock_command_topic": f"{cmd}/command", "dock_command_template": "dock"}
        )

    return comp


# MQTT vacuum feature names and the VacuumEntityFeature bits they stand for
_VACUUM_FEATURES = {"start": 8192, "pause": 4, "stop": 8, "return_home": 16, "status": 128, "locate": 512,
                    "clean_spot": 1024, "fan_speed": 32, "send_command": 256}


def document_extras(state: State) -> dict[str, Any]:
    """Top-level keys of the entity document for an MQTT platform that reads the document without a value
    template: MQTT vacuum takes `fan_speed` from the top level (always present, so a speed that goes away
    clears on the main HA instead of keeping the last one).  The same values stay under `attributes`."""
    if state.entity_id.startswith("vacuum."):
        return {"fan_speed": state.attributes.get("fan_speed")}
    return {}


def event_stream_topic(doc_topic: str) -> str:
    """Non-retained companion topic of an event entity's document."""
    base, rest = doc_topic.split("/event/", 1)
    return f"{base}/event_stream/{rest}"


def _mirror_as_sensor(entry: er.RegistryEntry | None, state: State, doc_topic: str, prefix: str) -> dict[str, Any]:
    """Read-only mirror for domains without an MQTT platform (camera,
    media_player, weather, remote, todo, calendar, ...): a sensor whose
    state is the entity state and whose attributes are the full attribute
    set, so automations on the consuming side still see everything."""
    domain, object_id = state.entity_id.split(".", 1)
    comp = _common(entry, state, doc_topic, prefix)
    comp.update(
        {
            "platform": "sensor",
            "name": f"{comp['name']} ({domain})",
            "default_entity_id": f"sensor.{domain}_{object_id}",
            "value_template": _STATE_TPL,
        }
    )
    comp.pop("icon", None)
    return comp


def build_component_from_entry(
    hass: HomeAssistant, entry: er.RegistryEntry, doc_topic: str, cmd_base: str, prefix: str
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
    comp = build_component(hass, state, doc_topic, cmd_base, prefix)
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


def _service_for(p: str, table: dict[str, str]) -> str:
    """The service a command token stands for, in any case and with surrounding spaces ignored, like the on/off
    payloads.  The error names the accepted tokens but not the payload: it may carry a code (the log line quotes
    the payload, masked)."""
    token = p.strip().upper()
    if token not in table:
        raise ValueError(f"unknown command, expected one of: {', '.join(t.lower() for t in table)}")
    return table[token]


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
        })
    if domain == "water_heater":
        return _pick(field, {
            "temperature": lambda: ("water_heater", "set_temperature", {**t, "temperature": _finite(p)}),
            "mode": lambda: ("water_heater", "set_operation_mode", {**t, "operation_mode": p}),
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
            "effect": lambda: ("light", "turn_on", {**t, "effect": p}),
        })
    if domain == "cover":
        if field == "command":
            return "cover", _service_for(p, {"OPEN": "open_cover", "CLOSE": "close_cover", "STOP": "stop_cover"}), t
        if field == "position":
            return "cover", "set_cover_position", {**t, "position": int(_finite(p))}
        if field == "tilt":
            if p.upper() == "STOP":  # MQTT cover sends its stop-tilt payload to the tilt command topic
                return "cover", "stop_cover_tilt", t
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
            "oscillate": lambda: ("fan", "oscillate", {**t, "oscillating": p == "oscillate_on"}),
            "direction": lambda: ("fan", "set_direction", {**t, "direction": p}),
        })
    if domain == "lock" and field == "command":
        return "lock", _service_for(p, {"LOCK": "lock", "UNLOCK": "unlock", "OPEN": "open"}), t
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
        action, code = p, None
        if p.strip().startswith("{"):  # {"action": ..., "code": ...} from the command template; a bare action still works
            try:
                body = json.loads(p)
            except (ValueError, RecursionError):
                body = {}
            if isinstance(body, dict):
                action, code = str(body.get("action") or ""), body.get("code")
        svc = _service_for(action, {
            "ARM_HOME": "alarm_arm_home", "ARM_AWAY": "alarm_arm_away", "ARM_NIGHT": "alarm_arm_night",
            "ARM_VACATION": "alarm_arm_vacation", "ARM_CUSTOM_BYPASS": "alarm_arm_custom_bypass",
            "DISARM": "alarm_disarm", "TRIGGER": "alarm_trigger",
        })
        return "alarm_control_panel", svc, ({**t, "code": str(code)} if code not in (None, "") else t)
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
                return "vacuum", "send_command", {**t, "command": data["command"], **({"params": params} if params else {})}
            return "vacuum", "send_command", {**t, "command": p}
    if domain == "lawn_mower" and field == "command":
        return "lawn_mower", _service_for(p, {"START_MOWING": "start_mowing", "PAUSE": "pause", "DOCK": "dock"}), t
    return None
