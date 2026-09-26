"""Thirteenth review, MQTT side.  F4: an uninstall while the broker was unreachable answered `retained_cleared: 0` like a
cleanup that found nothing, and nothing ever tried again: with no identity the manager does not connect, so the main
Home Assistant kept the entities of the removed integration until some other identity happened to sweep them.  Every
test fails on the tree before the fix, except the answer with a reachable broker, which must stay as it was."""

import asyncio
import json
import logging
import os
import shutil
import socket
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from custom_components.integration_manager import discovery as disc
from custom_components.integration_manager import manage_views
from custom_components.integration_manager import mqtt_publisher as mp
from custom_components.integration_manager.mqtt_rules import MqttRules


def _closed_port() -> int:
    s = socket.create_server(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _config(base: str) -> bytes:
    return json.dumps({"device": {"identifiers": [base]}, "origin": disc.origin(base + "_"), "components": {}}).encode()


class _Broker:
    """The retained store of a broker, reached by the throwaway clients the manager builds: SUBACK granting QoS 1, then
    the retained messages matching the filters; an empty retained publish removes the topic and is acknowledged."""

    def __init__(self, retained: dict[str, bytes]) -> None:
        self.retained = dict(retained)
        self.scans: list[list[str]] = []
        self.cleared: list[str] = []

    def client(self, _suffix, _what, _deadline, on_message=None):
        broker = self

        class Client:
            on_subscribe = None

            def subscribe(self, topics):
                filters = [t for t, _qos in topics]
                broker.scans.append(filters)
                self.on_subscribe(self, None, 1, [ReasonCode(PacketTypes.SUBACK, identifier=1) for _ in topics], None)
                for topic, payload in list(broker.retained.items()):
                    if any(mqtt.topic_matches_sub(f, topic) for f in filters):
                        on_message(self, None, SimpleNamespace(topic=topic, payload=payload, retain=True))
                return mqtt.MQTT_ERR_SUCCESS, 1

            def publish(self, topic, payload=None, qos=0, retain=False):
                if retain and payload == "":
                    broker.retained.pop(topic, None)
                    broker.cleared.append(topic)
                return SimpleNamespace(rc=mqtt.MQTT_ERR_SUCCESS, is_published=lambda: True)

            is_connected = staticmethod(lambda: True)
            disconnect = loop_stop = staticmethod(lambda: None)
            socket = staticmethod(lambda: None)

        return Client()


OURS = {
    "hass_demo/status": b"offline",
    "hass_demo/health": json.dumps({"updated_at": "t", "base_topic": "hass_demo"}).encode(),
    "hass_demo/sensor/sensor/a": json.dumps({"published_at": "t", "integration": "demo"}).encode(),
    "homeassistant/device/hass_demo_dev/config": _config("hass_demo"),
}
KEPT = {
    "hass_demo/notes": b"someone else's",  # under the base topic, not ours
    "hass_other/status": b"online",  # another identity
    "homeassistant/device/hass_other_dev/config": _config("hass_other"),
    "ha2/device/hass_demo_dev/config": _config("hass_demo"),  # another discovery prefix
}


class _Case(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "integration_manager"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.port = _closed_port()  # an unreachable broker: paho's connect is refused
        self.emit = mock.patch.object(mp.events, "emit").start()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(mp.MqttPublisher, "_collect_quiet", staticmethod(lambda *a, **k: None)).start()
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def publisher(self, running=None, **config):
        pub = object.__new__(mp.MqttPublisher)
        pub.config = mp.MqttConfig(**{"enabled": True, "host": "127.0.0.1", "port": self.port, **config})
        loop = asyncio.get_running_loop()
        pub.hass = SimpleNamespace(config=SimpleNamespace(path=lambda *p: os.path.join(self.dir, *p)),
                                   async_add_executor_job=lambda f, *a: loop.run_in_executor(None, f, *a))
        pub._key_provider = lambda: running
        pub._live_base = pub._live_prefix = None
        pub._conn_lock = asyncio.Lock()
        pub._topics, pub._last_hash, pub._discovery_map, pub._blocks = {}, {}, {}, {}
        pub.rules = MqttRules("/nonexistent/mqtt_rules.json")  # no file: no rules, and nothing holds the connection
        if hasattr(pub, "_read_cleanup_pending"):
            pub._cleanup_pending = pub._read_cleanup_pending()  # what async_start reads
        return pub

    def on_disk(self):
        try:
            with open(os.path.join(self.dir, "integration_manager", "mqtt_cleanup_pending.json"), encoding="utf-8") as fh:
                pending = json.load(fh)["pending"]
        except FileNotFoundError:
            return {}
        return {r["base"]: r for r in pending} if isinstance(pending, list) else pending

    async def uninstall(self, pub, domain="demo"):
        """InstalledActionView with an installer that, like the real one, clears the identity once itself."""
        installer = SimpleNamespace(running=None, last_identity_cleared=0)

        async def uninstall(d):
            installer.last_identity_cleared = await pub.async_clear_identity(f"hass_{d}") or 0
            return {"ok": True}

        installer.uninstall = uninstall
        view = object.__new__(manage_views.InstalledActionView)
        view.installer, view.publisher = installer, pub
        request = SimpleNamespace(headers={}, query={}, content_type="application/json", json=mock.AsyncMock(return_value={}))
        return json.loads((await view.post(request, domain=domain, action="uninstall")).body)

    async def fail_uninstall(self):
        pub = self.publisher()
        with mock.patch.object(mp.MqttPublisher, "_retained_scan", autospec=True, side_effect=mp.MqttPublisher._retained_scan) as scan:
            res = await self.uninstall(pub)
        return pub, res, scan


class UninstallDuringAnOutageTest(_Case):
    async def test_the_answer_says_the_cleanup_failed(self):
        pub, res, scan = await self.fail_uninstall()
        self.assertEqual(scan.call_count, 2)  # the installer's pass and the view's, both refused
        self.assertTrue(res["ok"])  # removed here all the same
        self.assertEqual(res["retained_cleared"], 0)
        self.assertIs(res["retained_cleanup_failed"], True)
        self.assertIn("ConnectionRefusedError", res["retained_cleanup_error"])
        self.assertEqual(set(self.on_disk()), {"hass_demo"})
        self.assertEqual(self.on_disk()["hass_demo"]["prefix"], "homeassistant")
        self.assertEqual([k[0] for k in pub._cleanup_pending], ["hass_demo"])

    async def test_one_timeline_line(self):
        await self.fail_uninstall()
        lines = [c.args[1] for c in self.emit.call_args_list]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("hass_demo", lines[0])
        self.assertIn("not cleared", lines[0])

    async def test_a_reachable_broker_answers_as_before(self):
        pub = self.publisher()
        broker = _Broker({**OURS, **KEPT})
        with mock.patch.object(mp.MqttPublisher, "_throwaway_client", lambda self, *a: broker.client(*a)):
            res = await self.uninstall(pub)
        self.assertEqual(res["retained_cleared"], len(OURS))
        self.assertNotIn("retained_cleanup_failed", res)
        self.assertEqual(broker.retained, KEPT)
        self.assertEqual(self.on_disk(), {})


class RetriedWhenTheBrokerIsBackTest(_Case):
    async def test_runs_once_with_nothing_installed_even_after_a_restart(self):
        await self.fail_uninstall()
        pub = self.publisher()  # a restart: nothing installed, no identity, no connection
        self.assertEqual({k[0] for k in pub._cleanup_pending}, {"hass_demo"})
        await pub._on_cleanup_timer(None)  # still unreachable
        self.assertEqual(set(self.on_disk()), {"hass_demo"})
        broker = _Broker({**OURS, **KEPT})
        with mock.patch.object(mp.MqttPublisher, "_throwaway_client", lambda self, *a: broker.client(*a)):
            await pub._on_cleanup_timer(None)
            await pub._on_cleanup_timer(None)
        self.assertEqual(broker.scans, [["homeassistant/device/+/config", "hass_demo/#"]])  # once, that identity only
        self.assertEqual(sorted(broker.cleared), sorted(OURS))
        self.assertEqual(broker.retained, KEPT)
        self.assertEqual(self.on_disk(), {})
        self.assertEqual(pub._cleanup_pending, {})
        self.assertTrue(any("cleared 4 retained topics" in c.args[1] and "hass_demo" in c.args[1] for c in self.emit.call_args_list))

    async def test_not_while_still_connected_under_it(self):
        pub, _res, _scan = await self.fail_uninstall()
        pub._live_base = "hass_demo"  # the uninstall's reconnect has not dropped the connection yet
        broker = _Broker({**OURS, **KEPT})
        with mock.patch.object(mp.MqttPublisher, "_throwaway_client", lambda self, *a: broker.client(*a)):
            await pub._on_cleanup_timer(None)
        self.assertEqual(broker.scans, [])
        self.assertEqual(set(self.on_disk()), {"hass_demo"})

    async def test_an_identity_sweep_that_clears_it_leaves_nothing_to_retry(self):
        await self.fail_uninstall()
        pub = self.publisher(running="hass_other")  # another integration started before the broker came back
        mp.write_json(os.path.join(self.dir, "integration_manager", "mqtt_identity.json"), {"base": "hass_demo", "prefix": "homeassistant"})
        broker = _Broker({**OURS, **KEPT})
        with mock.patch.object(mp.MqttPublisher, "_throwaway_client", lambda self, *a: broker.client(*a)):
            self.assertTrue(await pub.hass.async_add_executor_job(pub._sweep_old_identity, "hass_other"))
            await pub._on_cleanup_timer(None)
        self.assertEqual(len(broker.scans), 1)  # the timer found nothing left to do: no second clear
        self.assertEqual(broker.retained, KEPT)
        self.assertEqual(self.on_disk(), {})


class InstalledAgainBeforeTheBrokerIsBackTest(_Case):
    async def test_the_connect_of_that_identity_cancels_it(self):
        await self.fail_uninstall()
        pub = self.publisher(running="hass_demo", force_base_topic=True)
        pub._client, pub._connected, pub._probed_ok, pub.stats = None, False, set(), {}
        with mock.patch.object(mp.mqtt, "Client"):
            await pub.hass.async_add_executor_job(pub._connect)
        self.assertEqual(self.on_disk(), {})
        self.assertTrue(pub._orphan_sweep_due)  # what the removed copy announced and this one lacks goes with the orphan sweep
        self.assertIsNone(pub._boot_components)
        # stopped again later, the broker back: its documents are a stop's, and stay
        pub._key_provider, pub._live_base = (lambda: None), None
        broker = _Broker({**OURS, **KEPT})
        with mock.patch.object(mp.MqttPublisher, "_throwaway_client", lambda self, *a: broker.client(*a)):
            await pub._on_cleanup_timer(None)
        self.assertEqual(broker.scans, [])
        self.assertEqual(broker.retained, {**OURS, **KEPT})

    async def test_started_with_mqtt_off_the_timer_cancels_it(self):
        await self.fail_uninstall()
        pub = self.publisher(running="hass_demo", enabled=False)
        broker = _Broker({**OURS, **KEPT})
        with mock.patch.object(mp.MqttPublisher, "_throwaway_client", lambda self, *a: broker.client(*a)):
            await pub._on_cleanup_timer(None)
        self.assertEqual(broker.scans, [])
        self.assertEqual(self.on_disk(), {})

    async def test_installing_another_integration_keeps_it(self):
        await self.fail_uninstall()
        pub = self.publisher(running="hass_other", force_base_topic=True)
        pub._client, pub._connected, pub._probed_ok, pub.stats = None, False, set(), {}
        with mock.patch.object(mp.mqtt, "Client"):
            await pub.hass.async_add_executor_job(pub._connect)
        self.assertEqual(set(self.on_disk()), {"hass_demo"})


if __name__ == "__main__":
    unittest.main()
