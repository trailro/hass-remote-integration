"""Ninth review, MQTT side: MQTT 5 with a 3.1.1 fallback (the broker's maximum packet size and receive maximum, a
retained command refused at once), the republish count, the last command cut and masked, secrets behind escaped
or nested keys, file I/O off the loop, the cutover preconditions, force and Undo, orphans with discovery off, and
the unconfirmed concerns (call memory across threads, exclusions adopted by a reload, ca_certs from disk, the
manager result during an identity move)."""

import asyncio
import collections
import json
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import paho.mqtt.client as mqtt
from paho.mqtt.reasoncodes import ReasonCode
from paho.mqtt.packettypes import PacketTypes

from custom_components.integration_manager import events, parity, writer
from custom_components.integration_manager import mqtt_publisher as mp
from custom_components.integration_manager import mqtt_rules
from tests import test_camp_publish as camp
from tests import test_view_handlers as views

BASE = camp.BASE


def _unsupported():
    return ReasonCode(PacketTypes.CONNACK, "Unsupported protocol version")


class ProtocolTest(unittest.TestCase):
    """F10: only MQTT 5 lets the broker announce its maximum packet size; a 3.1.1 broker still gets a client."""

    def _pub(self, **config):
        pub = camp._publisher(**config)
        pub.hass.async_create_background_task = lambda coro, name: coro.close()
        return pub

    def test_clients_speak_mqtt5_with_a_clean_start(self):
        pub = self._pub()
        c = pub._new_client("x")
        self.assertEqual(c.protocol, mqtt.MQTTv5)
        self.assertEqual(pub._connect_options(c), {"clean_start": True})
        self.assertEqual(c.max_inflight_messages, mp.PAHO_INFLIGHT)

    def test_a_refusal_of_mqtt5_falls_back_to_311_for_that_broker_only(self):
        pub = self._pub(host="old-broker")
        replaced = []
        pub._replace_client_soon = replaced.append
        client = pub._new_client(BASE)
        pub._client = client
        with self.assertLogs(mp._LOGGER, "WARNING"), mock.patch.object(mp.events, "emit"):
            pub._on_connect(client, None, None, _unsupported())
        self.assertEqual(replaced, [client])
        self.assertIn("MQTT 3.1.1", pub.stats["connect_error"])
        c = pub._new_client("y")
        self.assertEqual(c.protocol, mqtt.MQTTv311)
        self.assertEqual(pub._connect_options(c), {})
        pub.config.host = "other-broker"  # another broker may speak MQTT 5
        self.assertEqual(pub._new_client("z").protocol, mqtt.MQTTv5)

    def test_a_small_receive_maximum_replaces_the_client_before_anything_is_sent(self):
        pub = self._pub()
        pub._connected = False
        replaced = []
        pub._replace_client_soon = replaced.append
        client = pub._new_client(BASE)
        client.publish = mock.Mock()
        client.subscribe = mock.Mock(return_value=(0, 1))
        pub._client = client  # _connect records it before paho's thread starts, and the callbacks answer for it
        pub._on_connect(client, None, None, 0, SimpleNamespace(ReceiveMaximum=5))
        self.assertEqual(replaced, [client])
        client.publish.assert_not_called()
        client.subscribe.assert_not_called()
        self.assertFalse(pub._connected)
        again = pub._new_client(BASE)
        self.assertEqual(again.max_inflight_messages, 5)
        again.publish, again.subscribe = mock.Mock(), mock.Mock(return_value=(0, 1))
        pub._client = again  # the replacement is the current one now
        pub._on_connect(again, None, None, 0, SimpleNamespace(ReceiveMaximum=5))  # the window fits now
        self.assertEqual(replaced, [client])
        self.assertTrue(pub._connected)

    def test_subscriptions_keep_the_retain_flag_and_skip_our_own_messages(self):
        pub = self._pub()
        client = pub._new_client(BASE)
        client.publish, client.subscribe = mock.Mock(), mock.Mock(return_value=(0, 1))
        pub._client = client  # _connect records it before paho's thread starts, and the callbacks answer for it
        pub._on_connect(client, None, None, 0, SimpleNamespace())
        (topics,), _ = client.subscribe.call_args
        self.assertEqual({t for t, _o in topics}, {f"{BASE}/cmd/#", f"{BASE}/call/#", f"{BASE}/manager/cmd/+"})
        for _t, options in topics:
            self.assertTrue(options.retainAsPublished)
            self.assertTrue(options.noLocal)
            self.assertEqual(options.QoS, 1)
        self.assertEqual(pub.stats["protocol"], "MQTT 5")

    def test_the_announced_maximum_packet_size_is_used(self):
        pub = self._pub()
        client = pub._new_client(BASE)
        client.publish, client.subscribe = mock.Mock(), mock.Mock(return_value=(0, 1))
        pub._client = client  # _connect records it before paho's thread starts, and the callbacks answer for it
        pub._on_connect(client, None, None, 0, SimpleNamespace(MaximumPacketSize=2048))
        pub._client = camp.FakeClient()
        self.assertFalse(pub._publish(f"{BASE}/demo/sensor/big", "x" * 3000))
        self.assertEqual(pub._client.published, [])

    def test_an_empty_retained_message_is_not_cleared_again(self):
        """With retainAsPublished the clear of a retained command arrives flagged: clearing it again would echo forever
        from any other client that clears it."""
        pub = self._pub()
        pub._handle_message(SimpleNamespace(topic=f"{BASE}/cmd/switch/a/state", payload=b"", retain=True))
        self.assertEqual(pub._client.published, [])

    def test_a_retained_command_is_still_refused_and_cleared(self):
        pub = self._pub()
        with self.assertLogs(mp._LOGGER, "WARNING"):
            pub._handle_message(SimpleNamespace(topic=f"{BASE}/cmd/switch/a/state", payload=b"ON", retain=True))
        self.assertEqual(pub._client.published, [(f"{BASE}/cmd/switch/a/state", "", 1, True)])

    def test_a_disconnect_sent_by_the_broker_is_reported(self):
        """paho 2.1 turns a bare MQTT 5 DISCONNECT (reason code only, as mosquitto sends "packet too large") into a
        normal disconnection."""
        pub = self._pub()
        pub._on_connect(pub._client, None, {}, 0)
        normal = ReasonCode(PacketTypes.DISCONNECT, "Normal disconnection")
        with self.assertLogs(mp._LOGGER, "WARNING"), mock.patch.object(mp.events, "emit"):
            pub._on_disconnect(pub._client, None, SimpleNamespace(is_disconnect_packet_from_server=True), normal)
        self.assertIn("closed the connection", pub.stats["connect_error"])
        pub.stats["connect_error"] = ""
        pub._on_disconnect(pub._client, None, SimpleNamespace(is_disconnect_packet_from_server=False), normal)
        self.assertEqual(pub.stats["connect_error"], "")  # our own disconnect

    def test_throwaway_clients_fall_back_too(self):
        pub = self._pub()
        pub._live_base = BASE
        built = []

        def client_factory(*_a, **kwargs):
            c = mock.Mock()
            c.protocol = kwargs.get("protocol")
            c.max_inflight_messages = 20
            refused = len(built) == 0
            c.connect.side_effect = lambda *a, **k: c.on_connect(c, None, None, _unsupported() if refused else 0, None)
            c.subscribe.side_effect = lambda topics: c.on_subscribe(c, None, 1, [ReasonCode(PacketTypes.SUBACK, identifier=1)])
            c.publish.return_value.is_published.return_value = True
            built.append((kwargs, c))
            return c

        with mock.patch.object(mp.mqtt, "Client", side_effect=client_factory), mock.patch.object(mp.MqttPublisher, "_collect_quiet"), \
                self.assertLogs(mp._LOGGER, "WARNING"), mock.patch.object(mp.events, "emit"):
            self.assertEqual(pub._retained_scan("probe", [("x/#", 1)]), {})
            pub._clear_topics("cleanup", ["x/y"])
        self.assertEqual([k.get("protocol") for k, _c in built], [mqtt.MQTTv5, mqtt.MQTTv311, mqtt.MQTTv311])
        self.assertEqual(built[0][1].connect.call_args.kwargs, {"keepalive": 30, "clean_start": True})
        self.assertEqual(built[1][1].connect.call_args.kwargs, {"keepalive": 30})
        built[0][1].subscribe.assert_not_called()  # the refused client never got to do anything
        built[2][1].publish.assert_called_once_with("x/y", "", qos=1, retain=True)


class RepublishCountTest(unittest.IsolatedAsyncioTestCase):
    """F19: the entities republished, not the discovery configs a pending cleanup removed."""

    async def test_count_is_entities(self):
        pub = camp._publisher()
        pub.stats.update(discovery_devices=0, discovery_components=0)
        pub._pending_clears, pub._orphan_sweep_due, pub._resync_excluded = set(), False, False
        pub._identity_sweep_due, pub._undiscover_due = False, True
        pub._discovery_map, pub._blocks = {}, {}
        pub.hass.states.async_all.return_value = [object(), object(), object()]
        pub._publish_state = lambda state, force=True: True
        pub.publish_health = pub._publish_manager_discovery = pub.publish_manager = lambda: None
        pub._publish_services = mock.AsyncMock()
        pub._set_undiscover_due = lambda due: None

        async def executor(fn, *args):
            return fn(*args)

        pub.hass.async_add_executor_job = executor
        pub._clear_discovery_retained = lambda: 99
        self.assertEqual(await pub.async_republish_all(), 3)
        self.assertEqual(pub.stats["entities_last_run"], 3)


class LastCommandTest(unittest.TestCase):
    """F20."""

    def test_cut_like_its_siblings_and_masked(self):
        pub = camp._publisher()
        pub.stats["commands"] = 0
        pub._topics = {"text.note": "t"}
        payload = '{"code": "1234", "value": "' + "x" * 200_000 + '"}'
        pub._handle_message(SimpleNamespace(topic=f"{BASE}/cmd/text/note/value", payload=payload.encode(), retain=False))
        self.assertLessEqual(len(pub.stats["last_command"]), 140)
        self.assertNotIn("1234", pub.stats["last_command"])


class NestedSecretTest(unittest.TestCase):
    """F21: a key written with escapes, or inside a JSON string value, is still a key."""

    def test_escaped_key_name(self):
        self.assertNotIn("1234", mp._mask_codes('{"\\u0063ode": "1234"}'))

    def test_json_inside_a_string_value(self):
        text = json.dumps({"entity_id": "script.x", "params": json.dumps({"code": "1234", "mode": "away"})})
        masked = mp._mask_codes(text)
        self.assertNotIn("1234", masked)
        self.assertIn("away", masked)
        self.assertEqual(json.loads(json.loads(masked)["params"])["code"], "***")

    def test_twice_nested_and_in_lists(self):
        inner = json.dumps({"password": "hunter2"})
        text = json.dumps({"items": [{"data": json.dumps({"nested": inner})}]})
        self.assertNotIn("hunter2", mp._mask_codes(text))

    def test_escaped_quotes_in_text_that_is_not_json(self):
        masked = mp._mask_codes('script.x {"params": "{\\"code\\": \\"1234\\"}" cut her')
        self.assertNotIn("1234", masked)
        self.assertIn('\\"code\\": \\"***\\"', masked)

    def test_unchanged_json_keeps_its_text(self):
        for kept in ('{"a":1,"b":"Café"}', '[1, 2]', '{"message": "no secrets"}'):
            self.assertEqual(mp._mask_codes(kept), kept)

    def test_history_row_masks_a_long_payload_before_cutting_it(self):
        pub = camp._publisher()
        payload = json.dumps({"params": json.dumps({"code": "1234"}), "filler": "x" * 2000})
        rec = pub._remember("call", "script.turn_on", payload)
        self.assertNotIn("1234", rec["data"])
        rec = pub._remember("call", "script.turn_on", '{"\\u0063ode": "4321", "x": "' + "y" * 1500 + '"}')
        self.assertNotIn("4321", rec["data"])


class UndiscoverFileTest(unittest.TestCase):
    """F24: the pending discovery cleanup is recorded by the ordered writer, not on the calling (loop) thread."""

    def test_written_by_the_writer_in_order(self):
        with tempfile.TemporaryDirectory() as d:
            pub = camp._publisher()
            pub.hass.config.path = lambda *p: os.path.join(d, *p)
            with mock.patch.object(mp, "write_json", side_effect=AssertionError("written on the caller's thread")), \
                    mock.patch.object(mp.os, "remove", side_effect=AssertionError("removed on the caller's thread")):
                pub._undiscover_due = False
                pub._set_undiscover_due(True)
                pub._set_undiscover_due(False)
                pub._set_undiscover_due(True)
                pub._set_undiscover_due(False)
            self.assertTrue(writer.drain(5))
            self.assertFalse(pub._read_undiscover_due())
            pub._set_undiscover_due(True)
            self.assertTrue(writer.drain(5))
            self.assertTrue(pub._read_undiscover_due())

    def test_a_file_from_an_older_version_is_due(self):
        with tempfile.TemporaryDirectory() as d:
            pub = camp._publisher()
            pub.hass.config.path = lambda *p: os.path.join(d, *p)
            os.makedirs(os.path.join(d, "integration_manager"))
            with open(pub._undiscover_file(), "w", encoding="utf-8") as fh:
                json.dump({"base": BASE, "prefix": "homeassistant"}, fh)
            self.assertTrue(pub._read_undiscover_due())
            os.remove(pub._undiscover_file())
            self.assertFalse(pub._read_undiscover_due())


class EventsOffTheLoopTest(unittest.IsolatedAsyncioTestCase):
    """F24: an event added on the loop is written by another thread, in order."""

    async def test_loop_adds_do_not_touch_the_file_and_keep_their_order(self):
        with tempfile.TemporaryDirectory() as d:
            store = events.Events(os.path.join(d, "integration_manager", "events.jsonl"))
            loop_thread = threading.get_ident()
            writers = set()

            def opened(*args, **kwargs):
                if "a" in str(args[1:2]) or "a" in str(kwargs.get("mode", "")):
                    writers.add(threading.get_ident())
                return open(*args, **kwargs)

            with mock.patch.object(events, "open", side_effect=opened, create=True):
                for i in range(50):
                    store.add("mqtt", f"loop {i}")
                await asyncio.get_running_loop().run_in_executor(None, store.add, "mqtt", "executor 50")
                store.add("mqtt", "loop 51")
                rows = await asyncio.get_running_loop().run_in_executor(None, store.recent, 100)
            self.assertEqual([r["message"] for r in rows], [f"loop {i}" for i in range(50)] + ["executor 50", "loop 51"])
            self.assertTrue(writers)
            self.assertNotIn(loop_thread, writers)

    def test_rotation_counts_bytes(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(events, "MAX_BYTES", 1000):
            path = os.path.join(d, "integration_manager", "events.jsonl")
            store = events.Events(path)
            for _ in range(6):
                store.add("mqtt", "é" * 60)  # 120 bytes of text in 60 characters
            self.assertTrue(events.drain(5))
            self.assertLessEqual(os.path.getsize(path), 1000)


def _parent(results):
    client = mock.Mock()
    client.commands = mock.AsyncMock(side_effect=results)
    return client


def _call(view, action, body=None):
    request = mock.Mock()
    emitted = []
    with mock.patch.object(views.http_util, "_json_object", mock.AsyncMock(return_value=body or {})), \
            mock.patch.object(parity.events, "emit", side_effect=lambda *a, **k: emitted.append((a, k))), \
            mock.patch.object(view, "json", side_effect=lambda d, **k: d):
        return asyncio.run(view.post(request, action=action)), emitted


class CutoverPreconditionTest(unittest.TestCase):
    """F5 and F6."""

    def test_an_unreadable_parent_registry_blocks(self):
        view = views._view()
        view.publisher.config.discovery_enabled = False
        client = _parent([[{"components": ["mqtt"]}], [[]], ValueError("cannot reach the parent at http://parent: TimeoutError")])
        with mock.patch.object(parity, "_parent_client", return_value=client):
            res, _ = _call(view, "enable")
        self.assertFalse(res["ok"])
        self.assertIn("could not be checked", res["error"])
        view.publisher.async_republish_all.assert_not_awaited()

    def test_an_unreachable_parent_for_the_config_entries_blocks(self):
        view = views._view()
        client = _parent([[{"components": ["mqtt"]}], ValueError("cannot reach the parent at http://parent: ClientError"), [[]]])
        with mock.patch.object(parity, "_parent_client", return_value=client):
            res, _ = _call(view, "enable")
        self.assertFalse(res["ok"])
        self.assertIn("config entries of ramses_cc", res["error"])

    def test_an_older_parent_without_the_command_uses_the_components(self):
        view = views._view()
        client = _parent([[{"components": ["mqtt"]}], parity.ParentCommandFailed("config_entries/get: Unknown command."), [[]]])
        with mock.patch.object(parity, "_parent_client", return_value=client):
            res, _ = _call(view, "enable")
        self.assertTrue(res["ok"], res)

    def test_force_is_recorded(self):
        view = views._view()
        res, emitted = _call(view, "enable", {"force": True})
        self.assertTrue(res["ok"])
        self.assertTrue(res["forced"])
        self.assertIn("forced", emitted[-1][0][1])
        self.assertTrue(emitted[-1][1]["forced"])
        res, emitted = _call(views._view(parent=False), "enable")
        self.assertFalse(res["forced"])
        self.assertNotIn("forced", emitted[-1][0][1])


class UndoManagerDeviceTest(unittest.TestCase):
    """F12: Undo keeps the manager device while manager_discovery is on, and says so."""

    def test_answer_and_timeline_say_it(self):
        for on in (True, False):
            with self.subTest(manager_discovery=on):
                view = views._view()
                view.publisher.config.manager_discovery = on
                res, emitted = _call(view, "undo")
                self.assertIs(res["manager_device_kept"], on)
                self.assertEqual("manager device stays" in emitted[-1][0][1], on)


class OrphanWithDiscoveryOffTest(unittest.TestCase):
    """F7: with discovery off, removing one orphan would remove its whole device on the main HA."""

    def test_refused_by_the_view(self):
        publisher = mock.Mock()
        publisher.config.discovery_enabled = False
        view = parity.ParityActionView(mock.Mock(), mock.Mock(), publisher)
        with mock.patch.object(parity, "compute_parity", mock.AsyncMock(return_value={"orphans": [
                {"parent_entity_id": "sensor.a", "discovery_id": f"{BASE}_dev", "our_entity_id": "sensor.a", "parent_domain": "sensor"}]})):
            res, _ = _call(view, "remove_orphans", {"entity_ids": ["sensor.a"]})
        self.assertFalse(res["ok"])
        self.assertIn("discovery is off", res["error"])
        publisher.remove_discovered_component.assert_not_called()

    def test_the_publisher_never_sends_the_device_level_empty_config(self):
        pub = camp._publisher(discovery_enabled=False)
        pub._group_by_device = lambda: ({f"{BASE}_dev": ({"identifiers": [f"{BASE}_dev"]}, {"sensor.b": {"platform": "sensor"}})}, {})
        pub._manager_discovery = lambda: (f"{BASE}_manager", {"identifiers": [f"{BASE}_manager"]}, {"sensor_health": {"platform": "sensor"}})
        self.assertFalse(pub.remove_discovered_component(f"{BASE}_dev", "sensor.a", "sensor"))
        self.assertFalse(pub.remove_discovered_component(f"{BASE}_manager", "sensor.old", "sensor"))  # manager_discovery off
        self.assertEqual(pub._client.published, [])

    def test_the_manager_device_keeps_its_siblings(self):
        """It is never in _group_by_device: the removal form went out as an empty config, with discovery on too."""
        for config in ({"discovery_enabled": True}, {"manager_discovery": True}):
            with self.subTest(config=config):
                pub = camp._publisher(**config)
                pub._group_by_device = lambda: ({}, {})
                pub._manager_discovery = lambda: (f"{BASE}_manager", {"identifiers": [f"{BASE}_manager"]},
                                                  {"sensor.health": {"platform": "sensor", "unique_id": "u"}})
                self.assertTrue(pub.remove_discovered_component(f"{BASE}_manager", "sensor.old", "sensor"))
                ((topic, payload, _qos, _retain),) = pub._client.published
                self.assertEqual(json.loads(payload)["components"],
                                 {"sensor_health": {"platform": "sensor", "unique_id": "u"}, "sensor_old": {"platform": "sensor"}})

    def test_the_view_lets_a_manager_orphan_through_while_manager_discovery_announces_it(self):
        publisher = mock.Mock(base_topic=BASE)
        publisher.config.discovery_enabled, publisher.config.manager_discovery = False, True
        view = parity.ParityActionView(mock.Mock(), mock.Mock(), publisher)
        with mock.patch.object(parity, "compute_parity", mock.AsyncMock(return_value={"orphans": [
                {"parent_entity_id": "sensor.old", "discovery_id": f"{BASE}_manager", "our_entity_id": "sensor.old", "parent_domain": "sensor"}]})):
            res, _ = _call(view, "remove_orphans", {"entity_ids": ["sensor.old"]})
        self.assertTrue(res["ok"])
        publisher.remove_discovered_component.assert_called_once_with(f"{BASE}_manager", "sensor.old", "sensor")


class PreviewViaDeviceTest(unittest.TestCase):
    """Cleanup: the preview shows the device block the publish sends."""

    def test_via_device_to_an_unannounced_device_is_stripped(self):
        pub = camp._publisher(discovery_enabled=True)
        block = {"identifiers": [f"{BASE}_child"], "name": "child", "via_device": f"{BASE}_hub_not_announced"}
        pub._group_by_device = lambda: ({f"{BASE}_child": (block, {"sensor.a": {"platform": "sensor"}})}, {})
        pub._manager_discovery = lambda: (f"{BASE}_manager", {"identifiers": [f"{BASE}_manager"]}, {})
        preview = {d["discovery_id"]: d for d in pub.discovery_preview()}
        self.assertNotIn("via_device", preview[f"{BASE}_child"]["device"])


class RulesGlobCacheTest(unittest.TestCase):
    """Cleanup: the glob order is computed once per change of the rules, with the same result."""

    def test_same_result_and_follows_changes(self):
        with tempfile.TemporaryDirectory() as d:
            rules = mqtt_rules.MqttRules(os.path.join(d, "rules.json"))
            rules.replace_all({"sensor.*": {"icon": "mdi:a"}, "sensor.b*": {"icon": "mdi:b"}, "sensor.bx": {"name": "Exact"}})
            self.assertEqual(rules.for_entity("sensor.bx"), {"icon": "mdi:b", "name": "Exact"})
            with mock.patch.object(mqtt_rules, "sorted", create=True, side_effect=AssertionError("sorted again")):
                self.assertEqual(rules.for_entity("sensor.a"), {"icon": "mdi:a"})
            rules.set("sensor.a*", icon="mdi:c")
            self.assertEqual(rules.for_entity("sensor.a"), {"icon": "mdi:c"})
            rules.replace_all({})
            self.assertEqual(rules.for_entity("sensor.a"), {})


class CallMemoryThreadsTest(unittest.TestCase):
    """Concern: _calls is iterated on paho's thread while the loop deletes a call refused for the in-flight cap."""

    def test_a_delete_from_the_loop_waits_for_the_iteration(self):
        pub = camp._publisher()
        pub._in_flight = mp.CALLS_IN_FLIGHT_MAX
        scheduled = []
        pub.hass.loop.call_soon_threadsafe = scheduled.append
        pub.hass.async_create_task = lambda coro: coro
        pub.hass.services.has_service = lambda d, s: True
        pub._call_target_problem = lambda *a: None
        pub._publish_result = lambda *a: None
        pub._on_call("light/turn_on", json.dumps({"_id": "refused"}))
        loop_thread = threading.Thread(target=lambda: asyncio.run(scheduled[0]()))

        class Record(dict):
            started = False

            def __getitem__(self, key):
                if not Record.started:  # the loop runs the refusal while paho's thread is inside the iteration
                    Record.started = True
                    loop_thread.start()
                    loop_thread.join(0.3)
                return super().__getitem__(key)

        now = time.time()
        pub._calls = {"a": Record(received=now, state="ok", result=None), **pub._calls,
                      **{f"k{i}": {"received": now, "state": "ok", "result": None} for i in range(5)}}
        pub._seen_call(None)  # raised "dictionary changed size during iteration" without the lock
        loop_thread.join(5)
        self.assertNotIn(mp._call_key("light", "turn_on", "refused"), pub._calls)


class ReloadExclusionTest(unittest.IsolatedAsyncioTestCase):
    """Concern: an exclusion adopted through async_reload_config (a save still waiting for its reconnect, then a
    cutover) left the entities in _topics, published and commandable."""

    async def test_excluded_entities_are_cleared(self):
        pub = camp._publisher(exclude_integrations=["integration_manager"])
        pub._conn_lock = asyncio.Lock()
        pub._topics = {"sensor.a": f"{BASE}/demo/sensor/a", "sensor.b": f"{BASE}/other/sensor/b"}
        pub._pending_clears = set()
        pub._integration_of = lambda eid: {"sensor.a": "demo", "sensor.b": "other"}[eid]
        new = mp.MqttConfig(exclude_integrations=["integration_manager", "demo"])

        async def executor(fn, *args):
            return fn(*args)

        pub.hass.async_add_executor_job = executor
        pub._load = lambda: new
        await pub.async_reload_config()
        self.assertEqual(set(pub._topics), {"sensor.b"})
        self.assertIn((f"{BASE}/demo/sensor/a", "", 1, True), pub._client.published)
        self.assertIs(pub.config, new)


class CaCertsOnLoadTest(unittest.TestCase):
    """Concern: a restored mqtt.json could point ca_certs anywhere; the save rule now applies on load too."""

    def test_outside_the_config_directory_is_dropped(self):
        with tempfile.TemporaryDirectory() as d, tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as outside:
            self.addCleanup(os.remove, outside.name)
            inside = os.path.join(d, "ca.pem")
            open(inside, "w").close()
            pub = object.__new__(mp.MqttPublisher)
            pub.hass = SimpleNamespace(config=SimpleNamespace(config_dir=d))
            pub.path = os.path.join(d, "mqtt.json")
            for stored, expected in ((outside.name, ""), ("ca.pem", inside), ("../" + os.path.basename(outside.name), ""),
                                     ("missing-for-now.pem", os.path.join(d, "missing-for-now.pem"))):
                with self.subTest(stored=stored):
                    with open(pub.path, "w", encoding="utf-8") as fh:
                        json.dump({"tls": True, "ca_certs": stored}, fh)
                    with self.assertNoLogs(mp._LOGGER, "WARNING") if expected else self.assertLogs(mp._LOGGER, "WARNING"):
                        self.assertEqual(pub._load().ca_certs, expected)


class ManagerResultWhileMovingTest(unittest.IsolatedAsyncioTestCase):
    """Concern: the retained manager document must not go out under the old names while a move sweeps them."""

    async def test_answer_only(self):
        pub = camp._publisher()
        pub._moving = True
        pub.manager = SimpleNamespace(document=lambda: {"manager_version": "1"})
        infos = []

        def publish(topic, payload=None, qos=0, retain=False):
            infos.append((topic, retain))
            return SimpleNamespace(rc=0, wait_for_publish=lambda t: None)

        pub._client.publish = publish

        async def executor(fn, *args):
            return fn(*args)

        pub.hass.async_add_executor_job = lambda fn, *a: asyncio.ensure_future(executor(fn, *a))
        await pub.async_publish_manager_result({"ok": True, "action": "backup"})
        self.assertEqual(infos, [(f"{BASE}/manager/result", False)])


if __name__ == "__main__":
    unittest.main()
