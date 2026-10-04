"""A retained scan and an accepted call keep the broker/namespace that supplied them."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import mqtt_publisher as mp
from homeassistant.core import SupportsResponse
from tests.test_r13_mqtt import _Broker
from tests.test_review_mqttfix import _publisher, FakeClient


class RetainedOriginTest(unittest.IsolatedAsyncioTestCase):
    def publisher(self):
        pub = _publisher(asyncio.get_running_loop(), enabled=True, host="broker-a", discovery_enabled=True,
                         exclude_integrations=["demo"])
        pub._cleanup_pending = {}
        pub._orphan_sweep_due = pub._resync_excluded = False
        pub._group_by_device = lambda: ({}, {})
        return pub

    def switch(self, pub, enabled=True):
        pub.config = mp.MqttConfig(enabled=enabled, host="broker-b", discovery_enabled=True)
        pub._live_base, pub._live_prefix = "hass_new", "hass_new_"
        pub._client = FakeClient() if enabled else None
        pub._connected = enabled

    async def test_cancelled_pending_retry_keeps_connection_gate_until_executor_finishes(self):
        pub = self.publisher()
        pub._cleanup_pending = {("hass_removed", "broker-a", 1883): {"base": "hass_removed"}}
        pub._cleanup_retrying = False
        pub._conn_lock = asyncio.Lock()
        job = asyncio.get_running_loop().create_future()
        entered = asyncio.Event()
        def executor(*args):
            entered.set()
            return job
        pub.hass.async_add_executor_job = executor
        task = asyncio.create_task(pub._on_cleanup_timer(None))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertTrue(pub._conn_lock.locked())
        self.assertFalse(job.cancelled())
        task.cancel()  # repeated cancellation must not abandon the running clear either
        await asyncio.sleep(0)
        self.assertTrue(pub._conn_lock.locked())
        job.set_result(None)
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(pub._conn_lock.locked())
        self.assertFalse(pub._cleanup_retrying)

    async def test_pending_uninstall_is_not_cancelled_just_because_its_old_connection_is_live(self):
        for wanted in (None, "hass_next"):
            with self.subTest(wanted=wanted):
                pub = self.publisher()
                old = pub.base_topic
                pub._key_provider = lambda: wanted
                key = pub._pending_key(old, pub._broker_identity())
                pub._cleanup_pending = {key: {"base": old, "prefix": pub.config.discovery_prefix}}
                pub._cancel_pending_cleanup = mock.Mock()
                worker = pub._retained_connection()
                worker._retry_pending_cleanups()
                pub._cancel_pending_cleanup.assert_not_called()
                self.assertIn(key, pub._cleanup_pending)
                self.assertEqual(worker.wanted_base_topic, wanted)
                self.assertEqual(worker.base_topic, old)

    async def test_scan_resumed_after_switch_never_deletes_destination_even_when_disabled_or_refused(self):
        for sweep, flag in [("_async_resync_excluded", "_resync_excluded"),
                            ("_async_sweep_orphans", "_orphan_sweep_due")]:
            for destination in ("connected", "disabled", "refused"):
                with self.subTest(sweep=sweep, destination=destination):
                    pub = self.publisher()
                    topic = pub.base_topic + "/demo/sensor/removed"
                    retained = {topic: json.dumps({"integration": "demo", "published_at": "t", "entity_id": "sensor.removed"}).encode()}
                    brokers = {"broker-a": _Broker(retained), "broker-b": _Broker(retained)}
                    entered, resume = asyncio.Event(), asyncio.Event()

                    async def executor(func, *args):
                        found = func(*args)
                        if func.__name__ == "_retained_scan":
                            entered.set()
                            await resume.wait()
                        return found

                    pub.hass.async_add_executor_job = executor
                    def client(worker, *args):
                        return brokers[worker.config.host].client(*args)
                    with mock.patch.object(mp.MqttPublisher, "_throwaway_client", client), \
                            mock.patch.object(mp.MqttPublisher, "_collect_quiet", return_value=False):
                        task = asyncio.create_task(getattr(pub, sweep)())
                        await entered.wait()
                        self.switch(pub, enabled=destination != "disabled")
                        if destination == "refused":
                            pub._client, pub._connected = None, False
                        resume.set()
                        await task
                    self.assertEqual(brokers["broker-b"].cleared, [])
                    self.assertEqual(brokers["broker-b"].retained, retained)
                    self.assertTrue(getattr(pub, flag))

    async def test_switch_during_blocking_cleanup_clears_only_the_scanned_broker(self):
        pub = self.publisher()
        topic = pub.base_topic + "/status"
        brokers = {"broker-a": _Broker({topic: b"offline"}), "broker-b": _Broker({topic: b"foreign"})}
        def client(worker, suffix, *args):
            cl = brokers[worker.config.host].client(suffix, *args)
            if suffix == "cleanup":
                subscribe = cl.subscribe
                def switched(topics):
                    result = subscribe(topics)
                    self.switch(pub, enabled=False)  # scan callback: the executor is still running
                    return result
                cl.subscribe = switched
            return cl
        with mock.patch.object(mp.MqttPublisher, "_throwaway_client", client), \
                mock.patch.object(mp.MqttPublisher, "_collect_quiet", return_value=False):
            n, why = pub._clear_retained_checked(pub.base_topic, pub.config.discovery_prefix)
        self.assertEqual((n, why), (1, ""))
        self.assertEqual(brokers["broker-a"].cleared, [topic])
        self.assertEqual(brokers["broker-b"].retained, {topic: b"foreign"})
        self.assertEqual(brokers["broker-b"].cleared, [])

    async def test_old_discovery_cleanup_does_not_reset_new_connection_statistics(self):
        pub = self.publisher()
        pub.stats["discovery_devices"], pub.stats["discovery_components"] = 7, 31
        topic = pub.config.discovery_prefix + "/device/old/config"
        broker = _Broker({topic: b'{}'})
        pub._is_ours = lambda *args: True
        worker = pub._retained_connection()
        def client(_worker, suffix, *args):
            cl = broker.client(suffix, *args)
            if suffix == "undisc":
                subscribe = cl.subscribe
                def switched(topics):
                    result = subscribe(topics)
                    self.switch(pub)
                    return result
                cl.subscribe = switched
            return cl
        with mock.patch.object(mp.MqttPublisher, "_throwaway_client", client),                 mock.patch.object(mp.MqttPublisher, "_collect_quiet", return_value=False):
            self.assertEqual(worker._clear_discovery_retained(), 1)
        self.assertEqual(pub.stats["discovery_devices"], 7)
        self.assertEqual(pub.stats["discovery_components"], 31)

    async def test_switch_between_document_clear_and_discovery_clear_is_retried(self):
        pub = self.publisher()
        topic = pub.base_topic + "/demo/sensor/removed"
        config_topic = pub.config.discovery_prefix + "/device/old/config"
        found = {topic: b'{"integration":"demo","published_at":"t"}', config_topic: b'{}'}
        pub._is_ours = lambda *args: True
        async def executor(func, *args):
            if func.__name__ == "_retained_scan":
                return found
            self.switch(pub)
            return None
        pub.hass.async_add_executor_job = executor
        await pub._async_resync_excluded()
        self.assertEqual(pub._client.published, [])
        self.assertTrue(pub._resync_excluded)


class CallOriginTest(unittest.IsolatedAsyncioTestCase):
    async def scenario(self, change):
        pub = _publisher(asyncio.get_running_loop(), enabled=True, host="broker-a")
        first_started, finish = asyncio.Event(), asyncio.Event()
        completed = []
        executions = []
        async def call(*args, **kwargs):
            executions.append(args)
            if len(executions) == 1:
                first_started.set()
                await finish.wait()
            return {"from": "a" if len(executions) == 1 else "b"}
        pub.hass.services.async_call = call
        pub.hass.services.supports_response.return_value = SupportsResponse.ONLY
        pub._call_target_problem = lambda *args: None
        pub.hass.async_create_task = lambda coro: completed.append(asyncio.create_task(coro)) or completed[-1]
        old_client = pub._client
        old_client._hri_origin = pub._configured_origin()
        pub._on_call("demo/read", '{"_id":"same"}')
        await first_started.wait()
        if change == "late_broker":
            async def timeout_sent():
                while not old_client.published:
                    await asyncio.sleep(0)
            await asyncio.wait_for(timeout_sent(), 1)
            self.assertIn("timeout", json.loads(old_client.published[-1][1])["error"])
        old_origin = pub._reply_origin()
        if change in ("broker", "late_broker"):
            pub.config = mp.MqttConfig(enabled=True, host="broker-b")
        elif change == "identity":
            pub._live_base, pub._live_prefix = "hass_new", "hass_new_"
        elif change == "settings_only":
            pub.config = mp.MqttConfig(enabled=True, host="broker-b")  # adopted before client reconnects
        if change != "settings_only":
            pub._client = FakeClient()
            pub._client._hri_origin = pub._configured_origin()
        new_client = pub._client
        finish.set()
        await asyncio.gather(*completed)
        replies_before_retry = list(new_client.published)
        pub._on_call("demo/read", '{"_id":"same"}')
        await asyncio.sleep(0)
        await asyncio.gather(*completed)
        return pub, executions, replies_before_retry, old_origin

    async def test_broker_and_identity_changes_drop_old_reply_and_execute_reused_id(self):
        for change in ("broker", "identity"):
            with self.subTest(change=change):
                pub, executions, replies, _ = await self.scenario(change)
                self.assertEqual(replies, [])
                self.assertEqual(len(executions), 2)
                answer = json.loads(pub._client.published[-1][1])
                self.assertNotIn("duplicate", answer)
                self.assertEqual(len(pub._calls), 2)

    @mock.patch.object(mp, "CALL_TIMEOUT_S", 0.01)
    async def test_late_completion_after_timeout_is_not_sent_to_new_broker(self):
        pub, executions, replies, _ = await self.scenario("late_broker")
        self.assertEqual(replies, [])
        self.assertEqual(len(executions), 2)
        self.assertEqual(len(pub._calls), 2)
        old = next(iter(pub._calls.values()))
        self.assertTrue(old["result"]["late"])

    async def test_same_origin_reconnect_keeps_reply_and_dedup(self):
        pub, executions, replies, origin = await self.scenario("same")
        self.assertEqual(len(executions), 1)
        self.assertEqual(replies[0][0], origin[-1] + "/result/demo/read")
        self.assertTrue(json.loads(pub._client.published[-1][1])["duplicate"])

    async def test_settings_adopted_before_reconnect_do_not_change_client_reply_origin(self):
        pub, executions, replies, origin = await self.scenario("settings_only")
        self.assertEqual(len(executions), 1)
        self.assertEqual(replies[0][0], origin[-1] + "/result/demo/read")
        self.assertTrue(json.loads(pub._client.published[-1][1])["duplicate"])
