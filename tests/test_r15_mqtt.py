"""Fifteenth review, MQTT side, N2: a pending removal of an uninstalled identity's retained data did not name its broker,
so after the settings moved to another broker the timer scanned that one, found nothing and dropped the record while the
first broker kept the data.  Every test fails on the tree before the fix."""

import os
from unittest import mock

from custom_components.integration_manager import mqtt_publisher as mp
from tests.test_r13_mqtt import KEPT, OURS, _Broker, _Case, _closed_port

SCAN = [["homeassistant/device/+/config", "hass_demo/#"]]


class BoundToItsBrokerTest(_Case):
    async def test_another_broker_never_completes_it_and_its_own_does(self):
        port_a, port_b = self.port, _closed_port()
        pub, _res, _scan = await self.fail_uninstall()  # broker A unreachable
        a, b = _Broker({**OURS, **KEPT}), _Broker(dict(KEPT))
        by_port = {port_a: a, port_b: b}  # a throwaway client reaches the broker the settings name at that moment
        with mock.patch.object(mp.MqttPublisher, "_throwaway_client", lambda self, *args: by_port[self.config.port].client(*args)):
            pub.config = mp.MqttConfig(enabled=True, host="127.0.0.1", port=port_b)  # the settings moved to B
            await pub._on_cleanup_timer(None)
            await pub._on_cleanup_timer(None)
            self.assertEqual(b.scans, [])
            self.assertEqual(b.cleared, [])
            self.assertEqual(set(self.on_disk()), {"hass_demo"})
            self.assertEqual(pub.retained_cleanup_pending(),
                             [{"base_topic": "hass_demo", "broker": f"127.0.0.1:{port_a}", "other_broker": True, "deferred": False,
                               "error": mock.ANY, "since": mock.ANY}])
            pub = self.publisher(port=port_b)  # a restart on B changes nothing either
            await pub._on_cleanup_timer(None)
            self.assertEqual(b.scans, [])
            pub.config = mp.MqttConfig(enabled=True, host="127.0.0.1", port=port_a)  # back on A, reachable now
            await pub._on_cleanup_timer(None)
        self.assertEqual(a.scans, SCAN)
        self.assertEqual(a.retained, KEPT)
        self.assertEqual(self.on_disk(), {})

    async def test_an_identity_sweep_on_another_broker_keeps_it(self):
        port_b = _closed_port()
        await self.fail_uninstall()
        pub = self.publisher(running="hass_other", port=port_b)
        mp.write_json(os.path.join(self.dir, "integration_manager", "mqtt_identity.json"), {"base": "hass_demo", "prefix": "homeassistant"})
        b = _Broker({**OURS, **KEPT})
        with mock.patch.object(mp.MqttPublisher, "_throwaway_client", lambda self, *a: b.client(*a)):
            self.assertTrue(await pub.hass.async_add_executor_job(pub._sweep_old_identity, "hass_other"))
        self.assertEqual(set(self.on_disk()), {"hass_demo"})

    async def test_starting_that_identity_on_another_broker_keeps_it(self):
        port_b = _closed_port()
        await self.fail_uninstall()
        for running, enabled in (("hass_demo", True), ("hass_demo", False)):
            pub = self.publisher(running=running, enabled=enabled, port=port_b, force_base_topic=True)
            pub._client, pub._connected, pub._probed_ok, pub.stats = None, False, set(), {}
            b = _Broker({})
            with mock.patch.object(mp.mqtt, "Client"), mock.patch.object(mp.MqttPublisher, "_throwaway_client", lambda self, *a: b.client(*a)), \
                    mock.patch.object(mp.MqttPublisher, "_sweep_old_identity", return_value=True):
                if enabled:
                    await pub.hass.async_add_executor_job(pub._connect)
                await pub._on_cleanup_timer(None)
            self.assertEqual(set(self.on_disk()), {"hass_demo"})

    async def test_a_second_outage_on_another_broker_keeps_both(self):
        port_a, port_b = self.port, _closed_port()
        await self.fail_uninstall()
        pub = self.publisher(port=port_b)
        await self.uninstall(pub)  # installed again on B, uninstalled while B is down too
        self.assertEqual(sorted(e["broker"] for e in pub.retained_cleanup_pending()), sorted([f"127.0.0.1:{port_a}", f"127.0.0.1:{port_b}"]))


    async def test_an_uninstall_on_another_broker_names_the_one_that_still_waits(self):
        port_a, port_b = self.port, _closed_port()
        await self.fail_uninstall()
        b = _Broker({**OURS, **KEPT})
        with mock.patch.object(mp.MqttPublisher, "_throwaway_client", lambda self, *a: b.client(*a)):
            res = await self.uninstall(self.publisher(port=port_b))  # installed again on B, uninstalled with B reachable
        self.assertEqual(b.retained, KEPT)
        self.assertIs(res["retained_cleanup_failed"], True)
        self.assertEqual(res["retained_cleanup_broker"], f"127.0.0.1:{port_a}")
        self.assertIs(res["retained_cleanup_other_broker"], True)
        self.assertEqual(set(self.on_disk()), {"hass_demo"})


class RecordsWithoutABrokerTest(_Case):
    async def test_bound_to_the_broker_configured_when_they_are_read(self):
        mp.write_json(os.path.join(self.dir, "integration_manager", "mqtt_cleanup_pending.json"),
                      {"pending": {"hass_demo": {"prefix": "homeassistant", "error": "OSError: down", "since": "t"}}})
        port_b = _closed_port()
        pub = self.publisher()  # read with broker A configured
        self.assertEqual(self.on_disk()["hass_demo"]["broker"], {"host": "127.0.0.1", "port": self.port, "tls": False, "username": ""})
        b = _Broker({**OURS, **KEPT})
        with mock.patch.object(mp.MqttPublisher, "_throwaway_client", lambda self, *a: b.client(*a)):
            pub.config = mp.MqttConfig(enabled=True, host="127.0.0.1", port=port_b)
            await pub._on_cleanup_timer(None)
            self.assertEqual(b.scans, [])
            pub = self.publisher(port=port_b)  # read again later: it stays bound to A
            await pub._on_cleanup_timer(None)
        self.assertEqual(b.scans, [])
        self.assertEqual(set(self.on_disk()), {"hass_demo"})
