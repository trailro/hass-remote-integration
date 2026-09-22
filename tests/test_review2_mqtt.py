"""Second external review, round two, MQTT side.

M-04  The main connection announced no Maximum Packet Size: paho reads any inbound packet whole before the
      CALL_MAX_BYTES check refuses it.  Only the main MQTT 5 client announces one; the scan clients must still see
      retained data of any size.
M-12  install_integration and install_home_assistant had no rate limit: a release whose smoke test fails is rolled
      back and stays the newest, so every press installs it again (a backup and a restart each time).
U-02  MqttPublisher was built on the event loop, reading mqtt.json and mqtt_rules.json there.
U-03  Target-less system services a dependency brings along (recorder.purge, logger.set_level, ...) were callable
      over MQTT: the published-entity check has no target to refuse.
"""

import asyncio
import os
import shutil
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import paho.mqtt.client as mqtt

import custom_components.integration_manager as im
from custom_components.integration_manager import manager_device as md
from custom_components.integration_manager import mqtt_publisher as mp
from custom_components.integration_manager import preflight
from tests import test_camp_publish as camp
from tests import test_r3_mqtt as r3
from tests import test_review_r2_core as core_boot
from tests.fakes import FakeInstaller, FakePublisher, FakeUpdater


# ----- M-04 --------------------------------------------------------------------------------------------------

class InboundPacketLimitTest(unittest.TestCase):
    def _pub(self):
        pub = camp._publisher(host="broker", force_base_topic=True)
        pub._client = None
        pub._connected = False
        pub._sweep_old_identity = lambda base: True
        pub._cleanup_pending = {}
        return pub

    def _connect_kwargs(self, pub):
        seen = []
        with mock.patch.object(mqtt.Client, "connect_async", lambda self_, *a, **kw: seen.append(kw)), \
                mock.patch.object(mqtt.Client, "loop_start", lambda self_: None):
            pub._connect()
        self.assertEqual(len(seen), 1, pub.stats.get("connect_error"))
        pub._client = None  # never started: nothing to stop
        return seen[0]

    def test_the_main_mqtt5_connection_announces_its_maximum(self):
        kwargs = self._connect_kwargs(self._pub())
        self.assertIn("properties", kwargs, "an inbound packet of any size is read whole before it is refused")
        self.assertEqual(kwargs["properties"].MaximumPacketSize, mp.INBOUND_MAX_PACKET)
        self.assertTrue(kwargs["clean_start"])

    def test_the_largest_accepted_call_still_fits(self):
        topic = f"{camp.BASE}/call/{'d' * 255}/{'s' * 255}"
        packet = 5 + 2 + len(topic) + 2 + 4 + mp.CALL_MAX_BYTES  # fixed header, topic, packet id, properties, payload
        self.assertLessEqual(packet, mp.INBOUND_MAX_PACKET)
        self.assertLess(mp.INBOUND_MAX_PACKET, 1024 * 1024)

    def test_mqtt_311_has_no_way_to_say_it(self):
        pub = self._pub()
        pub._broker_traits = lambda: (True, 0)
        self.assertNotIn("properties", self._connect_kwargs(pub))

    def test_the_scan_clients_announce_nothing(self):
        """A probe or sweep client with a maximum would never see a large retained document (mosquitto drops it
        for that client): a foreign one would let the base-topic probe pass."""
        pub = self._pub()
        seen = []
        with mock.patch.object(mqtt.Client, "connect", lambda self_, *a, **kw: seen.append(kw)), \
                mock.patch.object(mqtt.Client, "loop_start", lambda self_: None), \
                mock.patch.object(mp.MqttPublisher, "_stop_client", staticmethod(lambda c: None)):
            with self.assertRaises(RuntimeError):
                pub._throwaway_client("probe", "scan", time.monotonic())  # no CONNACK: refused after the connect
        self.assertEqual(len(seen), 1)
        self.assertNotIn("properties", seen[0])


# ----- M-12 --------------------------------------------------------------------------------------------------

class InstallRateLimitTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.written = []

        async def write(path, data, **kwargs):
            self.written.append((path, dict(data)))

        for patcher in (mock.patch.object(md.writer, "async_write", write),
                        mock.patch.object(preflight, "run", mock.AsyncMock(return_value={"ok": True, "blockers": []})),
                        mock.patch.object(preflight, "gate", mock.AsyncMock(return_value={"blocked": False, "report": None}))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _device(self, start_result):
        self.log = []
        inst = FakeInstaller(running="demo", running_tag="1.0.0", versions=("1.0.0", "2.0.0"), log=self.log)
        inst.start = mock.AsyncMock(return_value=start_result)
        dev = md.ManagerDevice(SimpleNamespace(), inst, FakeUpdater(), FakePublisher(log=self.log))
        dev._runs_file = os.path.join(self.dir, md.RUNS_FILE)
        return dev

    async def test_an_install_that_ran_limits_the_next_press(self):
        dev = self._device({"ok": True, "restart_required": False})
        self.assertTrue((await dev.async_action("install_integration"))["ok"])
        second = await dev.async_action("install_integration")
        self.assertFalse(second["ok"], "a rolled-back release is installed again at every press")
        self.assertIn("ran moments ago", second["error"])
        self.assertEqual(dev.installer.start.await_count, 1)
        self.assertIn("install_integration", self.written[-1][1])  # on disk: the restart it asks for keeps the limit

    async def test_a_start_whose_requirements_failed_ran_too(self):
        dev = self._device({"ok": False, "error": "pip failed for: x; demo stays on 1.0.0", "pip_failed": ["x"]})
        first = await dev.async_action("install_integration")
        self.assertFalse(first["ok"])
        self.assertIn("pip failed", first["error"])
        second = await dev.async_action("install_integration")
        self.assertIn("ran moments ago", second["error"], "every press takes a backup")
        self.assertEqual(dev.installer.start.await_count, 1)

    async def test_a_refused_start_does_not_spend_the_limit(self):
        dev = self._device({"ok": False, "error": "another action is running"})
        self.assertIn("another action is running", (await dev.async_action("install_integration"))["error"])
        self.assertEqual(dev._limit_wait("install_integration"), 0.0)
        self.assertEqual(self.written, [])
        await dev.async_action("install_integration")
        self.assertEqual(dev.installer.start.await_count, 2)

    async def test_a_home_assistant_upgrade_is_limited_and_its_restart_is_not_blocked(self):
        dev = self._device({"ok": True})
        dev._do_install_home_assistant = mock.AsyncMock(return_value={"ok": True, "note": "Home Assistant x", "restart": True})
        first = await dev.async_action("install_home_assistant")
        self.assertTrue(first["ok"], first)
        self.assertIn(("restart",), self.log)  # the install's own restart is not the restart action
        second = await dev.async_action("install_home_assistant")
        self.assertIn("ran moments ago", second["error"])
        self.assertEqual(dev._do_install_home_assistant.await_count, 1)
        self.assertEqual(dev._limit_wait("restart"), 0.0)


# ----- U-02 --------------------------------------------------------------------------------------------------

class PublisherBuiltOffTheLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_publisher_reads_its_files_in_the_executor(self):
        boot = core_boot._Boot(self)
        loop_thread = threading.get_ident()
        built_on = []

        def make_publisher(*args, **kwargs):
            built_on.append(threading.get_ident())
            return core_boot.FakePublisher(*args, **kwargs)

        with mock.patch.object(im, "MqttPublisher", make_publisher), mock.patch.object(im, "Installer", boot._installer):
            self.assertTrue(await im.async_setup(boot.hass, {}))
        self.assertEqual(len(built_on), 1)
        self.assertNotEqual(built_on[0], loop_thread, "mqtt.json and mqtt_rules.json are opened on the event loop")
        boot.gate.set()

    def test_the_real_constructor_runs_off_the_loop(self):
        """Nothing in it needs a running loop (asyncio.Lock binds to one on first use, since Python 3.10)."""
        with tempfile.TemporaryDirectory() as d:
            hass = mock.Mock()
            hass.config.path = lambda *p: os.path.join(d, *p)
            box = []
            t = threading.Thread(target=lambda: box.append(mp.MqttPublisher(hass)))
            t.start()
            t.join(10)
            self.assertEqual(len(box), 1)
            self.assertFalse(box[0].config.enabled)

            async def lock_on_loop():
                async with box[0]._conn_lock:
                    return True

            self.assertTrue(asyncio.run(lock_on_loop()))


# ----- U-03 --------------------------------------------------------------------------------------------------

class SystemServiceDenyTest(unittest.TestCase):
    DENIED = (("recorder", "purge"), ("recorder", "disable"), ("logger", "set_level"), ("system_log", "write"),
              ("system_log", "clear"), ("backup", "create"), ("conversation", "process"))

    def test_target_less_system_services_are_refused_over_mqtt(self):
        for domain, service in self.DENIED:
            with self.subTest(service=f"{domain}.{service}"):
                pub = r3._publisher()
                scheduled = []
                pub.hass.loop.call_soon_threadsafe = scheduled.append
                pub._on_call(f"{domain}/{service}", '{"text": "turn off everything"}')
                self.assertEqual(scheduled, [], "the service would run")
                self.assertEqual(len(pub.results), 1)
                self.assertIn("not callable over MQTT", pub.results[0][2]["error"])

    def test_the_operators_services_page_keeps_them(self):
        for domain, _service in self.DENIED:
            self.assertNotIn(domain, mp.CALL_DENY_DOMAINS)

    def test_entity_services_stay_callable(self):
        pub = r3._publisher()
        pub._topics = {"light.published": "t"}
        scheduled = []
        pub.hass.loop.call_soon_threadsafe = scheduled.append
        pub._on_call("light/turn_on", '{"entity_id": "light.published"}')
        self.assertEqual(len(scheduled), 1)


if __name__ == "__main__":
    unittest.main()
