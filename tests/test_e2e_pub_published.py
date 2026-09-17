"""End-to-end campaign on 0.17.0, publisher side: what is published.

- zone.home of the container's own Home Assistant reached the main HA (sensor.zone_home on a device "zone (no
  device)"), and entities_total counted it and every excluded entity."""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import DATA_ENTITY_PLATFORM

from custom_components.integration_manager import mqtt_publisher as mp
from custom_components.integration_manager.mqtt_rules import MqttRules

BASE = "hass_demo"


def _entry(entity_id, platform="demo"):
    return SimpleNamespace(entity_id=entity_id, domain=entity_id.split(".")[0], platform=platform, device_id=None,
                           disabled_by=None, disabled=False, capabilities=None, unit_of_measurement=None, name=None,
                           original_name=entity_id, icon=None, original_icon=None, entity_category=None, device_class=None,
                           original_device_class=None, unique_id=entity_id, hidden=False, area_id=None, labels=set(),
                           config_entry_id="e1", translation_key=None, options={})


class Registry:
    def __init__(self, entries):
        self.entities = {e.entity_id: e for e in entries}

    def async_get(self, entity_id):
        return self.entities.get(entity_id)


class _Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        # zone.home has no registry entry: the zone component's own entity platform added it
        self.registry = Registry([_entry("sensor.power"), _entry("camera.front"), _entry("sensor.hidden"),
                                  _entry("sensor.disabled"), _entry("zone.office", "zone")])
        self.registry.entities["sensor.disabled"].disabled = True
        self.states = {
            "sensor.power": State("sensor.power", "5"),
            "sensor.hidden": State("sensor.hidden", "1"),
            "zone.home": State("zone.home", "0", {"latitude": 45.1, "longitude": 25.2, "radius": 100, "friendly_name": "Home"}),
            "camera.front": State("camera.front", "idle", {
                "access_token": "a" * 64, "entity_picture": "/api/camera_proxy/camera.front?token=" + "a" * 64,
                "friendly_name": "Front", "brand": "Imou"}),
        }
        pub = mp.MqttPublisher.__new__(mp.MqttPublisher)
        pub.config = mp.MqttConfig(enabled=True, discovery_enabled=True)
        pub._connected, pub._moving = True, False
        pub._live_base, pub._live_prefix, pub._key_provider = BASE, None, lambda: BASE
        pub.rules = MqttRules(os.path.join(tmp, "mqtt_rules.json"))
        pub.rules.set("sensor.hidden", exclude=True)
        pub._collision_warned, pub._default_id_warned = set(), set()
        pub.hass = mock.Mock()
        pub.hass.data = {DATA_ENTITY_PLATFORM: {"zone": [SimpleNamespace(platform_name="zone", entities={"zone.home": object()})]}}
        pub.hass.states.async_all.side_effect = lambda: list(self.states.values())
        pub.hass.states.get.side_effect = self.states.get
        pub.hass.config.components = {"demo", "zone", "camera", "sensor"}
        self.pub = pub
        patch = mock.patch.object(er, "async_get", return_value=self.registry)
        patch.start()
        self.addCleanup(patch.stop)


class ZoneOfTheContainerTest(_Case):
    def test_zone_entities_are_never_published(self):
        self.assertIsNone(self.pub.build_document(self.states["zone.home"]))
        groups, _counts = self.pub._group_by_device()
        announced = {eid for _block, comps in groups.values() for eid in comps}
        self.assertNotIn("zone.home", announced)
        self.assertNotIn("zone.office", announced)  # a registry entry of zone without a state neither
        self.assertIn("sensor.power", announced)
        self.assertNotIn(f"{self.pub.prefix}zone_nodevice", groups)

    def test_a_zone_published_by_an_older_version_goes_with_the_orphan_sweep(self):
        self.assertTrue(self.pub._excluded_now("zone.home"))
        self.assertFalse(self.pub._excluded_now("sensor.power"))

    def test_entities_total_counts_what_is_published(self):
        published = [eid for eid, state in self.states.items() if self.pub.build_document(state) is not None]
        self.assertEqual(sorted(published), ["camera.front", "sensor.power"])
        self.pub.stats, self.pub.history, self.pub._health_last = {}, [], {"state": "ok"}
        with mock.patch.object(self.pub, "recent_commands", return_value=[]), \
                mock.patch.object(self.pub, "retained_cleanup_pending", return_value=[]):
            status = self.pub.status()
        self.assertEqual(status["entities_total"], len(published))  # zone.home and the rule-excluded sensor.hidden not counted
        self.assertEqual(status["entities_registry_only"], 1)  # sensor.disabled; zone.office is never published

    def test_the_setting_still_excludes_other_integrations(self):
        self.pub.config.exclude_integrations = ["integration_manager", "demo"]
        self.assertIsNone(self.pub.build_document(self.states["sensor.power"]))
