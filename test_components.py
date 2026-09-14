"""Validate the discovery components of EVERY mapped domain against the
MQTT platform schemas of the Home Assistant version in the image, without
publishing anything.  Run inside the container:

    sh verify.sh test      (runs this file in the container's Home Assistant venv)

Builds one synthetic State per domain (with the attributes that unlock the
optional features), runs it through discovery.build_component, then feeds
the result to the same DISCOVERY_SCHEMA the consuming HA applies when it
receives the device payload.  Exit code 1 on any rejection.
"""

import asyncio
import importlib
import sys
import tempfile

sys.path.insert(0, "/config")

from homeassistant import bootstrap, config_entries, core, loader  # noqa: E402
from homeassistant.core import State  # noqa: E402

from custom_components.integration_manager import discovery as disc  # noqa: E402

CASES = {
    "sensor.t": ("21.5", {"unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"}),
    "binary_sensor.b": ("on", {"device_class": "window"}),
    "climate.c": ("heat", {"hvac_modes": ["off", "heat", "auto"], "preset_modes": ["none", "eco"], "current_temperature": 21,
                           "temperature": 20, "min_temp": 5, "max_temp": 35, "fan_modes": ["low", "high"], "swing_modes": ["on", "off"],
                           "current_humidity": 40}),
    "water_heater.w": ("eco", {"operation_list": ["off", "eco"], "current_temperature": 50, "temperature": 55}),
    "switch.s": ("on", {}),
    "select.s": ("a", {"options": ["a", "b"]}),
    "number.n": ("5", {"min": 0, "max": 10, "step": 0.5, "mode": "slider", "unit_of_measurement": "%"}),
    "light.l": ("on", {"supported_color_modes": ["color_temp", "rgb"], "brightness": 100, "color_temp_kelvin": 3000,
                       "min_color_temp_kelvin": 2000, "max_color_temp_kelvin": 6500, "rgb_color": [1, 2, 3], "effect_list": ["x"]}),
    "cover.c": ("open", {"device_class": "shutter", "current_position": 50, "current_tilt_position": 10}),
    "valve.v": ("open", {"current_position": 30}),
    "fan.f": ("on", {"percentage": 50, "preset_modes": ["auto"], "oscillating": True, "direction": "forward"}),
    "lock.l": ("locked", {}),
    "button.b": ("unknown", {"device_class": "restart"}),
    "scene.s": ("unknown", {}),
    "notify.n": ("unknown", {}),
    "event.e": ("2026-01-01T00:00:00+00:00", {"event_types": ["press", "hold"], "event_type": "press"}),
    "text.t": ("hello", {"min": 0, "max": 100, "mode": "text"}),
    "date.d": ("2026-01-01", {}),
    "time.t": ("10:00:00", {}),
    "datetime.d": ("2026-01-01T10:00:00+00:00", {}),
    "siren.s": ("off", {"available_tones": ["a"]}),
    "humidifier.h": ("on", {"humidity": 50, "current_humidity": 45, "min_humidity": 30, "max_humidity": 70, "available_modes": ["auto"],
                            "device_class": "humidifier"}),
    "alarm_control_panel.a": ("disarmed", {}),
    "update.u": ("off", {"installed_version": "1", "latest_version": "1", "title": "x", "release_url": None}),
    "device_tracker.d": ("home", {"source_type": "gps", "latitude": 1.0, "longitude": 2.0}),
    "vacuum.v": ("docked", {"battery_level": 90, "fan_speed": "max", "fan_speed_list": ["min", "max"]}),
    "lawn_mower.m": ("docked", {}),
    # no MQTT platform -> read-only sensor mirror
    "media_player.tv": ("playing", {"volume_level": 0.5, "media_title": "x"}),
    "weather.w": ("sunny", {"temperature": 20}),
    "camera.c": ("idle", {}),
}

SCHEMAS = {  # platform -> (module, attribute)
    "light": ("light.schema_basic", "DISCOVERY_SCHEMA_BASIC"),
}


def schema_for(platform: str):
    mod, attr = SCHEMAS.get(platform, (platform, "DISCOVERY_SCHEMA"))
    return getattr(importlib.import_module(f"homeassistant.components.mqtt.{mod}"), attr)


async def main() -> int:
    hass = core.HomeAssistant(tempfile.mkdtemp())
    loader.async_setup(hass)
    hass.config_entries = config_entries.ConfigEntries(hass, {})
    await bootstrap.async_load_base_functionality(hass)
    failures = 0
    for entity_id, (st, attrs) in CASES.items():
        state = State(entity_id, st, attrs)
        comp = disc.build_component(hass, state, f"hass_test/x/{entity_id.replace('.', '/')}", "hass_test/cmd", "hass_test_")
        platform = comp["platform"]
        # What the consuming HA validates: the component merged with the
        # device-level keys, minus the "platform" discriminator.
        payload = {k: v for k, v in comp.items() if k != "platform"}
        payload.update({"device": {"identifiers": ["hass_test_x"], "name": "t"}, "origin": disc.ORIGIN,
                        "payload_available": "online", "payload_not_available": "offline"})
        try:
            schema_for(platform)(payload)
            extra = "" if platform == entity_id.split(".")[0] else f"  (mirrored as {platform})"
            print(f"  ok   {entity_id:24s} {len(comp):3d} keys{extra}")
        except Exception as err:  # noqa: BLE001
            failures += 1
            print(f"  FAIL {entity_id:24s} {platform}: {err}")
    await hass.async_stop()
    print(f"{len(CASES) - failures}/{len(CASES)} components valid")
    return 1 if failures else 0


sys.exit(asyncio.run(main()))
