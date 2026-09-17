"""End-to-end campaign on 0.17.0, publisher side: an MQTT rule's device_class was not checked.  With a unit that does not
fit (W with temperature) the main Home Assistant refused the whole device config, so the icon and category of the same
rule (and every other change on that device) never arrived, while the API answered ok."""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from homeassistant.core import State
from homeassistant.helpers import entity_registry as er

from custom_components.integration_manager import mqtt_publisher as mp
from custom_components.integration_manager import mqtt_rules
from custom_components.integration_manager.mqtt_rules import MqttRules

BASE = "hass_demo"


def _entry(entity_id, unit=None, device_class=None):
    return SimpleNamespace(entity_id=entity_id, domain=entity_id.split(".")[0], platform="demo", device_id=None,
                           disabled_by=None, disabled=False, capabilities=None, unit_of_measurement=unit, name=None,
                           original_name=entity_id, icon=None, original_icon=None, entity_category=None, device_class=None,
                           original_device_class=device_class, unique_id=entity_id, hidden=False, area_id=None, labels=set(),
                           config_entry_id="e1", translation_key=None, options={})


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        entries = [_entry("sensor.power", "W", "power"), _entry("sensor.temp", "°C", "temperature"), _entry("sensor.mode", None, "enum"), _entry("sensor.meter", "kWh"),
                   _entry("light.lamp"), _entry("binary_sensor.door")]
        self.registry = SimpleNamespace(entities={e.entity_id: e for e in entries})
        self.registry.async_get = self.registry.entities.get
        self.states = {
            "sensor.power": State("sensor.power", "5", {"unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"}),
            "sensor.temp": State("sensor.temp", "21", {"unit_of_measurement": "°C", "device_class": "temperature"}),
            "sensor.meter": State("sensor.meter", "12", {"unit_of_measurement": "kWh"}),
            "sensor.mode": State("sensor.mode", "eco", {"device_class": "enum", "options": ["eco", "boost"]}),
            "light.lamp": State("light.lamp", "on", {"supported_color_modes": ["onoff"], "color_mode": "onoff"}),
            "binary_sensor.door": State("binary_sensor.door", "off"),
        }
        pub = mp.MqttPublisher.__new__(mp.MqttPublisher)
        pub.config = mp.MqttConfig(enabled=True, discovery_enabled=True)
        pub._connected, pub._moving = True, False
        pub._live_base, pub._live_prefix, pub._key_provider = BASE, None, lambda: BASE
        pub._collision_warned, pub._default_id_warned = set(), set()
        pub.hass = mock.Mock()
        pub.hass.data = {}
        pub.hass.states.async_all.side_effect = lambda: list(self.states.values())
        pub.hass.states.get.side_effect = self.states.get
        pub.hass.config.components = {"demo"}
        pub.rules = MqttRules(os.path.join(self.tmp, "mqtt_rules.json"))
        pub.rules.components = pub._rule_components  # what __init__ wires
        self.pub = pub
        patch = mock.patch.object(er, "async_get", return_value=self.registry)
        patch.start()
        self.addCleanup(patch.stop)

    def announced(self, entity_id):
        groups, _ = self.pub._group_by_device()
        return next(comps[entity_id] for _block, comps in groups.values() if entity_id in comps)


class DeviceClassRuleTest(_Case):
    def test_a_unit_that_does_not_fit_is_refused_with_the_reason(self):
        with self.assertRaises(ValueError) as caught:
            self.pub.rules.replace_all({"sensor.power": {"device_class": "temperature", "icon": "mdi:radiator"}})
        self.assertIn("sensor.power", str(caught.exception))
        self.assertIn("W", str(caught.exception))
        self.assertEqual(self.pub.rules.rules, {})  # nothing of it stored
        with self.assertRaises(ValueError):
            self.pub.rules.set("sensor.power", device_class="temperature")
        self.assertEqual(self.pub.rules.rules, {})

    def test_a_glob_is_refused_when_one_entity_it_matches_does_not_fit(self):
        with self.assertRaises(ValueError) as caught:
            self.pub.rules.replace_all({"sensor.*": {"device_class": "temperature"}})
        self.assertIn("sensor.power", str(caught.exception))

    def test_a_class_that_fits_is_announced(self):
        self.pub.rules.replace_all({"sensor.meter": {"device_class": "energy", "icon": "mdi:flash"}})
        self.pub.rules.set("sensor.temp", device_class="temperature", entity_category="diagnostic")
        comp = self.announced("sensor.meter")
        self.assertEqual((comp["device_class"], comp["icon"]), ("energy", "mdi:flash"))
        self.assertEqual(self.announced("sensor.temp")["entity_category"], "diagnostic")

    def test_unknown_class_and_platforms_without_classes(self):
        for rules, needle in (({"sensor.power": {"device_class": "bogus"}}, "not a device class of Home Assistant"),
                              ({"light.lamp": {"device_class": "power"}}, "light on the main Home Assistant takes no device class"),
                              ({"binary_sensor.door": {"device_class": "temperature"}}, "not a device class of a binary_sensor"),
                              ({"sensor.mode": {"device_class": "temperature"}}, "keeps device class enum"),
                              ({"sensor.power": {"device_class": "enum"}}, "enum takes no unit")):
            with self.subTest(rules=rules):
                with self.assertRaises(ValueError) as caught:
                    self.pub.rules.replace_all(rules)
                self.assertIn(needle, str(caught.exception))
        self.pub.rules.replace_all({"binary_sensor.door": {"device_class": "door"}})  # the right platform's class

    def test_other_changes_to_a_rule_are_not_blocked_by_its_stored_class(self):
        self.pub.rules.rules = {"sensor.power": {"device_class": "temperature"}}  # stored before the check existed
        self.pub.rules.set("sensor.power", name="Meter")
        self.pub.rules.replace_all({"sensor.power": {"device_class": "temperature", "name": "Meter 2"}})

    def test_an_entity_a_glob_matches_later_gets_the_rest_of_the_rule(self):
        self.pub.rules.replace_all({"sensor.t*": {"device_class": "temperature", "icon": "mdi:thermometer"}})
        self.registry.entities["sensor.twatt"] = _entry("sensor.twatt", "W", "power")
        self.states["sensor.twatt"] = State("sensor.twatt", "3", {"unit_of_measurement": "W"})
        with self.assertLogs(mqtt_rules._LOGGER, "WARNING") as logs:
            comp = self.announced("sensor.twatt")
            self.announced("sensor.twatt")
        self.assertEqual(len(logs.output), 1)  # once
        self.assertEqual(comp["icon"], "mdi:thermometer")
        self.assertEqual(comp["device_class"], "power")  # its own class stays: the device config is still accepted
        self.assertEqual(self.announced("sensor.temp")["device_class"], "temperature")

    def test_a_stored_unknown_class_does_not_drop_the_rest_of_the_rule(self):
        path = os.path.join(self.tmp, "stored.json")
        with open(path, "w") as fh:
            json.dump({"rules": {"sensor.power": {"exclude": True, "device_class": "wattage"}}}, fh)
        with self.assertLogs(mqtt_rules._LOGGER, "ERROR"):
            rules = MqttRules(path)
        self.assertEqual(rules.rules, {"sensor.power": {"exclude": True}})


class ReadmeListsEveryFieldTest(unittest.TestCase):
    def test_every_rule_field_is_named(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "README.md"), encoding="utf-8") as fh:
            readme = fh.read()
        start = readme.index("## MQTT reference")
        section = readme[start:readme.index("\n## ", start)]
        for field in mqtt_rules.FIELDS:
            with self.subTest(field=field):
                self.assertIn(f"`{field}`", section)
