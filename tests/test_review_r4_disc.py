"""Fourth external review, discovery findings.

R3-04  a GPS device_tracker was always `not_home` on the main Home Assistant: discovery published the
       state this container computes, and this container is headless - its `zone.home` sits at 0,0.
R3-05  a code-protected lock could not be operated from there: no `code_format`, no `command_template`,
       and the code the user typed never left the main instance.
R3-06  discovery was shaped by attributes that are only in the state sometimes, so an entity that was
       `unavailable` (or simply off, or in the wrong mode) when its config went out was announced
       without the controls it has - until the hourly full republish, if ever.

The end-to-end half of these (what the main HA's own MQTT platforms make of the payloads) is in
tests/test_e2e_disc.py: DeviceTrackerZoneTest and LockCodeTest.
"""

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from homeassistant import core, loader
from homeassistant.core import State

from custom_components.integration_manager import discovery as disc
from custom_components.integration_manager import mqtt_publisher as mp

BASE = "hass_demo"


def component(state: State, entry=None) -> dict:
    registry = mock.Mock()
    registry.async_get.return_value = entry
    hass = mock.Mock()
    with mock.patch.object(disc.er, "async_get", return_value=registry):
        return disc.build_component(hass, state, f"{BASE}/demo/x", f"{BASE}/cmd", f"{BASE}_")


def entry(entity_id, capabilities=None, supported_features=0):
    return SimpleNamespace(entity_id=entity_id, capabilities=capabilities, supported_features=supported_features,
                           unit_of_measurement=None, disabled=False, name=None, original_name="X", icon=None,
                           original_icon=None, entity_category=None, device_class=None, original_device_class=None)


# ----- R3-04 -------------------------------------------------------------


class DeviceTrackerStateTest(unittest.IsolatedAsyncioTestCase):
    """What the state topic renders decides everything: MQTT device_tracker turns it into a location
    name, and a tracker with a location name never looks at a zone."""

    async def asyncSetUp(self):
        self.hass = core.HomeAssistant(tempfile.mkdtemp())
        loader.async_setup(self.hass)

    async def asyncTearDown(self):
        await self.hass.async_stop(force=True)

    def render(self, state, attrs=None):
        """The value template rendered the way MQTT renders it, on the document this container publishes."""
        from homeassistant.helpers.template import Template

        comp = component(State("device_tracker.t", state, attrs or {}))
        payload = json.dumps({"state": state, "attributes": attrs or {}})
        return Template(comp["value_template"], self.hass).async_render_with_possible_json_value(payload)

    async def test_coordinates_render_the_reset_payload(self):
        """The main Home Assistant places the device in ITS zones, which are the ones that matter."""
        comp = component(State("device_tracker.t", "not_home", {"latitude": 44.43, "longitude": 26.1}))
        self.assertEqual(comp["payload_reset"], "None")
        for state in ("home", "not_home", "Work", "unknown"):
            with self.subTest(state=state):
                self.assertEqual(self.render(state, {"latitude": 44.43, "longitude": 26.1, "gps_accuracy": 10}), "None")

    async def test_without_coordinates_home_and_not_home_are_the_only_truth(self):
        """A router or bluetooth tracker has no coordinates: its own answer goes out as it stands."""
        self.assertEqual(self.render("home", {"source_type": "router"}), "home")
        self.assertEqual(self.render("not_home", {"source_type": "router"}), "not_home")

    async def test_nothing_but_the_three_payloads_is_ever_rendered(self):
        """MQTT device_tracker takes the RAW message for a payload that is none of its three, so anything
        else here made the whole entity document the location name."""
        comp = component(State("device_tracker.t", "home", {}))
        allowed = {comp["payload_home"], comp["payload_not_home"], comp["payload_reset"]}
        self.assertEqual(allowed, {"home", "not_home", "None"})
        for state, attrs in (("Work", {}), ("Office", {"source_type": "gps"}), ("unavailable", {}), ("unknown", {}),
                             ("home", {"latitude": None, "longitude": None}),
                             ("not_home", {"latitude": "44.43", "longitude": "26.1"}),  # not numbers: no placing
                             ("home", {"latitude": 44.43}), ("Work", {"longitude": 26.1})):
            with self.subTest(state=state, attrs=attrs):
                self.assertIn(self.render(state, attrs), allowed)

    async def test_a_partial_fix_of_coordinates_is_not_a_placement(self):
        """One coordinate is not a position: MQTT device_tracker refuses the pair and resets the location,
        so the source's own answer has to stay the state."""
        self.assertEqual(self.render("home", {"latitude": 44.43}), "home")
        self.assertEqual(self.render("not_home", {"longitude": 26.1}), "not_home")

    async def test_the_source_type_still_travels(self):
        self.assertEqual(component(State("device_tracker.t", "home", {"source_type": "router"}))["source_type"], "router")
        self.assertNotIn("source_type", component(State("device_tracker.t", "home", {})))


# ----- R3-05 -------------------------------------------------------------


class LockCodeTest(unittest.TestCase):
    def test_the_component_carries_the_code_format_and_a_command_template(self):
        comp = component(State("lock.front", "locked", {"code_format": r"^\d{4}$"}))
        self.assertEqual(comp["code_format"], r"^\d{4}$")
        self.assertIn("{{ value }}", comp["command_template"])
        self.assertIn("code", comp["command_template"])

    def test_a_code_format_the_main_ha_cannot_compile_is_left_out(self):
        for bad in ("[unclosed", "(", "*", "", None, 4, {"number": True}):
            with self.subTest(code_format=bad):
                self.assertNotIn("code_format", component(State("lock.front", "locked", {"code_format": bad})))

    def test_the_code_reaches_the_service_call(self):
        self.assertEqual(disc.command_to_service("lock", "front", "command", '{"action": "UNLOCK", "code": "1234"}'),
                         ("lock", "unlock", {"entity_id": "lock.front", "code": "1234"}))
        self.assertEqual(disc.command_to_service("lock", "front", "command", '{"action": "LOCK", "code": 99}'),
                         ("lock", "lock", {"entity_id": "lock.front", "code": "99"}))
        self.assertEqual(disc.command_to_service("lock", "front", "command", '{"action": "OPEN", "code": null}'),
                         ("lock", "open", {"entity_id": "lock.front"}))
        self.assertEqual(disc.command_to_service("lock", "front", "command", '{"action": "LOCK", "code": ""}'),
                         ("lock", "lock", {"entity_id": "lock.front"}))

    def test_a_bare_action_still_works(self):
        """A script publishing by hand, or a retained command from a config without the template."""
        self.assertEqual(disc.command_to_service("lock", "front", "command", "UNLOCK"),
                         ("lock", "unlock", {"entity_id": "lock.front"}))
        self.assertEqual(disc.command_to_service("lock", "front", "command", " lock "),
                         ("lock", "lock", {"entity_id": "lock.front"}))

    def test_an_unknown_action_is_refused_without_quoting_the_payload(self):
        for payload in ('{"action": "BURN", "code": "1234"}', "BURN", "{", '{"code": "1234"}', "[]"):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError) as caught:
                    disc.command_to_service("lock", "front", "command", payload)
                self.assertNotIn("1234", str(caught.exception))

    def test_the_alarm_panel_keeps_its_shape(self):
        """Both platforms now parse the action and the code through one helper."""
        self.assertEqual(disc.command_to_service("alarm_control_panel", "a", "command",
                                                 '{"action": "DISARM", "code": "4321"}'),
                         ("alarm_control_panel", "alarm_disarm", {"entity_id": "alarm_control_panel.a", "code": "4321"}))
        self.assertEqual(disc.command_to_service("alarm_control_panel", "a", "command", "ARM_AWAY"),
                         ("alarm_control_panel", "alarm_arm_away", {"entity_id": "alarm_control_panel.a"}))


# ----- R3-06 -------------------------------------------------------------


class ShapeFromFeaturesTest(unittest.TestCase):
    """The capability values are in the state only while the entity has them: an entity that was
    `unavailable` when its config went out lost every control announced on the strength of a value."""

    def test_a_cover_keeps_its_position_and_tilt(self):
        live = component(State("cover.c", "open", {"supported_features": 255, "current_position": 50,
                                                   "current_tilt_position": 20}))
        for attrs in ({"supported_features": 255}, {"supported_features": 255, "current_position": None,
                                                    "current_tilt_position": None}):
            with self.subTest(attrs=attrs):
                comp = component(State("cover.c", "unavailable", attrs))
                for key in ("position_topic", "set_position_topic", "tilt_status_topic", "tilt_command_topic"):
                    self.assertIn(key, comp, key)
                    self.assertEqual(comp[key], live[key])

    def test_a_cover_that_declares_neither_gets_neither(self):
        comp = component(State("cover.c", "open", {"supported_features": 1 | 2 | 8}))
        for key in ("position_topic", "set_position_topic", "tilt_status_topic", "tilt_command_topic"):
            self.assertNotIn(key, comp, key)

    def test_a_cover_without_declared_features_still_follows_its_values(self):
        self.assertIn("position_topic", component(State("cover.c", "open", {"current_position": 10})))
        self.assertNotIn("position_topic", component(State("cover.c", "open", {})))

    def test_a_fan_keeps_speed_oscillation_and_direction(self):
        comp = component(State("fan.f", "unavailable", {"supported_features": 1 | 2 | 4}))
        for key in ("percentage_command_topic", "oscillation_command_topic", "direction_command_topic"):
            self.assertIn(key, comp, key)
        off = component(State("fan.f", "off", {"supported_features": 1 | 2 | 4}))
        self.assertEqual(off["percentage_command_topic"], comp["percentage_command_topic"])

    def test_a_fan_that_declares_nothing_gets_nothing(self):
        comp = component(State("fan.f", "on", {"supported_features": 16 | 32}))
        for key in ("percentage_command_topic", "oscillation_command_topic", "direction_command_topic"):
            self.assertNotIn(key, comp, key)

    def test_a_valve_keeps_reporting_its_position(self):
        comp = component(State("valve.v", "unavailable", {"supported_features": 1 | 2 | 4}))
        self.assertTrue(comp["reports_position"])
        self.assertEqual(comp["command_topic"], f"{BASE}/cmd/valve/v/position")
        plain = component(State("valve.v", "unavailable", {"supported_features": 1 | 2 | 8}))
        self.assertNotIn("reports_position", plain)
        self.assertEqual(plain["command_topic"], f"{BASE}/cmd/valve/v/command")

    def test_a_thermostat_gets_its_range_whatever_mode_it_is_in(self):
        """The two values are only in the state while the thermostat is in a range mode, so one published
        in `heat` never got the high/low topics - and could not be put in `heat_cool` from there at all."""
        attrs = {"supported_features": 1 | 2, "hvac_modes": ["off", "heat", "heat_cool"]}
        for state in ("heat", "off", "unavailable", "heat_cool"):
            with self.subTest(state=state):
                comp = component(State("climate.t", state, attrs))
                for key in ("temperature_high_command_topic", "temperature_low_command_topic",
                            "temperature_high_state_topic", "temperature_low_state_topic"):
                    self.assertIn(key, comp, key)

    def test_a_single_setpoint_thermostat_gets_no_range(self):
        comp = component(State("climate.t", "heat", {"supported_features": 1, "hvac_modes": ["off", "heat"]}))
        self.assertNotIn("temperature_high_command_topic", comp)

    def test_declares_needs_a_declared_bit(self):
        self.assertTrue(disc._declares({"supported_features": 5}, 4))
        self.assertFalse(disc._declares({"supported_features": 5}, 2))
        for features in (None, "4", 4.0, True):  # unknown features invent nothing
            with self.subTest(features=features):
                self.assertFalse(disc._declares({"supported_features": features}, 4))
        self.assertFalse(disc._declares({}, 4))


class RegistryCapabilitiesTest(unittest.TestCase):
    """An `unavailable` entity has no attributes at all - not even the capability ones.  The registry
    keeps them, and they do not depend on the moment."""

    def test_a_fan_keeps_its_preset_modes(self):
        fan = entry("fan.f", capabilities={"preset_modes": ["eco", "boost"]}, supported_features=1 | 8)
        comp = component(State("fan.f", "unavailable", {}), entry=fan)
        self.assertEqual(comp["preset_modes"], ["eco", "boost"])
        self.assertIn("percentage_command_topic", comp)

    def test_a_climate_keeps_its_modes_and_limits(self):
        thermostat = entry("climate.t", capabilities={"hvac_modes": ["off", "heat", "heat_cool"], "min_temp": 7,
                                                      "max_temp": 30, "target_temp_step": 0.5},
                           supported_features=1 | 2)
        comp = component(State("climate.t", "unavailable", {}), entry=thermostat)
        self.assertEqual(comp["modes"], ["off", "heat", "heat_cool"])
        self.assertEqual((comp["min_temp"], comp["max_temp"]), (7, 30))
        self.assertIn("temperature_high_command_topic", comp)

    def test_a_select_keeps_its_options(self):
        sel = entry("select.s", capabilities={"options": ["a", "b"]})
        self.assertEqual(component(State("select.s", "unavailable", {}), entry=sel)["options"], ["a", "b"])

    def test_the_state_wins_over_the_registry(self):
        """The registry is a fallback, never a correction: what the entity says now is what goes out."""
        sel = entry("select.s", capabilities={"options": ["a", "b"]})
        comp = component(State("select.s", "c", {"options": ["c", "d"]}), entry=sel)
        self.assertEqual(comp["options"], ["c", "d"])
        cover = entry("cover.c", supported_features=255)
        self.assertNotIn("position_topic", component(State("cover.c", "open", {"supported_features": 3}), entry=cover))


# ----- R3-06, the publisher half -----------------------------------------


class Loop:
    def __init__(self):
        self.later = []

    def call_later(self, delay, cb):
        handle = SimpleNamespace(cancel=lambda: None)
        self.later.append((delay, cb, handle))
        return handle


def _publisher(**config):
    pub = object.__new__(mp.MqttPublisher)
    pub.config = mp.MqttConfig(**{"enabled": True, "discovery_enabled": True, **config})
    pub._connected, pub._moving = True, False
    pub._live_base, pub._live_prefix, pub._key_provider = BASE, None, lambda: BASE
    pub._registry_timer = None
    pub.hass = mock.Mock()
    pub.hass.loop = Loop()
    pub.rules = mock.Mock()
    pub.rules.for_entity.return_value = {}
    pub.rules.apply_component.side_effect = lambda comp, rule: comp
    return pub


class ReshapeRefreshTest(unittest.TestCase):
    """Only the registry and the device registry refreshed discovery; a state change never did, so an
    entity announced while it was unavailable stayed announced that way until the full republish."""

    ATTRS = {"supported_features": 255, "current_position": 50, "current_tilt_position": 20}

    def setUp(self):
        self.pub = _publisher()
        registry = mock.Mock()
        registry.async_get.return_value = None
        self.registry = mock.patch.object(disc.er, "async_get", return_value=registry)
        self.registry.start()
        self.addCleanup(self.registry.stop)
        self.platform = mock.patch.object(mp, "platform_of", return_value="demo")
        self.platform.start()
        self.addCleanup(self.platform.stop)
        announced = self.component(State("cover.c", "unavailable", {}))
        self.pub._discovery_map = {f"{BASE}_dev1": {"cover.c": announced}}

    def component(self, state):
        return disc.build_component(self.pub.hass, state, self.pub._topic_for(state.entity_id, "demo"),
                                    self.pub._cmd_base(), self.pub.prefix)

    def armed(self):
        return [delay for delay, _cb, _h in self.pub.hass.loop.later]

    def test_a_reshaped_component_arms_the_refresh(self):
        self.pub._refresh_discovery_if_reshaped(State("cover.c", "open", self.ATTRS))
        self.assertEqual(self.armed(), [3])  # the same debounce a registry change uses

    def test_an_unchanged_component_arms_nothing(self):
        self.pub._discovery_map[f"{BASE}_dev1"]["cover.c"] = self.component(State("cover.c", "open", self.ATTRS))
        self.pub._refresh_discovery_if_reshaped(State("cover.c", "closed", {**self.ATTRS, "current_position": 0}))
        self.assertEqual(self.armed(), [])

    def test_an_entity_this_process_never_announced_is_left_alone(self):
        self.pub._refresh_discovery_if_reshaped(State("cover.other", "open", self.ATTRS))
        self.assertEqual(self.armed(), [])

    def test_nothing_happens_while_discovery_is_off_or_the_broker_is_away(self):
        for name, kwargs in (("discovery off", {"discovery_enabled": False}), ("disconnected", {"_connected": False}),
                             ("moving", {"_moving": True})):
            with self.subTest(name):
                pub = _publisher(**{k: v for k, v in kwargs.items() if not k.startswith("_")})
                for k, v in kwargs.items():
                    if k.startswith("_"):
                        setattr(pub, k, v)
                pub._discovery_map = dict(self.pub._discovery_map)
                pub._refresh_discovery_if_reshaped(State("cover.c", "open", self.ATTRS))
                self.assertEqual([d for d, _c, _h in pub.hass.loop.later], [])

    def test_an_excluded_entity_is_left_alone(self):
        self.pub.config = mp.MqttConfig(enabled=True, discovery_enabled=True, exclude_integrations=["demo"])
        self.pub._refresh_discovery_if_reshaped(State("cover.c", "open", self.ATTRS))
        self.assertEqual(self.armed(), [])
        self.pub.config = mp.MqttConfig(enabled=True, discovery_enabled=True)
        self.pub.rules.for_entity.return_value = {"exclude": True}
        self.pub._refresh_discovery_if_reshaped(State("cover.c", "open", self.ATTRS))
        self.assertEqual(self.armed(), [])

    def test_a_component_that_cannot_be_built_is_not_an_error(self):
        with mock.patch.object(disc, "build_component", side_effect=ValueError("bad attribute")):
            self.pub._refresh_discovery_if_reshaped(State("cover.c", "open", self.ATTRS))
        self.assertEqual(self.armed(), [])

    def test_a_state_event_reaches_it(self):
        self.pub._publish_state = mock.Mock()
        self.pub._last_event = {}
        new = State("cover.c", "open", self.ATTRS)
        with mock.patch.object(mp.MqttPublisher, "_refresh_discovery_if_reshaped") as reshaped:
            self.pub._on_state(SimpleNamespace(data={"new_state": new, "old_state": None}))
        reshaped.assert_called_once_with(new)


if __name__ == "__main__":
    unittest.main()
