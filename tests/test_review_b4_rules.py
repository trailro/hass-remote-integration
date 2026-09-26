"""Review of b4cd1a1.  S3-4: an unreadable mqtt_rules.json dropped every rule, so what the rules excluded was published
and could be commanded (exclusions failed open), and the next rule change overwrote the file for good."""

import asyncio
import glob
import json
import os
import tempfile
import unittest
from unittest import mock

from custom_components.integration_manager import mqtt_publisher as mp
from custom_components.integration_manager.mqtt_rules import MqttRules
from tests import test_camp_publish as camp
from tests import test_e2e_pub_status as pub_status
from tests.test_e2e_pub_status import _publisher

BROKEN = '{"rules": {"lock.*": {"exclude": true}'


class BrokenFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "mqtt_rules.json")

    def _rules(self, text):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)
        with self.assertLogs("custom_components.integration_manager.mqtt_rules", "ERROR"):
            return MqttRules(self.path)

    def test_the_damaged_file_is_kept(self):
        rules = self._rules(BROKEN)
        self.assertIn("mqtt_rules.json is not valid JSON", rules.problem)
        [kept] = glob.glob(self.path + ".corrupt-*")
        with open(kept, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), BROKEN)
        with open(self.path, encoding="utf-8") as fh:  # still there: the next start must not read "no rules" either
            self.assertEqual(fh.read(), BROKEN)

    def test_rules_that_are_not_an_object(self):
        for text in ("[]", '{"rules": []}', '"x"'):
            with self.subTest(text=text):
                self.assertIsNotNone(self._rules(text).problem)

    def test_changes_are_refused(self):
        rules = self._rules(BROKEN)
        with self.assertRaisesRegex(ValueError, "mqtt_rules.json"):
            rules.set("lock.door", exclude=False)
        with self.assertRaisesRegex(ValueError, "mqtt_rules.json"):
            rules.replace_all({"light.x": {"name": "X"}})
        with self.assertRaisesRegex(ValueError, "mqtt_rules.json"):
            asyncio.run(rules.async_save())
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), BROKEN)

    def test_fixed_or_removed_it_is_read_again(self):
        rules = self._rules(BROKEN)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"rules": {"lock.*": {"exclude": True}}}, fh)
        rules.load()
        self.assertIsNone(rules.problem)
        self.assertTrue(rules.for_entity("lock.door")["exclude"])
        os.remove(self.path)
        rules.load()
        self.assertIsNone(rules.problem)

    def test_no_file_is_no_problem(self):
        self.assertIsNone(MqttRules(self.path).problem)


class FailClosedTest(unittest.TestCase):
    """Which entities are excluded is unknown: the publisher does not connect, and says why."""

    def _broken_rules(self):
        path = os.path.join(tempfile.mkdtemp(), "mqtt_rules.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(BROKEN)
        with self.assertLogs("custom_components.integration_manager.mqtt_rules", "ERROR"):
            return MqttRules(path)

    def test_no_connection(self):
        pub = camp._publisher()
        pub._client, pub._connected, pub._probed_ok = None, False, set()
        pub.rules = self._broken_rules()
        with mock.patch.object(pub, "probe_foreign", return_value={}), \
                mock.patch.object(pub, "_sweep_old_identity", return_value=True), \
                mock.patch.object(pub, "_new_client") as new_client, \
                mock.patch.object(pub, "_cancel_pending_cleanup"), mock.patch.object(mp.MqttPublisher, "_pending_key", return_value=None):
            pub._cleanup_pending = {}
            pub._connect()
        new_client.assert_not_called()
        self.assertIsNone(pub._client)
        self.assertIn("mqtt_rules.json is not valid JSON", pub.stats["connect_error"])

    def test_a_reconnect_reads_the_fixed_file(self):
        pub = camp._publisher(enabled=True)
        pub.rules = self._broken_rules()
        pub._pending_clears, pub._registry_timer, pub._services_timer = set(), None, None
        pub._republish_interval = pub.config.republish_interval_s
        pub._load = lambda: pub.config
        pub.publish_health = lambda: None
        pub._disconnect = lambda publish_offline=True: None
        pub._drop_newly_excluded = lambda new: None
        pub._connected = False
        connected = []
        pub._connect = lambda: connected.append(pub.rules.problem)

        async def executor(func, *args):
            return func(*args)

        pub.hass.async_add_executor_job = executor
        with open(pub.rules.path, "w", encoding="utf-8") as fh:
            json.dump({"rules": {}}, fh)
        asyncio.run(pub._async_reconnect_locked())
        self.assertEqual(connected, [None])

    def test_the_status_says_why(self):
        pub = _publisher("hass_demo")
        pub.rules = self._broken_rules()
        status = pub_status.NoIdentityTest.status(None, pub)
        self.assertIn("mqtt_rules.json is not valid JSON", status["rules_error"])
        self.assertIsNone(pub_status.NoIdentityTest.status(None, _publisher("hass_demo"))["rules_error"])


if __name__ == "__main__":
    unittest.main()
