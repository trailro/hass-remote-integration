"""Partial retained scans must not erase uninstall retry records."""

import json
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import mqtt_publisher as mp
from tests.test_r13_mqtt import _Broker


class CompleteCleanupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pub = object.__new__(mp.MqttPublisher)
        self.pub.config = mp.MqttConfig(enabled=True, host="synthetic-broker")
        self.pub.hass = SimpleNamespace(config=SimpleNamespace(path=lambda *p: os.path.join(self.tmp.name, *p)))
        self.pub._key_provider = lambda: None
        self.pub._live_base = None
        self.pub._stopping = False
        self.pub._cleanup_pending_lock = threading.Lock()
        self.pub._cleanup_pending = {}
        self.base = "hass_removed"
        self.key = self.pub._pending_key(self.base, self.pub._broker_identity())
        self.record = {"base": self.base, "prefix": "homeassistant", "broker": self.pub._broker_identity(), "since": "before"}
        os.makedirs(os.path.join(self.tmp.name, "integration_manager"))
        self.pub._set_cleanup_pending(self.key, self.record)
        self.retained = {f"{self.base}/demo/sensor/n{i}": json.dumps({"integration": "demo", "published_at": "t"}).encode()
                         for i in range(3)}
        self.broker = _Broker(self.retained)

    def incomplete(self, mode):
        def client(suffix, *args):
            cl = self.broker.client(suffix, *args)
            if mode == "drop" and suffix == "cleanup":
                subscribe = cl.subscribe
                def dropped(topics):
                    result = subscribe(topics)
                    cl.on_disconnect(cl, None, None, None, None)
                    return result
                cl.subscribe = dropped
                cl.is_connected = lambda: False
            return cl
        budget = 1 if mode == "budget" else mp.RETAINED_SCAN_MAX_BYTES
        with mock.patch.object(self.pub, "_throwaway_client", side_effect=client), \
                mock.patch.object(mp.MqttPublisher, "_collect_quiet", return_value=mode == "time"), \
                mock.patch.object(mp, "RETAINED_SCAN_MAX_BYTES", budget), mock.patch.object(mp.events, "emit"):
            self.pub._retry_pending_cleanups()

    def test_budget_time_and_drop_keep_pending_in_memory_and_on_disk_until_complete_scan(self):
        for mode in ("budget", "time", "drop"):
            with self.subTest(mode=mode):
                self.incomplete(mode)
                self.assertIn(self.key, self.pub._cleanup_pending)
                with open(self.pub._cleanup_pending_file(), encoding="utf-8") as fh:
                    self.assertEqual(json.load(fh)["pending"], [self.record])
                self.assertEqual(self.broker.retained, self.retained)
                self.assertEqual(self.broker.cleared, [])
        # Once a full scan succeeds, the same record is cleared and every owned topic is removed.
        with mock.patch.object(self.pub, "_throwaway_client", side_effect=self.broker.client), \
                mock.patch.object(mp.MqttPublisher, "_collect_quiet", return_value=False), mock.patch.object(mp.events, "emit"):
            self.pub._retry_pending_cleanups()
        self.assertNotIn(self.key, self.pub._cleanup_pending)
        self.assertEqual(self.broker.retained, {})
        with open(self.pub._cleanup_pending_file(), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["pending"], [])

    def test_incomplete_cleanup_reports_failure_instead_of_success_count(self):
        with mock.patch.object(self.pub, "_throwaway_client", side_effect=self.broker.client), \
                mock.patch.object(mp.MqttPublisher, "_collect_quiet", return_value=True):
            n, why = self.pub._clear_retained_checked(self.base, "homeassistant", warn=False)
        self.assertIsNone(n)
        self.assertIn("time limit", why)
        self.assertIn(self.key, self.pub._cleanup_pending)
