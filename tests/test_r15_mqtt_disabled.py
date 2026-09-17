"""Fifteenth review, MQTT side, N1: with MQTT disabled an uninstall returned before recording anything, so the retained
data an identity published before MQTT was turned off stayed on the broker for good once MQTT was on again.  Every test
fails on the tree before the fix, except the guard that an identity never published records nothing."""

import os
from unittest import mock

from custom_components.integration_manager import mqtt_publisher as mp
from tests.test_r13_mqtt import KEPT, OURS, _Broker, _Case, _closed_port

SCAN = [["homeassistant/device/+/config", "hass_demo/#"]]


class DisabledBeforeUninstallTest(_Case):
    def ledger(self, base, **broker):
        mp.write_json(os.path.join(self.dir, "integration_manager", "mqtt_identity.json"),
                      {"base": base, "prefix": "homeassistant", **({"broker": broker} if broker else {})})

    async def publish_then_disable_then_uninstall(self, broker):
        pub = self.publisher(running="hass_demo")
        await pub.hass.async_add_executor_job(pub._remember_identity, "hass_demo", "homeassistant")  # what its connect records
        pub = self.publisher(enabled=False)  # MQTT turned off afterwards
        with mock.patch.object(mp.MqttPublisher, "_throwaway_client", lambda self, *a: broker.client(*a)), \
                mock.patch.object(mp.mqtt, "Client", side_effect=AssertionError("no client while MQTT is off")):
            return await self.uninstall(pub)

    async def test_deferred_until_mqtt_is_on_then_runs_once_for_that_identity(self):
        broker = _Broker({**OURS, **KEPT})
        res = await self.publish_then_disable_then_uninstall(broker)
        self.assertTrue(res["ok"])
        self.assertIs(res.get("retained_cleanup_deferred"), True)
        self.assertNotIn("retained_cleanup_failed", res)
        self.assertEqual(res.get("retained_cleanup_broker"), f"127.0.0.1:{self.port}")
        self.assertEqual(broker.scans, [])  # nothing sent while MQTT is off
        self.assertEqual(set(self.on_disk()), {"hass_demo"})
        self.assertTrue(any("hass_demo" in c.args[1] for c in self.emit.call_args_list))
        with mock.patch.object(mp.MqttPublisher, "_throwaway_client", lambda self, *a: broker.client(*a)):
            pub = self.publisher(enabled=False)  # a restart, still off
            await pub._on_cleanup_timer(None)
            self.assertEqual(broker.scans, [])
            pub = self.publisher()  # on again
            await pub._on_cleanup_timer(None)
            await pub._on_cleanup_timer(None)
        self.assertEqual(broker.scans, SCAN)  # once, that identity only
        self.assertIn("cleared 4 retained topics of the uninstalled hass_demo (MQTT was disabled at the uninstall)",
                      [c.args[1] for c in self.emit.call_args_list])
        self.assertEqual(broker.retained, KEPT)
        self.assertEqual(self.on_disk(), {})

    async def test_an_identity_never_published_records_nothing(self):
        for ledger in (None, "hass_other"):
            with self.subTest(ledger=ledger):
                if ledger:
                    self.ledger(ledger)
                pub = self.publisher(enabled=False)
                broker = _Broker({**OURS, **KEPT})
                with mock.patch.object(mp.MqttPublisher, "_throwaway_client", lambda self, *a: broker.client(*a)):
                    res = await self.uninstall(pub)
                    await self.publisher()._on_cleanup_timer(None)
                self.assertNotIn("retained_cleanup_deferred", res)
                self.assertNotIn("retained_cleanup_failed", res)
                self.assertEqual(self.on_disk(), {})
                self.assertEqual(broker.scans, [])

    async def test_bound_to_the_broker_it_was_published_to(self):
        other = _closed_port()
        self.ledger("hass_demo", host="127.0.0.1", port=other, tls=False, username="")
        pub = self.publisher(enabled=False)  # the settings name another broker by now
        res = await self.uninstall(pub)
        self.assertIs(res.get("retained_cleanup_deferred"), True)
        self.assertEqual(res.get("retained_cleanup_broker"), f"127.0.0.1:{other}")
        self.assertIs(res.get("retained_cleanup_other_broker"), True)

    async def test_names_recorded_by_an_older_version_are_bound_to_the_configured_broker(self):
        self.ledger("hass_demo")  # no broker recorded
        res = await self.uninstall(self.publisher(enabled=False))
        self.assertIs(res.get("retained_cleanup_deferred"), True)
        self.assertEqual(res.get("retained_cleanup_broker"), f"127.0.0.1:{self.port}")
        self.assertNotIn("retained_cleanup_other_broker", res)

    async def test_the_first_try_after_mqtt_is_on_replaces_the_disabled_reason(self):
        await self.publish_then_disable_then_uninstall(_Broker({}))
        pub = self.publisher()  # on, the broker unreachable
        await pub._on_cleanup_timer(None)
        rec = self.on_disk()["hass_demo"]
        self.assertFalse(rec.get("deferred"))
        self.assertIn("ConnectionRefusedError", rec["error"])
