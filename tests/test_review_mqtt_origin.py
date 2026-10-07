"""A retained scan and an accepted call keep the broker/namespace that supplied them; a stop ends a cleanup retry."""

import asyncio
import dataclasses
import json
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

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
                        if isinstance(found, dict):  # the scan; a clear returns nothing
                            entered.set()
                            await resume.wait()
                        return found

                    pub.hass.async_add_executor_job = executor
                    def client(_pub, *args, origin=None):
                        return brokers[origin.config.host].client(*args)
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
        def client(_pub, suffix, *args, origin=None):
            cl = brokers[(origin.config if origin else pub.config).host].client(suffix, *args)
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
        def client(_pub, suffix, *args, **_kw):
            cl = broker.client(suffix, *args)
            if suffix == "undisc":
                subscribe = cl.subscribe
                def switched(topics):
                    result = subscribe(topics)
                    self.switch(pub)
                    return result
                cl.subscribe = switched
            return cl
        with mock.patch.object(mp.MqttPublisher, "_throwaway_client", client), \
                mock.patch.object(mp.MqttPublisher, "_collect_quiet", return_value=False):
            self.assertEqual(pub._clear_discovery_retained(), 1)
        self.assertEqual(pub.stats["discovery_devices"], 7)
        self.assertEqual(pub.stats["discovery_components"], 31)

    async def test_switch_between_document_clear_and_discovery_clear_is_retried(self):
        pub = self.publisher()
        topic = pub.base_topic + "/demo/sensor/removed"
        config_topic = pub.config.discovery_prefix + "/device/old/config"
        found = {topic: b'{"integration":"demo","published_at":"t"}', config_topic: b'{}'}
        pub._is_ours = lambda *args: True
        jobs = []
        async def executor(func, *args):
            jobs.append(func)
            if len(jobs) == 1:
                return found  # the scan
            self.switch(pub)  # during the clear of the documents
            return None
        pub.hass.async_add_executor_job = executor
        await pub._async_resync_excluded()
        self.assertEqual(pub._client.published, [])
        self.assertTrue(pub._resync_excluded)

    async def test_settings_adopted_without_reconnect_do_not_discard_sweeps(self):
        """A reload adopts mqtt.json while the connection keeps its broker: a sweep submitted after it is not discarded."""
        for change in ({"qos": 1}, {"host": "broker-b"}):
            with self.subTest(change=change):
                pub = self.publisher()
                pub._client._hri_origin = pub._configured_origin()  # bound at the connect, before the reload
                pub.config = dataclasses.replace(pub.config, **change)  # async_reload_config: a new object, no reconnect
                topic = pub.base_topic + "/demo/sensor/excluded"
                broker = _Broker({topic: json.dumps({"integration": "demo", "published_at": "t"}).encode()})

                async def executor(func, *args):
                    return func(*args)
                pub.hass.async_add_executor_job = executor
                with mock.patch.object(mp.MqttPublisher, "_throwaway_client", lambda _pub, *a, **k: broker.client(*a)), \
                        mock.patch.object(mp.MqttPublisher, "_collect_quiet", return_value=False):
                    await pub._async_resync_excluded()
                self.assertEqual(broker.cleared, [topic])
                self.assertFalse(pub._resync_excluded)

    async def test_broker_traits_learned_by_a_cleanup_stay_with_the_publisher(self):
        pub = self.publisher()
        built = []

        def client_factory(*_a, **kwargs):
            c = mock.Mock()
            c.protocol = kwargs.get("protocol")
            c.max_inflight_messages = 20
            refused = not built
            c.connect.side_effect = lambda *a, **k: c.on_connect(
                c, None, None, ReasonCode(PacketTypes.CONNACK, "Unsupported protocol version") if refused else 0, None)
            c.subscribe.side_effect = lambda topics: c.on_subscribe(c, None, 1, [ReasonCode(PacketTypes.SUBACK, identifier=1)])
            c.publish.return_value.is_published.return_value = True
            built.append(c)
            return c

        with mock.patch.object(mp.mqtt, "Client", side_effect=client_factory), \
                mock.patch.object(mp.MqttPublisher, "_collect_quiet", return_value=False), \
                self.assertLogs(mp._LOGGER, "WARNING"), mock.patch.object(mp.events, "emit"):
            n, why = pub._clear_retained_checked("hass_removed", pub.config.discovery_prefix)
        self.assertEqual((n, why), (0, ""))
        self.assertTrue(pub._mqtt311)
        self.assertEqual(pub._broker_traits(), (True, 0))  # the next client of this broker speaks 3.1.1 at once

    async def test_orphan_sweep_and_services_still_run_in_the_pass_that_turns_discovery_off(self):
        pub = self.publisher()
        pub.config = mp.MqttConfig(enabled=True, host="broker-a", discovery_enabled=False)
        pub.stats.update(discovery_devices=0, discovery_components=0)
        pub._pending_clears, pub._resync_excluded = set(), False
        pub._identity_sweep_due, pub._ids_undecided, pub._ids_switch_due = False, False, False
        pub._undiscover_due, pub._orphan_sweep_due = True, True
        pub._started_at = 0.0
        pub.hass.is_running = True
        pub._discovery_map, pub._blocks = {}, {}
        pub.hass.states.async_all.return_value = []
        pub.publish_health = pub._publish_manager_discovery = pub.publish_manager = lambda: None
        pub._publish_services = mock.AsyncMock()
        pub._async_sweep_orphans = mock.AsyncMock()
        pub._set_undiscover_due = mock.Mock()

        async def executor(func, *args):
            if func == pub._clear_discovery_retained:
                pub._live_base, pub._live_prefix = "hass_new", "hass_new_"  # a Move while the configs were cleared
                return 3
            return func(*args)
        pub.hass.async_add_executor_job = executor
        await pub.async_republish_all()
        pub._set_undiscover_due.assert_not_called()  # due again for the names announced now
        pub._async_sweep_orphans.assert_awaited_once()
        pub._publish_services.assert_awaited_once()


class StopDuringRetryTest(unittest.IsolatedAsyncioTestCase):
    """Home Assistant stopping during a pending-cleanup retry: the retry ends at once and clears nothing more."""

    async def test_stop_during_the_scan_returns_promptly_and_clears_nothing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.makedirs(os.path.join(tmp.name, "integration_manager"))
        pub = _publisher(asyncio.get_running_loop(), enabled=True, host="broker-a")
        pub.hass.config.path = lambda *p: os.path.join(tmp.name, *p)
        pub.hass.async_add_executor_job = lambda f, *a: asyncio.get_running_loop().run_in_executor(None, f, *a)
        pub._conn_lock = asyncio.Lock()
        pub._cleanup_retrying = False
        pub._cleanup_pending_lock = threading.Lock()
        pub._cleanup_pending = {}
        retained, records = {}, []
        for base in ("hass_gone_a", "hass_gone_b"):
            retained[f"{base}/demo/sensor/x"] = json.dumps({"integration": "demo", "published_at": "t"}).encode()
            key = pub._pending_key(base, pub._broker_identity())
            records.append({"base": base, "prefix": "homeassistant", "broker": pub._broker_identity(), "since": "before"})
            pub._set_cleanup_pending(key, records[-1])
        broker = _Broker(retained)

        def client(_pub, suffix, *args, **_kw):
            cl = broker.client(suffix, *args)
            subscribe = cl.subscribe
            def stopping(topics):
                result = subscribe(topics)
                pub._stopping = True  # _on_stop, while the first record's scan collects
                return result
            cl.subscribe = stopping
            return cl

        started = time.monotonic()
        with mock.patch.object(mp.MqttPublisher, "_throwaway_client", client), mock.patch.object(mp.events, "emit"):
            await asyncio.wait_for(pub._on_cleanup_timer(None), 5)
        elapsed = time.monotonic() - started
        self.assertEqual(broker.cleared, [])
        self.assertEqual(broker.retained, retained)
        self.assertEqual(len(broker.scans), 1)  # the second record was never scanned
        self.assertEqual(sorted(r["base"] for r in pub._cleanup_pending.values()), ["hass_gone_a", "hass_gone_b"])
        with open(pub._cleanup_pending_file(), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["pending"], records)
        self.assertFalse(pub._conn_lock.locked())
        self.assertLess(elapsed, 1.5)  # the scan's own minimum is 2 s

    async def test_cancelled_while_stopping_releases_the_connection_gate(self):
        pub = _publisher(asyncio.get_running_loop(), enabled=True, host="broker-a")
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
        pub._stopping = True
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=1)
        if not done:  # still holding the gate: let it end so the test does not hang
            job.set_result(None)
            await asyncio.wait({task}, timeout=1)
        self.assertIn(task, done)
        self.assertTrue(task.cancelled())
        self.assertFalse(pub._conn_lock.locked())
        job.cancel()


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
