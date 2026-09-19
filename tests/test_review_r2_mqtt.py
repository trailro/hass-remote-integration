"""Second external review: the MQTT publisher, the manager device and the health watchdog.

R2-08  our own fix of last round armed every session: on MQTT 5 (noLocal, so the echo never comes) the
       topic stayed armed and swallowed the next genuine empty command on a text or notify entity
R2-10  a manager action refused by what it called still started its rate limit, on disk
R2-11  the watchdog could restart the process in the middle of a preflight (no manager/result ever)
R2-12  the identity sweep ran against the broker the settings name now, not the one the data went to
R2-13  the watchdog's ledger and its minimum interval trusted timestamps from a clock that was ahead,
       and a number JSON can spell but int() cannot convert raised out of two readers

Every test here fails on the tree before the fix.
"""

import asyncio
import collections
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from custom_components.integration_manager import discovery as disc
from custom_components.integration_manager import manager_device as md
from custom_components.integration_manager import mqtt_publisher as mp
from custom_components.integration_manager import preflight
from custom_components.integration_manager.installer import Installer
from custom_components.integration_manager.scheduler import WATCHDOG_BOOT_GRACE_S, Scheduler
from tests.fakes import FakeInstaller, FakePublisher, FakeUpdater

BASE = "hass_r2"


# ----- R2-08: the cleared-echo window on an MQTT 5 session ------------------------------------------

class FakeClient:
    protocol = mqtt.MQTTv311

    def __init__(self, protocol=mqtt.MQTTv311):
        self.protocol = protocol
        self.published = []
        self.max_inflight_messages = 20

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        return SimpleNamespace(rc=0)

    def subscribe(self, topics):
        return mqtt.MQTT_ERR_SUCCESS, 1


class Msg:
    def __init__(self, topic, payload, retain=False):
        self.topic, self.payload, self.retain = topic, payload.encode(), retain


def _publisher(loop, calls, protocol=mqtt.MQTTv311):
    """A publisher built field by field: no HA instance and no broker, as the other MQTT tests do."""
    pub = object.__new__(mp.MqttPublisher)
    pub.hass = mock.Mock()
    pub.hass.data = {}
    pub.hass.loop = loop
    pub.hass.async_create_task = loop.create_task

    async def async_call(domain, service, data, blocking=True, return_response=False):
        calls.append((domain, service, dict(data)))

    pub.hass.services.async_call = async_call
    pub.config = mp.MqttConfig(qos=1)
    pub.history = collections.deque(maxlen=mp.HISTORY_MAX)
    pub._calls = {}
    pub._calls_lock = threading.Lock()
    pub._in_flight = 0
    pub._cleared_cmds = {}
    pub._subscribing = None
    pub._subscribing_lock = threading.Lock()
    pub.stats = {"calls": 0, "commands": 0, "last_call": None, "last_command": None, "published": 0, "cleared": 0,
                 "unchanged_skipped": 0, "oversized_skipped": 0, "last_oversized": None, "services_published": 0,
                 "connected": False, "connect_error": "", "subscribe_error": "", "protocol": None}
    pub._client = FakeClient(protocol)
    pub._connected = True
    pub._connected_at = 0.0
    pub._broker_max_packet = 0
    pub._oversized_warned = set()
    pub._last_hash = {}
    pub._last_disconnect = ""
    pub._last_subscribe_error = ""
    pub._manager_absent_sent = True
    pub._moving = False
    pub._stopping = False
    pub._live_base = BASE
    pub._live_prefix = BASE + "_"
    pub._key_provider = lambda: BASE
    pub._tls_checked_at, pub._tls_error = 0.0, ""
    pub._topics = {"text.note": f"{BASE}/x/text/note"}
    pub._range_pending = {}
    pub._pending_clears = set()  # a set in MqttPublisher.__init__, and .add()ed to: a dict here would not carry a clear
    pub._services_published = set()
    pub.hass.states.get.return_value = mock.Mock(attributes={})
    pub.async_republish_all = lambda: asyncio.sleep(0)  # a CONNACK starts one: not what these tests are about
    return pub


class ClearedEchoOnMqtt5Test(unittest.TestCase):
    """R2-08: the window that drops the echo of our own clear belongs to a session without noLocal."""

    def _run(self, protocol, connect=True):
        async def run():
            calls = []
            pub = _publisher(asyncio.get_running_loop(), calls, protocol)
            if connect:
                pub._on_connect(pub._client, None, None, 0, None)
            topic = f"{BASE}/cmd/text/note/value"
            pub._handle_message(Msg(topic, "hello", retain=True))  # refused and cleared
            pub._handle_message(Msg(topic, ""))  # the main HA blanking the text entity for real
            for _ in range(6):
                await asyncio.sleep(0)
            return pub, calls
        return asyncio.run(run())

    def test_a_real_empty_text_command_survives_a_clear_on_mqtt5(self):
        pub, calls = self._run(mqtt.MQTTv5)
        self.assertEqual(pub.stats["protocol"], "MQTT 5")
        self.assertEqual(calls, [("text", "set_value", {"entity_id": "text.note", "value": ""})])
        self.assertEqual(pub._cleared_cmds, {})  # nothing was armed: the echo cannot arrive

    def test_the_echo_is_still_dropped_on_mqtt_311(self):
        pub, calls = self._run(mqtt.MQTTv311)
        self.assertEqual(pub.stats["protocol"], "MQTT 3.1.1")
        self.assertEqual(calls, [])
        self.assertEqual(list(pub.history), [])

    def test_a_session_is_mqtt_311_until_a_connack_says_otherwise(self):
        """No CONNACK seen (a test, a client built before the connect): the safe side is to remember."""
        pub, calls = self._run(mqtt.MQTTv5, connect=False)
        self.assertEqual(calls, [])


# ----- R2-10: a refused manager action must not start its rate limit ---------------------------------

def _device(tmp):
    log = []
    dev = md.ManagerDevice(SimpleNamespace(), FakeInstaller(log=log), FakeUpdater(), FakePublisher(log=log))
    dev._runs_file = os.path.join(tmp, "manager_actions.json")
    return dev


class RefusedActionRateLimitTest(unittest.IsolatedAsyncioTestCase):
    """R2-10: the limit was stamped (and saved) before the action ran, so a refusal spent it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.written = []
        patcher = mock.patch.object(md.writer, "async_write", self._write)
        patcher.start()
        self.addCleanup(patcher.stop)
        quiet = logging.getLogger("custom_components.integration_manager")
        was = quiet.level
        quiet.setLevel(logging.CRITICAL)
        self.addCleanup(quiet.setLevel, was)

    async def _write(self, path, data, **kwargs):
        self.written.append((path, dict(data)))

    async def test_a_backup_refused_by_the_installer_does_not_spend_the_limit(self):
        dev = _device(self.dir)

        async def busy():  # exactly what installer.async_backup_exclusive raises
            raise ValueError("an install, start or restore is running: try again in a moment")

        dev._do_backup = busy
        first = await dev.async_action("backup")
        self.assertFalse(first["ok"])
        self.assertIn("try again in a moment", first["error"])
        self.assertEqual(dev._last_run, {})
        self.assertEqual(dev._limit_wait("backup"), 0.0)
        self.assertEqual(self.written, [])  # and nothing was saved for a restart to inherit

        taken = []

        async def ok():
            taken.append(1)
            return {"ok": True, "note": "backup x"}

        dev._do_backup = ok
        second = await dev.async_action("backup")
        self.assertTrue(second["ok"], second)
        self.assertEqual(taken, [1])

    async def test_a_backup_that_ran_still_starts_the_limit_and_is_saved(self):
        dev = _device(self.dir)

        async def ok():
            return {"ok": True, "note": "backup x"}

        dev._do_backup = ok
        self.assertTrue((await dev.async_action("backup"))["ok"])
        self.assertIn("backup", dev._last_run)
        self.assertEqual(self.written, [(dev._runs_file, dev._last_run)])
        refused = await dev.async_action("backup")
        self.assertFalse(refused["ok"])
        self.assertIn("ran moments ago", refused["error"])

    async def test_a_check_for_updates_that_failed_may_be_tried_again(self):
        dev = _device(self.dir)

        async def down():
            raise OSError("github is unreachable")

        dev._do_check_updates = down
        self.assertFalse((await dev.async_action("check_updates"))["ok"])
        self.assertEqual(dev._limit_wait("check_updates"), 0.0)


# ----- R2-11: the watchdog and the rest of the manager ------------------------------------------------

class WatchdogLiveRefusalTest(unittest.IsolatedAsyncioTestCase):
    """R2-11: a preflight takes minutes and sets no `busy`; a restart in the middle of one loses the
    requested start and never answers the MQTT command that asked for it."""

    def scheduler(self, manager=None):
        inst = SimpleNamespace(state=SimpleNamespace(domain="demo", pending_smoke=None, pending_start=None, pending_rollback=None),
                               busy=False, smoke={}, manager=manager, _entries_of=lambda _domain: [])
        sch = Scheduler.__new__(Scheduler)
        sch.hass = SimpleNamespace(is_running=True)
        sch.installer = inst
        sch._started = time.monotonic() - WATCHDOG_BOOT_GRACE_S - 1
        return sch

    def test_nothing_running_is_no_refusal(self):
        self.assertIsNone(self.scheduler()._live_refusal())

    async def test_an_integration_preflight_holds_the_watchdog_off(self):
        sch = self.scheduler()
        async with preflight.LOCK:
            self.assertIn("preflight", sch._live_refusal() or "")

    async def test_a_home_assistant_preflight_holds_the_watchdog_off(self):
        sch = self.scheduler()
        async with preflight._HA_LOCK:
            self.assertIn("preflight", sch._live_refusal() or "")

    async def test_a_manager_action_holds_the_watchdog_off(self):
        dev = md.ManagerDevice(SimpleNamespace(), FakeInstaller(), FakeUpdater(), FakePublisher())
        sch = self.scheduler(manager=dev)
        async with dev._action_lock:
            dev._running, dev._running_since = "install_integration", time.monotonic()
            refusal = sch._live_refusal()
        self.assertIn("install_integration", refusal or "")
        self.assertIsNone(sch._live_refusal())  # and it is over when the action is

    async def test_a_manager_action_hung_past_its_maximum_no_longer_holds_it_off(self):
        """The watchdog exists for a process that is stuck: an action that has held the lock for half an
        hour is exactly that, and `_action_held` already says so."""
        dev = md.ManagerDevice(SimpleNamespace(), FakeInstaller(), FakeUpdater(), FakePublisher())
        sch = self.scheduler(manager=dev)
        async with dev._action_lock:
            dev._running, dev._running_since = "backup", time.monotonic() - md.ACTION_MAX_S - 1
            self.assertIsNone(sch._live_refusal())

    def test_the_manager_device_introduces_itself_to_the_installer(self):
        inst = FakeInstaller()
        dev = md.ManagerDevice(SimpleNamespace(), inst, FakeUpdater(), FakePublisher())
        self.assertIs(inst.manager, dev)
        self.assertIsNone(Installer.manager)  # and an installer without one answers, it does not raise


# ----- R2-12: the identity sweep and the broker the data went to --------------------------------------

def _closed_port() -> int:
    import socket

    s = socket.create_server(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Broker:
    """The retained store reached by a throwaway client, as in test_r13_mqtt.py."""

    def __init__(self, retained):
        self.retained = dict(retained)
        self.scans = []
        self.cleared = []

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


OLD = {
    "hass_demo/status": b"offline",
    "hass_demo/health": json.dumps({"updated_at": "t", "base_topic": "hass_demo"}).encode(),
    "homeassistant/device/hass_demo_dev/config": json.dumps(
        {"device": {"identifiers": ["hass_demo"]}, "origin": disc.origin("hass_demo_"), "components": {}}).encode(),
}


class IdentitySweepBrokerTest(unittest.IsolatedAsyncioTestCase):
    """R2-12: the names recorded last name a broker too, and only that broker holds what they left behind."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "integration_manager"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.port = _closed_port()
        self.emit = mock.patch.object(mp.events, "emit").start()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(mp.MqttPublisher, "_collect_quiet", staticmethod(lambda *a, **k: None)).start()
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def publisher(self, running=None, host="127.0.0.1", port=None):
        pub = object.__new__(mp.MqttPublisher)
        pub.config = mp.MqttConfig(enabled=True, host=host, port=self.port if port is None else port)
        loop = asyncio.get_running_loop()
        pub.hass = SimpleNamespace(config=SimpleNamespace(path=lambda *p: os.path.join(self.dir, *p)),
                                   async_add_executor_job=lambda f, *a: loop.run_in_executor(None, f, *a))
        pub._key_provider = lambda: running
        pub._live_base = pub._live_prefix = None
        pub._conn_lock = asyncio.Lock()
        pub._topics, pub._last_hash, pub._discovery_map, pub._blocks = {}, {}, {}, {}
        pub._cleanup_pending = pub._read_cleanup_pending()
        return pub

    def record(self, **over):
        doc = {"base": "hass_demo", "prefix": "homeassistant",
               "broker": {"host": "10.9.9.9", "port": 1883, "tls": False, "username": ""}}
        doc.update(over)
        mp.write_json(os.path.join(self.dir, "integration_manager", "mqtt_identity.json"), doc)

    def identity_on_disk(self):
        with open(os.path.join(self.dir, "integration_manager", "mqtt_identity.json"), encoding="utf-8") as fh:
            return json.load(fh)

    async def sweep(self, pub, base, broker):
        with mock.patch.object(mp.MqttPublisher, "_throwaway_client", lambda self, *a, **k: broker.client(*a, **k)):
            return await pub.hass.async_add_executor_job(pub._sweep_old_identity, base)

    async def test_the_old_brokers_topics_are_not_looked_for_on_this_one(self):
        self.record()
        pub = self.publisher(running="hass_other")
        broker = _Broker(dict(OLD))
        self.assertTrue(await self.sweep(pub, "hass_other", broker))
        self.assertEqual(broker.scans, [])  # this broker was never asked about another broker's identity
        self.assertEqual(broker.cleared, [])

    async def test_the_removal_is_recorded_as_pending_on_the_broker_that_has_it(self):
        self.record()
        pub = self.publisher(running="hass_other")
        await self.sweep(pub, "hass_other", _Broker(dict(OLD)))
        pending = pub.retained_cleanup_pending()
        self.assertEqual([(p["base_topic"], p["broker"], p["other_broker"]) for p in pending],
                         [("hass_demo", "10.9.9.9:1883", True)])
        self.assertIn("10.9.9.9:1883", pending[0]["error"])
        with open(os.path.join(self.dir, "integration_manager", "mqtt_cleanup_pending.json"), encoding="utf-8") as fh:
            self.assertEqual([r["base"] for r in json.load(fh)["pending"]], ["hass_demo"])

    async def test_the_same_identity_on_another_broker_is_pending_too(self):
        """Nothing about the names changed: the data is simply on a broker this process no longer talks to."""
        self.record()
        pub = self.publisher(running="hass_demo")
        await self.sweep(pub, "hass_demo", _Broker(dict(OLD)))
        self.assertEqual([p["base_topic"] for p in pub.retained_cleanup_pending()], ["hass_demo"])

    async def test_the_new_names_and_broker_are_recorded(self):
        self.record()
        pub = self.publisher(running="hass_other")
        await self.sweep(pub, "hass_other", _Broker(dict(OLD)))
        doc = self.identity_on_disk()
        self.assertEqual(doc["base"], "hass_other")
        self.assertEqual(doc["broker"]["host"], "127.0.0.1")

    async def test_the_same_broker_still_sweeps_as_it_did(self):
        self.record(broker={"host": "127.0.0.1", "port": self.port, "tls": False, "username": ""})
        pub = self.publisher(running="hass_other")
        broker = _Broker(dict(OLD))
        self.assertTrue(await self.sweep(pub, "hass_other", broker))
        self.assertEqual(broker.cleared and sorted(broker.cleared), sorted(OLD))
        self.assertEqual(pub.retained_cleanup_pending(), [])

    async def test_the_removal_runs_when_the_settings_name_that_broker_again(self):
        """The record the sweep leaves is the one the retry timer already knows how to act on."""
        self.record()
        await self.sweep(self.publisher(running="hass_other"), "hass_other", _Broker(dict(OLD)))
        back = self.publisher(running="hass_other", host="10.9.9.9", port=1883)
        broker = _Broker(dict(OLD))
        with mock.patch.object(mp.MqttPublisher, "_throwaway_client", lambda self, *a, **k: broker.client(*a, **k)):
            await back._on_cleanup_timer(None)
        self.assertEqual(sorted(broker.cleared), sorted(OLD))
        self.assertEqual(back.retained_cleanup_pending(), [])

    async def test_a_record_from_before_brokers_were_named_is_this_broker(self):
        self.record(broker=None)
        pub = self.publisher(running="hass_other")
        broker = _Broker(dict(OLD))
        self.assertTrue(await self.sweep(pub, "hass_other", broker))
        self.assertEqual(sorted(broker.cleared), sorted(OLD))
        self.assertEqual(pub.retained_cleanup_pending(), [])


# ----- R2-13: the watchdog ledger and a clock that was ahead -------------------------------------------

def _installer(tmp, watchdog=None, **settings):
    inst = Installer.__new__(Installer)
    inst.hass = SimpleNamespace(data={})
    inst.config_dir = tmp
    inst.state_dir = os.path.join(tmp, "integration_manager")
    os.makedirs(inst.state_dir, exist_ok=True)
    inst.state = SimpleNamespace(watchdog=watchdog)
    inst.settings = SimpleNamespace(watchdog=lambda: {"enabled": True, "after_min": 15, "min_interval_min": 30,
                                                     "max_per_day": 3, **settings})
    inst._save_state = lambda: None
    return inst


class WatchdogClockTest(unittest.TestCase):
    """R2-13: a restart stamped by a clock that was ahead blocked every automatic restart until real time
    caught up; ManagerDevice._limit_wait has clamped exactly this for as long as it has existed."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_a_restart_stamped_far_in_the_future_limits_nothing(self):
        ahead = time.time() + 30 * 86400  # the clock was a month ahead; NTP has corrected it since
        inst = _installer(self.dir, {"restarts": [ahead], "attempts": 0})
        self.assertIsNone(inst.watchdog_cap_refusal())

    def test_a_restart_stamped_far_in_the_future_is_not_one_of_the_days_restarts(self):
        ahead = time.time() + 30 * 86400
        inst = _installer(self.dir, {"restarts": [ahead, ahead + 60, ahead + 120], "attempts": 0})
        self.assertEqual(inst.watchdog_record()["restarts"], [])
        self.assertIsNone(inst.watchdog_cap_refusal())

    def test_the_wait_never_exceeds_the_interval(self):
        soon = time.time() + 300  # a clock a few minutes ahead: a real restart, badly stamped
        inst = _installer(self.dir, {"restarts": [soon], "attempts": 0})
        refusal = inst.watchdog_cap_refusal()
        self.assertIsNotNone(refusal)
        minutes = int(refusal[0].split("(")[1].split(" min")[0])
        self.assertLessEqual(minutes, 31)  # the interval, not 43261

    def test_a_real_recent_restart_still_waits(self):
        inst = _installer(self.dir, {"restarts": [time.time() - 60], "attempts": 0})
        refusal = inst.watchdog_cap_refusal()
        self.assertIsNotNone(refusal)
        self.assertIn("less than 30 min ago", refusal[0])
        self.assertFalse(refusal[1])

    def test_the_daily_maximum_still_gives_up(self):
        now = time.time()
        inst = _installer(self.dir, {"restarts": [now - 3600, now - 7200, now - 10800], "attempts": 0})
        refusal = inst.watchdog_cap_refusal()
        self.assertTrue(refusal[1])
        self.assertIn("maximum", refusal[0])


class UnconvertibleNumberTest(unittest.TestCase):
    """R2-13's neighbours: json.load spells Infinity and NaN, and int() raises OverflowError on the first."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_attempts_that_cannot_be_converted_read_as_none(self):
        for value in (float("inf"), float("nan"), "seven", None):
            inst = _installer(self.dir, {"restarts": [], "attempts": value})
            self.assertEqual(inst.watchdog_record()["attempts"], 0, value)

    def test_a_boot_failure_count_that_cannot_be_converted_is_left_alone(self):
        inst = _installer(self.dir)
        path = os.path.join(inst.state_dir, "ha.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"boot_failures": Infinity, "other": 1}')
        inst._undo_boot_failure()  # must not raise
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), '{"boot_failures": Infinity, "other": 1}')

    def test_a_real_boot_failure_count_is_still_taken_back(self):
        inst = _installer(self.dir)
        path = os.path.join(inst.state_dir, "ha.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"boot_failures": 2}, fh)
        inst._undo_boot_failure()
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["boot_failures"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
