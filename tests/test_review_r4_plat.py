"""Fourth review, platform fidelity: the fields a platform carries, the features it announces
and the name it shows, checked against the MQTT platforms of the Home Assistant in this venv.

Every case here builds the real entity of the main Home Assistant out of the component this
container publishes and feeds it the real entity document, the way ``test_e2e_disc`` does: what
is asserted is what the main instance ends up with, not the shape of the dict we produced.
"""

import unittest
from unittest import mock

from homeassistant import config_entries
from homeassistant.core import State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.integration_manager import discovery as disc

from .test_e2e_disc import BASE, Consumer, _CLASSES, _HassCase, document

# the platforms this file drives, on top of the ones test_e2e_disc already builds
_CLASSES.update(
    {
        "sensor": ("sensor", "MqttSensor", "DISCOVERY_SCHEMA"),
        "climate": ("climate", "MqttClimate", "DISCOVERY_SCHEMA"),
        "humidifier": ("humidifier", "MqttHumidifier", "DISCOVERY_SCHEMA"),
        "alarm_control_panel": ("alarm_control_panel", "MqttAlarm", "DISCOVERY_SCHEMA"),
        "valve": ("valve", "MqttValve", "DISCOVERY_SCHEMA"),
    }
)


class LockStatesTest(_HassCase):
    """R3-18: `opening` is one of the seven LockState values and the only one that had no
    payload, so a lock opening its door showed as unknown on the main Home Assistant."""

    async def test_every_lock_state_arrives(self):
        consumer = Consumer(self.hass, State("lock.l", "locked", {}))
        for st in ("locked", "unlocked", "locking", "unlocking", "jammed", "open", "opening"):
            with self.subTest(state=st):
                self.assertQuiet(consumer, State("lock.l", st, {}))
                self.assertEqual(consumer.entity.state, st)
        self.assertTrue(Consumer(self.hass, State("lock.l", "opening", {})).component["state_opening"] == "opening")

    async def test_opening_is_not_unknown(self):
        consumer = Consumer(self.hass, State("lock.l", "opening", {}))
        self.assertQuiet(consumer, State("lock.l", "opening", {}))
        self.assertTrue(consumer.entity.is_opening)  # was None: no state_opening was sent


class SensorLastResetTest(_HassCase):
    """R3-18: a `total` sensor's last_reset never reached the main HA - MQTT blocks it from the
    JSON attributes, so only last_reset_value_template carries it."""

    ATTRS = {"state_class": "total", "device_class": "energy", "unit_of_measurement": "kWh"}

    async def test_last_reset_reaches_the_main_ha(self):
        attrs = {**self.ATTRS, "last_reset": "2026-01-01T00:00:00+00:00"}
        state = State("sensor.e", "12.5", attrs)
        consumer = Consumer(self.hass, state)
        self.assertQuiet(consumer, state)
        self.assertEqual(consumer.entity.native_value, "12.5")
        self.assertIsNotNone(consumer.entity.last_reset)  # was None
        self.assertEqual(consumer.entity.last_reset.isoformat(), "2026-01-01T00:00:00+00:00")
        # a new cycle: the value drops and last_reset moves with it
        self.assertQuiet(consumer, State("sensor.e", "0.2", {**self.ATTRS, "last_reset": "2026-02-01T00:00:00+00:00"}))
        self.assertEqual(consumer.entity.last_reset.isoformat(), "2026-02-01T00:00:00+00:00")

    async def test_a_sensor_without_one_stays_quiet(self):
        state = State("sensor.e", "12.5", self.ATTRS)
        consumer = Consumer(self.hass, state)
        self.assertQuiet(consumer, state)
        self.assertIsNone(consumer.entity.last_reset)
        self.assertQuiet(consumer, State("sensor.e", "unknown", self.ATTRS))

    async def test_the_platform_takes_the_template_only_with_total(self):
        """The template is refused with any other state class, which would cost the whole
        device its payload: it goes out for `total` and for nothing else."""
        from homeassistant.components.mqtt.sensor import DISCOVERY_SCHEMA

        for state_class in ("total", "total_increasing", "measurement", None):
            with self.subTest(state_class=state_class):
                attrs = {"unit_of_measurement": "kWh", "device_class": "energy"}
                if state_class:
                    attrs["state_class"] = state_class
                comp = Consumer(self.hass, State("sensor.e", "1", attrs)).component
                self.assertEqual("last_reset_value_template" in comp, state_class == "total")
                payload = {k: v for k, v in comp.items() if k != "platform"}
                payload.update({"device": {"identifiers": [f"{BASE}_x"], "name": "t"}, "origin": disc.ORIGIN})
                DISCOVERY_SCHEMA(payload)


class UpdateFieldsTest(_HassCase):
    """R3-18: MQTT update validates the rendered JSON as one document and keeps nothing when a
    field is wrong - a relative release_url cost the entity its versions too."""

    BASE_ATTRS = {"installed_version": "1.0", "latest_version": "1.1", "title": "Firmware"}

    async def test_update_percentage_arrives(self):
        attrs = {**self.BASE_ATTRS, "in_progress": True, "update_percentage": 42}
        state = State("update.u", "on", attrs)
        consumer = Consumer(self.hass, state)
        self.assertQuiet(consumer, state)
        self.assertEqual(consumer.entity.update_percentage, 42)  # was None
        self.assertTrue(consumer.entity.in_progress)
        # the install finishes: the percentage is gone at the source, and a field left out of the
        # document keeps the value it had there, so it goes out as null and clears the bar
        self.assertQuiet(consumer, State("update.u", "off", {**self.BASE_ATTRS, "in_progress": False}))
        self.assertIsNone(consumer.entity.update_percentage)
        self.assertFalse(consumer.entity.in_progress)
        self.assertEqual(consumer.entity.installed_version, "1.0")

    async def test_a_relative_release_url_costs_nothing_else(self):
        attrs = {**self.BASE_ATTRS, "release_url": "/local/notes.html"}
        state = State("update.u", "on", attrs)
        consumer = Consumer(self.hass, state)
        self.assertQuiet(consumer, state)  # was "Schema violation ...: invalid url at 'release_url'"
        self.assertEqual((consumer.entity.installed_version, consumer.entity.latest_version), ("1.0", "1.1"))
        self.assertIsNone(consumer.entity.release_url)

    async def test_an_absolute_release_url_still_arrives(self):
        for url in ("https://example.invalid/notes", "http://example.invalid/notes"):
            with self.subTest(url=url):
                state = State("update.u", "on", {**self.BASE_ATTRS, "release_url": url})
                consumer = Consumer(self.hass, state)
                self.assertQuiet(consumer, state)
                self.assertEqual(consumer.entity.release_url, url)

    async def test_a_percentage_the_platform_refuses_is_left_out(self):
        for bad in ("42%", 120, -1):
            with self.subTest(percentage=bad):
                attrs = {**self.BASE_ATTRS, "update_percentage": bad}
                state = State("update.u", "on", attrs)
                consumer = Consumer(self.hass, state)
                self.assertQuiet(consumer, state)
                self.assertEqual(consumer.entity.installed_version, "1.0")
                self.assertIsNone(consumer.entity.update_percentage)


class LightColourAndEffectTest(_HassCase):
    """R3-18: the main HA reads a light's colour mode off the topics it was given, and takes an
    effect payload as the effect's name."""

    async def test_an_rgbw_light_keeps_its_white_channel(self):
        attrs = {"supported_color_modes": ["rgbw"], "color_mode": "rgbw", "brightness": 100,
                 "rgbw_color": [10, 20, 30, 40], "rgb_color": [10, 20, 30]}
        consumer = Consumer(self.hass, State("light.l", "on", attrs))
        self.assertQuiet(consumer, State("light.l", "on", attrs))
        from homeassistant.components.light import ColorMode

        self.assertEqual(consumer.entity.supported_color_modes, {ColorMode.RGBW})  # was {RGB}
        self.assertEqual(consumer.entity.rgbw_color, (10, 20, 30, 40))

    async def test_an_rgbww_light_keeps_both_whites(self):
        attrs = {"supported_color_modes": ["rgbww"], "color_mode": "rgbww", "brightness": 100,
                 "rgbww_color": [10, 20, 30, 40, 50], "rgb_color": [10, 20, 30]}
        consumer = Consumer(self.hass, State("light.l", "on", attrs))
        self.assertQuiet(consumer, State("light.l", "on", attrs))
        self.assertEqual(consumer.entity.rgbww_color, (10, 20, 30, 40, 50))

    async def test_hs_and_xy_still_travel_as_rgb(self):
        for mode in ("hs", "xy", "rgb"):
            with self.subTest(mode=mode):
                attrs = {"supported_color_modes": [mode], "brightness": 100, "rgb_color": [1, 2, 3]}
                consumer = Consumer(self.hass, State("light.l", "on", attrs))
                self.assertIn("rgb_state_topic", consumer.component)
                self.assertNotIn("rgbw_state_topic", consumer.component)
                self.assertQuiet(consumer, State("light.l", "on", attrs))
                self.assertEqual(consumer.entity.rgb_color, (1, 2, 3))

    async def test_no_effect_is_not_an_effect_called_none(self):
        attrs = {"supported_color_modes": ["brightness"], "brightness": 100, "effect_list": ["Rainbow"], "effect": "Rainbow"}
        consumer = Consumer(self.hass, State("light.l", "on", attrs))
        self.assertQuiet(consumer, State("light.l", "on", attrs))
        self.assertEqual(consumer.entity.effect, "Rainbow")
        for gone in ({**attrs, "effect": None}, {k: v for k, v in attrs.items() if k != "effect"}):
            with self.subTest(attrs=gone):
                self.assertQuiet(consumer, State("light.l", "on", gone))
                self.assertNotEqual(consumer.entity.effect, "None")  # was the word, shown as the effect

    async def test_an_effect_really_called_none_still_arrives(self):
        attrs = {"supported_color_modes": ["brightness"], "brightness": 100,
                 "effect_list": ["None", "Rainbow"], "effect": "None"}
        consumer = Consumer(self.hass, State("light.l", "on", attrs))
        self.assertQuiet(consumer, State("light.l", "on", attrs))
        self.assertEqual(consumer.entity.effect, "None")


class LightColourCommandsTest(_HassCase):
    SERVICES = ("light",)

    async def test_rgbw_and_rgbww_from_the_main_ha(self):
        for mode, colour, service_key in (("rgbw", (10, 20, 30, 40), "rgbw_color"),
                                          ("rgbww", (10, 20, 30, 40, 50), "rgbww_color")):
            with self.subTest(mode=mode):
                attrs = {"supported_color_modes": [mode], "color_mode": mode, "brightness": 255,
                         f"{mode}_color": list(colour)}
                consumer = Consumer(self.hass, State("light.l", "on", attrs))
                consumer.receive(State("light.l", "on", attrs))
                await consumer.entity.async_turn_on(**{service_key: colour})
                # the colour goes to its own topic, and the plain on command follows it
                self.assertIn(("light", "turn_on", {"entity_id": "light.l", service_key: list(colour)}),
                              await self.run_here(consumer))


class ClimateFieldsTest(_HassCase):
    """R3-18: target humidity, horizontal swing and the source's own power switch."""

    SERVICES = ("climate",)
    ATTRS = {"hvac_modes": ["off", "heat"], "current_temperature": 21, "temperature": 20,
             "humidity": 45, "current_humidity": 40, "min_humidity": 30, "max_humidity": 70,
             "swing_horizontal_modes": ["on", "off"], "swing_horizontal_mode": "on",
             "supported_features": 1 | 4 | 128 | 256 | 512}

    async def test_target_humidity_and_horizontal_swing_arrive(self):
        from homeassistant.components.climate import ClimateEntityFeature

        state = State("climate.c", "heat", self.ATTRS)
        consumer = Consumer(self.hass, state)
        self.assertQuiet(consumer, state)
        features = ClimateEntityFeature(int(consumer.entity.supported_features))
        self.assertIn(ClimateEntityFeature.TARGET_HUMIDITY, features)  # was absent
        self.assertIn(ClimateEntityFeature.SWING_HORIZONTAL_MODE, features)
        self.assertEqual(consumer.entity.target_humidity, 45)
        self.assertEqual(consumer.entity.swing_horizontal_mode, "on")
        self.assertEqual((consumer.entity.min_humidity, consumer.entity.max_humidity), (30, 70))

    async def test_commands_from_the_main_ha(self):
        consumer = Consumer(self.hass, State("climate.c", "heat", self.ATTRS))
        await consumer.entity.async_set_humidity(55)
        await consumer.entity.async_set_swing_horizontal_mode("off")
        await consumer.entity.async_turn_off()
        await consumer.entity.async_turn_on()
        self.assertEqual(await self.run_here(consumer), [
            ("climate", "set_humidity", {"entity_id": "climate.c", "humidity": 55}),
            ("climate", "set_swing_horizontal_mode", {"entity_id": "climate.c", "swing_horizontal_mode": "off"}),
            ("climate", "turn_off", {"entity_id": "climate.c"}),
            ("climate", "turn_on", {"entity_id": "climate.c"})])

    async def test_power_only_when_the_source_can_be_turned_on_and_off(self):
        for features, wanted in ((1 | 128 | 256, True), (1 | 128, False), (1 | 256, False), (1, False)):
            with self.subTest(features=features):
                comp = Consumer(self.hass, State("climate.c", "heat", {**self.ATTRS, "supported_features": features})).component
                self.assertEqual("power_command_topic" in comp, wanted)

    async def test_a_humidity_range_the_platform_refuses_is_replaced(self):
        """min >= max, a maximum above 100 or a negative minimum costs the WHOLE device its
        payload on the main HA; a source is free to report any of the three."""
        for low, high in ((70, 30), (0, 0), (-5, 50), (10, 120)):
            with self.subTest(range=(low, high)):
                attrs = {**self.ATTRS, "min_humidity": low, "max_humidity": high}
                consumer = Consumer(self.hass, State("climate.c", "heat", attrs))
                self.assertEqual((consumer.entity.min_humidity, consumer.entity.max_humidity), (0, 100))

    async def test_without_humidity_nothing_is_announced(self):
        attrs = {k: v for k, v in self.ATTRS.items() if k != "humidity"}
        comp = Consumer(self.hass, State("climate.c", "heat", {**attrs, "supported_features": 1})).component
        self.assertNotIn("target_humidity_command_topic", comp)
        self.assertNotIn("swing_horizontal_mode_command_topic",
                         Consumer(self.hass, State("climate.c", "heat", {"hvac_modes": ["off"]})).component)


class HumidifierActionTest(_HassCase):
    """R3-18: what the humidifier is doing right now had no topic at all."""

    ATTRS = {"humidity": 50, "current_humidity": 45, "min_humidity": 30, "max_humidity": 70,
             "available_modes": ["auto"], "mode": "auto", "supported_features": 1}

    async def test_the_action_arrives(self):
        from homeassistant.components.humidifier import HumidifierAction

        for action in ("humidifying", "drying", "idle", "off"):
            with self.subTest(action=action):
                state = State("humidifier.h", "on", {**self.ATTRS, "action": action})
                consumer = Consumer(self.hass, state)
                self.assertQuiet(consumer, state)
                self.assertEqual(consumer.entity.action, HumidifierAction(action))  # was None

    async def test_no_action_is_quiet(self):
        state = State("humidifier.h", "on", self.ATTRS)
        consumer = Consumer(self.hass, state)
        self.assertQuiet(consumer, state)
        self.assertIsNone(consumer.entity.action)
        self.assertQuiet(consumer, State("humidifier.h", "unavailable", {}))

    async def test_a_humidity_range_the_platform_refuses_is_replaced(self):
        for low, high in ((70, 30), (0, 0), (-5, 50), (10, 120)):
            with self.subTest(range=(low, high)):
                attrs = {**self.ATTRS, "min_humidity": low, "max_humidity": high}
                consumer = Consumer(self.hass, State("humidifier.h", "on", attrs))
                self.assertEqual((consumer.entity.min_humidity, consumer.entity.max_humidity), (0, 100))


class WaterHeaterFidelityTest(_HassCase):
    """R3-18 said water_heater was checked with a synthetic state only.  Everything the MQTT
    water heater can carry does arrive; what it has no option for (away mode, a target range)
    stays an attribute, and an unknown state clears the operation instead of showing a word."""

    ATTRS = {"operation_list": ["off", "eco", "performance"], "current_temperature": 50, "temperature": 55,
             "min_temp": 30, "max_temp": 70, "away_mode": "off", "target_temp_high": 60, "target_temp_low": 40,
             "supported_features": 1 | 2 | 4 | 8}

    async def test_everything_the_platform_has_an_option_for_arrives(self):
        state = State("water_heater.tank", "eco", self.ATTRS)
        consumer = Consumer(self.hass, state)
        self.assertQuiet(consumer, state)
        self.assertEqual(consumer.entity.current_operation, "eco")
        self.assertEqual(consumer.entity.operation_list, ["off", "eco", "performance"])
        self.assertEqual((consumer.entity.current_temperature, consumer.entity.target_temperature), (50, 55))
        self.assertEqual((consumer.entity.min_temp, consumer.entity.max_temp), (30, 70))

    async def test_what_it_has_no_option_for_stays_an_attribute(self):
        from homeassistant.components.mqtt.water_heater import DISCOVERY_SCHEMA

        doc = document(State("water_heater.tank", "eco", self.ATTRS))
        for key in ("away_mode", "target_temp_high", "target_temp_low"):
            self.assertIn(key, doc["attributes"])
        # the platform would drop the whole device for an option it does not know: prove it has none
        for key in ("away_mode_command_topic", "temperature_high_command_topic", "temperature_low_command_topic"):
            self.assertNotIn(key, DISCOVERY_SCHEMA({"state_topic": "t", "device": {"identifiers": ["i"], "name": "n"}}))

    async def test_an_unknown_operation_clears_instead_of_showing_a_word(self):
        consumer = Consumer(self.hass, State("water_heater.tank", "eco", self.ATTRS))
        self.assertQuiet(consumer, State("water_heater.tank", "eco", self.ATTRS))
        for st in ("unknown", "unavailable"):
            with self.subTest(state=st):
                self.assertQuiet(consumer, State("water_heater.tank", st, self.ATTRS))
                self.assertIsNone(consumer.entity.current_operation)


class AnnouncedFeaturesTest(_HassCase):
    """R3-19: alarm 63, lock OPEN, update INSTALL, lawn_mower 7 and valve STOP were announced
    whatever the source said, so the buttons appeared on the main HA and failed at the source."""

    CASES = {
        "alarm_control_panel.a": ("disarmed", {}),
        "lock.l": ("locked", {}),
        "update.u": ("off", {"installed_version": "1", "latest_version": "1"}),
        "lawn_mower.m": ("docked", {}),
        "valve.v": ("open", {}),
    }

    def features(self, entity_id, bits):
        st, attrs = self.CASES[entity_id]
        consumer = Consumer(self.hass, State(entity_id, st, {**attrs, "supported_features": bits}))
        return int(consumer.entity.supported_features)

    async def test_the_source_decides(self):
        # (entity, what the source says, what the main HA must end up with)
        for entity_id, source, wanted in (
            ("alarm_control_panel.a", 1 | 2, 1 | 2),          # was 63: every arm mode
            ("alarm_control_panel.a", 0, 0),
            ("alarm_control_panel.a", 1 | 2 | 4 | 8 | 16 | 32, 63),
            ("lock.l", 0, 0),                                  # was 1: OPEN
            ("lock.l", 1, 1),
            ("update.u", 0, 4),                                # was 5: INSTALL | PROGRESS
            ("update.u", 1, 5),
            ("lawn_mower.m", 1, 1),                            # was 7: start | pause | dock
            ("lawn_mower.m", 2 | 4, 2 | 4),
            ("valve.v", 1 | 2, 1 | 2),                         # was 11: open | close | stop
            ("valve.v", 1 | 2 | 8, 1 | 2 | 8),
            ("valve.v", 0, 0),
        ):
            with self.subTest(entity=entity_id, source=source):
                self.assertEqual(self.features(entity_id, source), wanted)

    async def test_a_source_that_reports_none_still_gets_everything(self):
        """A registry entry of an integration that never loaded has no features to report:
        the payload then is what it has always been."""
        for entity_id, wanted in (("alarm_control_panel.a", 63), ("lock.l", 1), ("update.u", 5),
                                  ("lawn_mower.m", 7), ("valve.v", 11)):
            with self.subTest(entity=entity_id):
                st, attrs = self.CASES[entity_id]
                consumer = Consumer(self.hass, State(entity_id, st, attrs))
                self.assertEqual(int(consumer.entity.supported_features), wanted)

    async def test_a_valve_that_reports_a_position_it_cannot_set(self):
        """reports_position is what announces SET_POSITION there: a valve that only tells its
        position keeps the open/close it does have instead of a slider that fails here."""
        attrs = {"current_position": 30, "supported_features": 1 | 2}
        consumer = Consumer(self.hass, State("valve.v", "open", attrs))
        self.assertNotIn("reports_position", consumer.component)
        self.assertEqual(int(consumer.entity.supported_features), 1 | 2)
        settable = Consumer(self.hass, State("valve.v", "open", {"current_position": 30, "supported_features": 4 | 8}))
        self.assertTrue(settable.component["reports_position"])
        # open and close come back from the main HA's own defaults: a valve that reports a
        # position may not carry those payloads at all, and there they mean position 100 and 0
        self.assertEqual(int(settable.entity.supported_features), 1 | 2 | 4 | 8)


class AnnouncedFeatureCommandsTest(_HassCase):
    """The commands a source that announces the feature still sends, unchanged."""

    SERVICES = ("lock", "lawn_mower", "valve")

    async def test_lock_open_and_lawn_mower_and_valve_stop(self):
        lock = Consumer(self.hass, State("lock.l", "locked", {"supported_features": 1}))
        await lock.entity.async_open()
        self.assertEqual(await self.run_here(lock), [("lock", "open", {"entity_id": "lock.l"})])
        mower = Consumer(self.hass, State("lawn_mower.m", "docked", {"supported_features": 1 | 2 | 4}))
        for action in ("async_start_mowing", "async_pause", "async_dock"):
            await getattr(mower.entity, action)()
        self.assertEqual([svc for _, svc, _ in await self.run_here(mower)], ["start_mowing", "pause", "dock"])
        valve = Consumer(self.hass, State("valve.v", "open", {"supported_features": 1 | 2 | 8}))
        for action in ("async_open_valve", "async_close_valve", "async_stop_valve"):
            await getattr(valve.entity, action)()
        self.assertEqual([svc for _, svc, _ in await self.run_here(valve)],
                         ["open_valve", "close_valve", "stop_valve"])


class _Registries:
    """A device and its entities in the real registries of a Home Assistant."""

    def __init__(self, hass):
        self.hass = hass
        entry = config_entries.ConfigEntry(
            version=1, minor_version=1, domain="hri_r4plat", title="t", data={}, source="user",
            options={}, unique_id=None, discovery_keys={}, subentries_data=[],
        )
        hass.config_entries._entries[entry.entry_id] = entry  # noqa: SLF001 - no integration to set up
        self.entry_id = entry.entry_id

    def device(self, key, name):
        return dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.entry_id, identifiers={("hri_r4plat", key)}, name=name)

    def entity(self, entity_id, device, name, has_entity_name=True):
        domain, object_id = entity_id.split(".", 1)
        return er.async_get(self.hass).async_get_or_create(
            domain, "hri_r4plat", object_id, device_id=device.id, has_entity_name=has_entity_name,
            original_name=name, suggested_object_id=object_id)


class EntityNameTest(_HassCase):
    """R3-20: every MQTT entity on the main Home Assistant has has_entity_name, so what it shows
    is "<device name> <component name>" - a component carrying the device's name read "Hall Lamp
    Hall Lamp" there.  What the source shows is what the main instance has to show."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        from homeassistant import bootstrap

        self.hass.config_entries = config_entries.ConfigEntries(self.hass, {})
        await bootstrap.async_load_base_functionality(self.hass)  # the real device and entity registries
        self.reg = _Registries(self.hass)

    def component_for(self, entry, state):
        doc_topic = f"{BASE}/demo/{state.entity_id.replace('.', '/', 1)}"
        return disc.build_component(self.hass, state, doc_topic, f"{BASE}/cmd", f"{BASE}_")

    def shown_on_the_main_ha(self, device_name, component_name):
        """The friendly name the main Home Assistant composes for this component."""
        device = self.reg.device(f"main_{device_name}_{component_name}", device_name)
        entry = er.async_get(self.hass).async_get_or_create(
            "light", "mqtt", f"main_{device_name}_{component_name}", device_id=device.id,
            has_entity_name=True, original_name=component_name)
        return er.async_get_full_entity_name(self.hass, entry)

    async def test_the_entity_that_is_its_device_has_no_name_of_its_own(self):
        device = self.reg.device("lamp", "Hall Lamp")
        entry = self.reg.entity("light.hall_lamp", device, None)
        state = State(entry.entity_id, "on", {"friendly_name": "Hall Lamp"})
        comp = self.component_for(entry, state)
        self.assertIsNone(comp["name"])  # was "Hall Lamp", shown as "Hall Lamp Hall Lamp"
        self.assertEqual(self.shown_on_the_main_ha("Hall Lamp", comp["name"]), "Hall Lamp")
        self.assertEqual(self.shown_on_the_main_ha("Hall Lamp", "Hall Lamp"), "Hall Lamp Hall Lamp")

    async def test_a_named_entity_keeps_only_its_own_part(self):
        device = self.reg.device("lamp2", "Hall Lamp")
        entry = self.reg.entity("sensor.hall_lamp_power", device, "Power")
        state = State(entry.entity_id, "12", {"friendly_name": "Hall Lamp Power"})
        comp = self.component_for(entry, state)
        self.assertEqual(comp["name"], "Power")
        self.assertEqual(self.shown_on_the_main_ha("Hall Lamp", comp["name"]), "Hall Lamp Power")

    async def test_a_legacy_entity_loses_the_device_prefix(self):
        """An integration that predates has_entity_name carries the composed name in the
        registry; the main HA would compose it a second time."""
        device = self.reg.device("lamp3", "Hall Lamp")
        entry = self.reg.entity("sensor.hall_lamp_power", device, "Hall Lamp Power", has_entity_name=False)
        state = State(entry.entity_id, "12", {"friendly_name": "Hall Lamp Power"})
        comp = self.component_for(entry, state)
        self.assertEqual(comp["name"], "Power")
        self.assertEqual(self.shown_on_the_main_ha("Hall Lamp", comp["name"]), "Hall Lamp Power")

    async def test_a_name_that_only_starts_like_the_device_is_untouched(self):
        device = self.reg.device("lamp4", "Hall")
        entry = self.reg.entity("sensor.hallway_damp", device, "Hallway damp", has_entity_name=False)
        state = State(entry.entity_id, "12", {})
        self.assertEqual(self.component_for(entry, state)["name"], "Hallway damp")

    async def test_an_entity_without_a_device_keeps_its_whole_name(self):
        """It lands in the per-integration bucket, whose name is nobody's own: nothing to strip."""
        state = State("light.orphan", "on", {"friendly_name": "Orphan Lamp"})
        registry = mock.Mock()
        registry.async_get.return_value = None
        with mock.patch.object(disc.er, "async_get", return_value=registry):
            comp = disc.build_component(self.hass, state, f"{BASE}/demo/light/orphan", f"{BASE}/cmd", f"{BASE}_")
        self.assertEqual(comp["name"], "Orphan Lamp")

    async def test_the_mirror_of_a_device_entity_keeps_the_domain_suffix(self):
        device = self.reg.device("cam", "Front Door")
        entry = self.reg.entity("camera.front_door", device, None)
        comp = self.component_for(entry, State(entry.entity_id, "idle", {"friendly_name": "Front Door"}))
        self.assertEqual(comp["platform"], "sensor")
        self.assertEqual(comp["name"], "(camera)")  # not "None (camera)"
        self.assertEqual(self.shown_on_the_main_ha("Front Door", comp["name"]), "Front Door (camera)")


class NameHelperTest(unittest.TestCase):
    """The prefix rule, the way Home Assistant's own one works."""

    def test_prefix_stripping(self):
        cases = {("Hall Lamp Power", "Hall Lamp"): "Power",
                 ("hall lamp power", "Hall Lamp"): "Power",
                 ("Hall Lamp - Power", "Hall Lamp"): "Power",
                 ("Hall Lamp: Power", "Hall Lamp"): "Power",
                 ("Hall Lamp SW1", "Hall Lamp"): "SW1",
                 ("Hallway damp", "Hall"): "Hallway damp",
                 ("Kitchen Power", "Hall Lamp"): "Kitchen Power",
                 ("Hall Lamp", "Hall Lamp"): None}
        for (name, device), wanted in cases.items():
            with self.subTest(name=name, device=device):
                state = State("light.x", "on", {"friendly_name": name})
                self.assertEqual(disc._entity_name(None, state, device), wanted)  # noqa: SLF001

    def test_without_a_device_or_a_name(self):
        self.assertEqual(disc._entity_name(None, State("light.x", "on", {}), None), "x")  # noqa: SLF001
        self.assertEqual(disc._entity_name(None, State("light.x", "on", {}), "Hall"), "x")  # noqa: SLF001
