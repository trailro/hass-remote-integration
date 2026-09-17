"""End-to-end campaign on 0.17.0, publisher side: status fields.

- After a stop the MQTT status (and the answer of the stop) showed base_topic "hass_none", a name no topic uses.
- A removal kept for an uninstall while MQTT was disabled still said "MQTT is disabled" once MQTT was enabled again
  with another broker, where the real reason is that its broker is not the configured one."""

import os
import tempfile
import threading
import unittest
from unittest import mock

from homeassistant.helpers import entity_registry as er

from custom_components.integration_manager import mqtt_publisher as mp
from custom_components.integration_manager.mqtt_rules import MqttRules


def _publisher(identity):
    pub = mp.MqttPublisher.__new__(mp.MqttPublisher)
    pub.config = mp.MqttConfig(enabled=True, host="mqtt")
    pub._connected, pub._moving, pub._stopping = False, False, False
    pub._live_base, pub._live_prefix, pub._key_provider = None, None, lambda: identity
    pub.rules = MqttRules(os.path.join(tempfile.mkdtemp(), "mqtt_rules.json"))
    pub.hass = mock.Mock()
    pub.hass.data = {}
    pub.hass.states.async_all.return_value = []
    pub.stats, pub.history, pub._health_last = {}, [], {"state": "stopped"}
    pub._cleanup_pending, pub._cleanup_pending_lock = {}, threading.Lock()
    pub.hass.config.path.side_effect = lambda *p: os.path.join(tempfile.gettempdir(), "hri-e2e-pub-none", *p)
    return pub


class NoIdentityTest(unittest.TestCase):
    def status(self, pub):
        with mock.patch.object(er, "async_get", return_value=mock.Mock(entities={})), \
                mock.patch.object(pub, "recent_commands", return_value=[]):
            return pub.status()

    def test_after_a_stop_no_topic_is_named(self):
        status = self.status(_publisher(None))
        for field in ("base_topic", "prefix", "manager_topic", "cmd_base", "call_base", "health_topic", "wanted_base_topic"):
            with self.subTest(field=field):
                self.assertIsNone(status[field])
        self.assertFalse(status["has_identity"])
        self.assertNotIn("hass_none", repr(status))

    def test_the_health_document_names_no_topic_either(self):
        pub = _publisher(None)
        pub._health_provider = None  # stopped: no integration to report
        pub._started_at = 0
        with mock.patch.object(mp, "_notification_count", return_value=0):
            doc = pub.build_health()
        self.assertIsNone(doc["base_topic"])
        self.assertNotIn("hass_none", repr(doc))

    def test_with_an_integration_running_the_topics_are_there(self):
        status = self.status(_publisher("hass_demo"))
        self.assertEqual((status["base_topic"], status["prefix"], status["health_topic"], status["cmd_base"]),
                         ("hass_demo", "hass_demo_", "hass_demo/health", "hass_demo/cmd"))

    def test_the_connect_error_after_a_stop(self):
        pub = _publisher(None)
        pub._connect()
        self.assertEqual(pub.stats["connect_error"], "no integration is running: MQTT has no identity (hass_<domain>) until one starts")


class PendingCleanupReasonTest(unittest.TestCase):
    def setUp(self):
        self.pub = _publisher(None)
        broker = {"host": "mqtt", "port": 1883, "tls": False, "username": ""}
        self.key = self.pub._pending_key("hass_gone", broker)
        self.pub._cleanup_pending = {self.key: {"base": "hass_gone", "broker": broker, "error": "MQTT is disabled", "deferred": True,
                                                "since": "2026-09-17T10:00:00+0000"}}

    def pending(self):
        [rec] = self.pub.retained_cleanup_pending()
        return rec

    def test_enabled_with_another_broker(self):
        self.pub.config = mp.MqttConfig(enabled=True, host="mqtt2")
        rec = self.pending()
        self.assertTrue(rec["other_broker"])
        self.assertEqual(rec["error"], "waiting for the MQTT settings to name the broker mqtt:1883 again")

    def test_disabled_still_says_so(self):
        self.pub.config = mp.MqttConfig(enabled=False, host="mqtt2")
        self.assertIn("mqtt:1883", self.pending()["error"])  # another broker: that is what it waits for first
        self.pub.config = mp.MqttConfig(enabled=False, host="mqtt")
        self.assertEqual(self.pending()["error"], "MQTT is disabled")

    def test_enabled_with_its_broker_before_the_next_try(self):
        self.pub.config = mp.MqttConfig(enabled=True, host="mqtt")
        rec = self.pending()
        self.assertEqual((rec["other_broker"], rec["deferred"]), (False, True))
        self.assertNotIn("disabled", rec["error"])

    def test_a_broker_error_of_the_configured_broker_is_kept(self):
        self.pub._cleanup_pending[self.key].update(deferred=False, error="gaierror: [Errno -2] Name does not resolve")
        self.assertEqual(self.pending()["error"], "gaierror: [Errno -2] Name does not resolve")
        self.pub.config = mp.MqttConfig(enabled=True, host="mqtt2")
        self.assertIn("mqtt:1883", self.pending()["error"])
