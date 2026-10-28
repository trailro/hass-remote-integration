"""Climate setpoints on a main Home Assistant 2026.10.0 or later.

Its MQTT climate, when the modes hold heat_cool and also heat or cool, keeps one setpoint kind at a time: a
non-None single setpoint parks (clears) the range and a non-None low/high parks the single one.  The entity
document carries all three, so a thermostat in `heat` at 23.5 with a remembered 18/24 range showed
`temperature: null` there (and the range went null on 2026.11).  The templates now render exactly one non-None
kind per message, chosen the way the main HA's own mode swap does (only heat_cool is the range mode).

What the main HA's own MQTT climate makes of these templates is in tests/test_e2e_disc.py: ClimateSetpointsTest.
"""

import json
import tempfile
import unittest
from unittest import mock

from homeassistant import core, loader
from homeassistant.core import State

from custom_components.integration_manager import discovery as disc

BASE = "hass_demo"
DUAL = {"supported_features": 1 | 2, "hvac_modes": ["off", "heat", "cool", "heat_cool"]}
ALL = {"temperature": 23.5, "target_temp_low": 18, "target_temp_high": 24}
KEYS = ("temperature_state_template", "temperature_low_state_template", "temperature_high_state_template")


def component(state: State) -> dict:
    registry = mock.Mock()
    registry.async_get.return_value = None
    with mock.patch.object(disc.er, "async_get", return_value=registry):
        return disc.build_component(mock.Mock(), state, f"{BASE}/demo/x", f"{BASE}/cmd", f"{BASE}_")


class SetpointTemplatesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hass = core.HomeAssistant(tempfile.mkdtemp())
        loader.async_setup(self.hass)

    async def asyncTearDown(self):
        await self.hass.async_stop(force=True)

    def render(self, comp, state, attrs) -> tuple[str, str, str]:
        """(single, low, high) rendered the way MQTT renders a state template, on the published document."""
        from homeassistant.helpers.template import Template

        payload = json.dumps({"state": state, "attributes": attrs})
        return tuple(Template(comp[k], self.hass).async_render_with_possible_json_value(payload) for k in KEYS)

    async def test_heat_shows_the_single_setpoint_only(self):
        for state in ("heat", "cool", "off"):
            with self.subTest(state=state):
                comp = component(State("climate.t", state, {**DUAL, **ALL}))
                self.assertEqual(self.render(comp, state, {**DUAL, **ALL}), ("23.5", "None", "None"))

    async def test_heat_cool_shows_the_range_only(self):
        comp = component(State("climate.t", "heat_cool", {**DUAL, **ALL}))
        self.assertEqual(self.render(comp, "heat_cool", {**DUAL, **ALL}), ("None", "18", "24"))
        half = {**DUAL, "temperature": 23.5, "target_temp_low": None, "target_temp_high": 24}
        self.assertEqual(self.render(comp, "heat_cool", half), ("None", "None", "24"))

    async def test_a_source_with_one_kind_still_shows_it(self):
        comp = component(State("climate.t", "heat_cool", {**DUAL, **ALL}))
        only_single = {**DUAL, "temperature": 21, "target_temp_low": None, "target_temp_high": None}
        self.assertEqual(self.render(comp, "heat_cool", only_single), ("21", "None", "None"))
        self.assertEqual(self.render(comp, "heat_cool", {**DUAL, "temperature": 21}), ("21", "None", "None"))
        only_range = {**DUAL, "temperature": None, "target_temp_low": 18, "target_temp_high": 24}
        self.assertEqual(self.render(comp, "heat", only_range), ("None", "18", "24"))

    async def test_unavailable_source_renders_none_everywhere(self):
        comp = component(State("climate.t", "heat", {**DUAL, **ALL}))
        for state in ("unavailable", "unknown"):
            with self.subTest(state=state):
                self.assertEqual(self.render(comp, state, {}), ("None", "None", "None"))

    async def test_cool_alone_is_enough_for_the_dual_rule(self):
        attrs = {**ALL, "supported_features": 1 | 2, "hvac_modes": ["off", "cool", "heat_cool"]}
        comp = component(State("climate.t", "cool", attrs))
        self.assertEqual(self.render(comp, "cool", attrs), ("23.5", "None", "None"))

    def test_other_climates_keep_their_templates(self):
        """No parking without heat_cool next to heat or cool: those templates are what they always were."""
        for modes in (["off", "heat"], ["off", "heat_cool"], ["off", "auto", "heat_cool", "dry"], ["off", "cool", "auto"]):
            with self.subTest(modes=modes):
                comp = component(State("climate.t", "heat_cool", {**ALL, "supported_features": 1 | 2, "hvac_modes": modes}))
                self.assertEqual(comp["temperature_state_template"], "{{ value_json.attributes.get('temperature', None) }}")
                self.assertEqual(comp["temperature_low_state_template"],
                                 "{{ value_json.attributes.get('target_temp_low', None) }}")
                self.assertEqual(comp["temperature_high_state_template"],
                                 "{{ value_json.attributes.get('target_temp_high', None) }}")
        single = component(State("climate.t", "heat", {"supported_features": 1, "hvac_modes": ["off", "heat", "heat_cool"],
                                                       "temperature": 20}))
        self.assertNotIn("temperature_low_state_template", single)  # no range declared: no range topics invented

    def test_water_heater_is_left_alone(self):
        """MQTT water_heater has one setpoint and parks nothing."""
        comp = component(State("water_heater.w", "eco", {"operation_list": ["off", "eco"], "temperature": 55}))
        self.assertEqual(comp["temperature_state_template"], "{{ value_json.attributes.get('temperature', None) }}")


if __name__ == "__main__":
    unittest.main()
