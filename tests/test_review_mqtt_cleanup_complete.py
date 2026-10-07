"""Partial retained scans must not erase uninstall retry records."""

import json
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import mqtt_publisher as mp
from tests import test_camp_publish as camp
from tests.test_r13_mqtt import _Broker, _config


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


class UndiscoverCompleteTest(unittest.IsolatedAsyncioTestCase):
    """Discovery turned off (or Undo): a partial scan must not mark the undiscover as done."""

    def setUp(self):
        pub = self.pub = camp._publisher(enabled=True, discovery_enabled=False, manager_discovery=False)
        pub.stats.update(discovery_devices=0, discovery_components=0)
        pub._pending_clears, pub._orphan_sweep_due, pub._resync_excluded = set(), False, False
        pub._identity_sweep_due, pub._ids_undecided, pub._ids_switch_due = False, False, False
        pub._undiscover_due = True
        pub._discovery_map, pub._blocks = {}, {}
        pub.hass.states.async_all.return_value = []
        pub.publish_health = pub._publish_manager_discovery = pub.publish_manager = lambda: None
        pub._publish_services = mock.AsyncMock()
        self.due = []
        pub._set_undiscover_due = lambda due: (self.due.append(due), setattr(pub, "_undiscover_due", due))

        async def executor(fn, *args):
            return fn(*args)

        pub.hass.async_add_executor_job = executor
        self.topic = f"homeassistant/device/{camp.BASE}_dev/config"
        self.broker = _Broker({self.topic: _config(camp.BASE)})

    async def republish(self, cut):
        with mock.patch.object(self.pub, "_throwaway_client", side_effect=self.broker.client), \
                mock.patch.object(mp.MqttPublisher, "_collect_quiet", return_value=cut), mock.patch.object(mp.events, "emit"):
            await self.pub.async_republish_all()

    async def test_cut_scan_keeps_undiscover_due_until_a_complete_scan(self):
        with self.assertLogs(mp._LOGGER, "WARNING"):
            await self.republish(cut=True)
        self.assertEqual(self.due, [])
        self.assertTrue(self.pub._undiscover_due)
        self.assertEqual(self.broker.cleared, [])
        await self.republish(cut=False)
        self.assertEqual(self.due, [False])
        self.assertEqual(self.broker.cleared, [self.topic])
