"""Malformed persisted metadata must not silently discard exclusions."""

import glob
import json
import os
import tempfile
import unittest
from unittest import mock

from custom_components.integration_manager import mqtt_publisher as mp
from custom_components.integration_manager.mqtt_rules import MqttRules


class PersistedRulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "mqtt_rules.json")

    def load(self, raw):
        text = json.dumps(raw)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)
        rules = MqttRules(self.path)
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), text)
        return rules

    def refused(self, raw):
        with self.assertLogs("custom_components.integration_manager.mqtt_rules", "ERROR"):
            rules = self.load(raw)
        self.assertIsNotNone(rules.problem)
        self.assertEqual(rules.rules, {})
        self.assertTrue(glob.glob(self.path + ".corrupt-*"))
        with self.assertRaises(ValueError):
            rules.set("lock.front", exclude=False)
        pub = object.__new__(mp.MqttPublisher)
        pub._stopping, pub.rules, pub.stats = False, rules, {}
        with mock.patch.object(pub, "_new_client") as connect:
            pub._connect()
        connect.assert_not_called()
        self.assertIn("not connecting", pub.stats["connect_error"])
        return rules

    def test_missing_rules_object_fails_closed_but_explicit_empty_object_is_valid(self):
        self.refused({})
        self.refused({"other": {}})
        self.assertIsNone(self.load({"rules": {}}).problem)

    def test_valid_exclusion_with_bad_metadata_fails_closed(self):
        for field, value in [("icon", 42), ("icon", "missing_colon"), ("name", []),
                             ("entity_category", "unexpected"), ("enabled_by_default", "false")]:
            with self.subTest(field=field, value=value):
                self.refused({"rules": {"lock.*": {"exclude": True, field: value}}})

    def test_invalid_rule_shape_and_exclusion_flag_fail_closed(self):
        self.refused({"rules": {"lock.*": True}})
        self.refused({"rules": {"lock.*": {"exclude": "true"}}})

    def test_legacy_bad_device_class_still_preserves_valid_exclusion_and_name(self):
        with self.assertLogs("custom_components.integration_manager.mqtt_rules", "ERROR"):
            rules = self.load({"rules": {"lock.*": {"exclude": True, "device_class": "BAD", "name": "Front"}}})
        self.assertIsNone(rules.problem)
        self.assertEqual(rules.for_entity("lock.front"), {"exclude": True, "name": "Front"})

    def test_fixed_file_can_load_again(self):
        rules = self.refused({"rules": {"lock.*": {"exclude": True, "icon": 42}}})
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"rules": {"lock.*": {"exclude": True, "icon": "mdi:lock"}}}, fh)
        rules.load()
        self.assertIsNone(rules.problem)
        self.assertTrue(rules.for_entity("lock.front")["exclude"])
