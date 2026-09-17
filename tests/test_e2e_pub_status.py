"""End-to-end campaign on 0.17.0, publisher side: status fields.

- A removal kept for an uninstall while MQTT was disabled still said "MQTT is disabled" once MQTT was enabled again
  with another broker, where the real reason is that its broker is not the configured one."""

import os
import tempfile
import threading
import unittest
from unittest import mock

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
