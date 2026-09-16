"""Twelfth review, MQTT side.  m3: a SUBACK refusing the command topics left the connection "connected" with no error,
and paho's own log was never enabled.  m4: "online" went out before the SUBSCRIBE, so a command sent at the availability
flip was lost.  m5: the value of a text entity in password mode was kept in clear in the command history, the status
and the log.  D2: an entity moved into a device whose config is over the broker's maximum rescheduled the discovery pass
every 5 s for good.  C11: a whitespace-only call payload ran the service with no data.
C13: a live discovery-prefix change swept every retained entity document, not only the discovery configs.  Every test fails on the tree
before its fix."""

import asyncio
import json
import socket
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from custom_components.integration_manager import mqtt_publisher as mp
from tests import test_camp_publish as camp

BASE = camp.BASE
NOT_AUTHORIZED = 0x87
PASSWORD = "s3cr3t-broker-pass"


def _until(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class _Mqtt5Broker:
    """MQTT 5 on a loopback socket, one connection: CONNACK success, a SUBACK with the given reason code for every topic
    (held back until `suback_gate` is set, never sent with answer_suback=False), PUBACK for QoS 1, PINGRESP.
    Records what arrives, in order."""

    def __init__(self, suback_code=0, answer_suback=True):
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.suback_code, self.answer_suback = suback_code, answer_suback
        self.suback_gate = threading.Event()
        self.suback_gate.set()
        self.conn = None
        self.received: list[tuple] = []
        self.connect_body = b""
        self._send_lock = threading.Lock()
        threading.Thread(target=self._serve, daemon=True).start()

    def _read(self, n):
        data = b""
        while len(data) < n:
            chunk = self.conn.recv(n - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    @staticmethod
    def _varint(data, i):
        value, shift = 0, 0
        while True:
            byte = data[i]
            i += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return value, i

    def _send(self, data):
        with self._send_lock:
            self.conn.sendall(data)

    def _suback(self, pid, n):
        self.suback_gate.wait(30)
        body = pid + b"\x00" + bytes([self.suback_code]) * n
        try:
            self._send(bytes([0x90, len(body)]) + body)
        except OSError:
            pass

    def _serve(self):
        self.conn, _ = self.server.accept()
        try:
            while True:
                first = self._read(1)[0]
                kind = first >> 4
                length, shift = 0, 0
                while True:
                    byte = self._read(1)[0]
                    length |= (byte & 0x7F) << shift
                    shift += 7
                    if not byte & 0x80:
                        break
                body = self._read(length)
                if kind == 1:  # CONNECT
                    self.connect_body = body
                    self.received.append(("connect",))
                    self._send(b"\x20\x03\x00\x00\x00")
                elif kind == 8:  # SUBSCRIBE
                    pid = body[:2]
                    props_len, i = self._varint(body, 2)
                    i += props_len
                    topics = []
                    while i < len(body):
                        n = int.from_bytes(body[i:i + 2], "big")
                        topics.append(body[i + 2:i + 2 + n].decode())
                        i += 2 + n + 1
                    self.received.append(("subscribe", topics))
                    if self.answer_suback:
                        threading.Thread(target=self._suback, args=(pid, len(topics)), daemon=True).start()
                elif kind == 3:  # PUBLISH
                    qos, retain = (first >> 1) & 3, bool(first & 1)
                    n = int.from_bytes(body[:2], "big")
                    topic, i = body[2:2 + n].decode(), 2 + n
                    pid = body[i:i + 2]
                    if qos:
                        i += 2
                    props_len, i = self._varint(body, i)
                    self.received.append(("publish", topic, body[i + props_len:], retain))
                    if qos == 1:
                        self._send(b"\x40\x02" + pid)
                elif kind == 12:  # PINGREQ
                    self._send(b"\xd0\x00")
                elif kind == 14:  # DISCONNECT
                    return
        except (EOFError, OSError):
            return

    def published(self, topic):
        return [r for r in self.received if r[0] == "publish" and r[1] == topic]

    def close(self):
        self.suback_gate.set()
        for s in (self.conn, self.server):
            if s is not None:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                s.close()


def _live_publisher(broker, **config):
    """A publisher connecting for real (paho) to the loopback broker, without Home Assistant."""
    pub = camp._publisher(host="127.0.0.1", port=broker.port, force_base_topic=True, **config)
    pub._client, pub._connected = None, False
    pub._probed_ok = set()
    pub._tls_checked_at, pub._tls_error = 0.0, ""
    pub.stats.update(connected=False, connect_error="")
    return pub


class _LiveTest(unittest.TestCase):
    def connect(self, broker, **config):
        pub = _live_publisher(broker, **config)
        self.addCleanup(broker.close)
        self.addCleanup(lambda: pub._disconnect(publish_offline=False))
        with mock.patch.object(mp.MqttPublisher, "_sweep_old_identity", return_value=True):
            pub._connect()
        return pub


class SubscriptionRefusedTest(_LiveTest):
    """m3: a broker ACL allowing publish but denying subscribe killed commands, calls and manager actions silently."""

    def test_a_refused_suback_is_reported(self):
        broker = _Mqtt5Broker(suback_code=NOT_AUTHORIZED)
        with mock.patch.object(mp.events, "emit") as emit, self.assertLogs(mp._LOGGER, "ERROR") as logs:
            pub = self.connect(broker)
            self.assertTrue(_until(lambda: pub.stats.get("subscribe_error")), "the refusal was never recorded")
        self.assertTrue(pub.stats["connected"])  # state mirroring still works
        error = pub.stats["subscribe_error"]
        for topic in (f"{BASE}/cmd/#", f"{BASE}/call/#", f"{BASE}/manager/cmd/+"):
            self.assertIn(topic, error)
        self.assertIn("Not authorized", error)
        self.assertEqual(pub.stats["connect_error"], error)
        self.assertEqual([c.args for c in emit.call_args_list].count(("mqtt", error)), 1)
        self.assertEqual(len(logs.output), 1)

    def test_a_granted_suback_reports_nothing(self):
        broker = _Mqtt5Broker(suback_code=1)
        with mock.patch.object(mp.events, "emit") as emit:
            pub = self.connect(broker)
            self.assertTrue(_until(lambda: pub._connected and pub._subscribing is None))
        self.assertEqual(pub.stats.get("subscribe_error", ""), "")
        self.assertEqual(pub.stats["connect_error"], "")
        emit.assert_called_once()  # "connected to ..."

    def test_paho_logs_through_our_logger_without_the_password(self):
        broker = _Mqtt5Broker()
        with self.assertLogs(mp._PAHO_LOGGER, "DEBUG") as logs:
            pub = self.connect(broker, username="hri", password=PASSWORD)
            self.assertTrue(_until(lambda: pub._connected and pub._subscribing is None))
        self.assertIn(PASSWORD.encode(), broker.connect_body)  # the password did go over the wire
        lines = [r.getMessage() for r in logs.records]
        self.assertTrue(any("Sending CONNECT" in line for line in lines), lines)
        self.assertFalse([line for line in lines if PASSWORD in line])


def _suback(*codes):
    return [ReasonCode(PacketTypes.SUBACK, identifier=c) for c in codes]


class SubscribeDoubleTest(unittest.TestCase):
    """m3 with a double shaped like paho: subscribe returns (rc, mid), the SUBACK brings ReasonCodes."""

    class Client(camp.FakeClient):
        def __init__(self, rc=mqtt.MQTT_ERR_SUCCESS):
            super().__init__()
            self.subscribe_rc = rc

        def subscribe(self, topics):
            self.subscribed.append(topics)
            return (self.subscribe_rc, 7 if self.subscribe_rc == mqtt.MQTT_ERR_SUCCESS else None)

    def _connected(self, pub, client):
        pub._client = client
        pub._on_connect(client, None, None, 0, None)

    def test_a_subscribe_that_could_not_be_sent_is_reported(self):
        pub = camp._publisher()
        with mock.patch.object(mp.events, "emit") as emit, self.assertLogs(mp._LOGGER, "ERROR"):
            self._connected(pub, self.Client(rc=mqtt.MQTT_ERR_NO_CONN))
        self.assertIn("could not be sent", pub.stats["subscribe_error"])
        self.assertIn("could not be sent", emit.call_args_list[0].args[1])

    def test_the_same_refusal_is_logged_once_across_reconnects(self):
        pub = camp._publisher()
        with mock.patch.object(mp.events, "emit") as emit, self.assertLogs(mp._LOGGER, "ERROR") as logs:
            for _ in range(3):
                client = self.Client()
                self._connected(pub, client)
                pub._on_subscribe(client, None, 7, _suback(NOT_AUTHORIZED, 1, 1))
        self.assertEqual(len(logs.output), 1)
        self.assertEqual([c.args[1] for c in emit.call_args_list].count(pub.stats["subscribe_error"]), 1)
        self.assertIn(f"{BASE}/cmd/#", pub.stats["subscribe_error"])
        self.assertNotIn(f"{BASE}/call/#", pub.stats["subscribe_error"])
        # granted again: the next refusal is news
        client = self.Client()
        self._connected(pub, client)
        pub._on_subscribe(client, None, 7, _suback(1, 1, 1))
        self.assertEqual(pub.stats["subscribe_error"], "")
        client = self.Client()
        self._connected(pub, client)
        with mock.patch.object(mp.events, "emit"), self.assertLogs(mp._LOGGER, "ERROR"):
            pub._on_subscribe(client, None, 7, _suback(NOT_AUTHORIZED, 1, 1))

    def test_a_suback_of_an_earlier_client_is_ignored(self):
        pub = camp._publisher()
        old = self.Client()
        self._connected(pub, old)
        self._connected(pub, self.Client())
        pub._on_subscribe(old, None, 7, _suback(NOT_AUTHORIZED, NOT_AUTHORIZED, NOT_AUTHORIZED))
        self.assertEqual(pub.stats["subscribe_error"], "")

    def test_mqtt311_refusal_codes_count_too(self):
        """paho turns a 3.1.1 SUBACK's 0x80 into a ReasonCode that is_failure."""
        pub = camp._publisher()
        client = self.Client()
        self._connected(pub, client)
        with mock.patch.object(mp.events, "emit"), self.assertLogs(mp._LOGGER, "ERROR"):
            pub._on_subscribe(client, None, 7, _suback(0x80, 0x80, 0x80))
        self.assertIn("Unspecified error", pub.stats["subscribe_error"])


class OnlineAfterSubackTest(_LiveTest):
    """m4: the main HA sends commands as soon as the device turns available; they must find the subscription in place."""

    def _online(self, broker):
        return broker.published(f"{BASE}/status")

    def test_online_waits_for_the_suback(self):
        broker = _Mqtt5Broker()
        broker.suback_gate.clear()
        with mock.patch.object(mp.events, "emit"):
            pub = self.connect(broker)
            self.assertTrue(_until(lambda: any(r[0] == "subscribe" for r in broker.received)))
            time.sleep(0.3)
            self.assertEqual(self._online(broker), [], "online went out before the broker acknowledged the subscription")
            broker.suback_gate.set()
            self.assertTrue(_until(lambda: self._online(broker)))
        kinds = [r[0] if r[0] != "publish" else r[1] for r in broker.received]
        self.assertLess(kinds.index("subscribe"), kinds.index(f"{BASE}/status"))
        self.assertEqual(self._online(broker), [("publish", f"{BASE}/status", b"online", True)])
        self.assertEqual(pub.stats["subscribe_error"], "")

    def test_a_refused_subscription_still_goes_online(self):
        broker = _Mqtt5Broker(suback_code=NOT_AUTHORIZED)
        with mock.patch.object(mp.events, "emit"), self.assertLogs(mp._LOGGER, "ERROR"):
            pub = self.connect(broker)
            self.assertTrue(_until(lambda: self._online(broker)))
        self.assertIn("Not authorized", pub.stats["subscribe_error"])

    def test_a_broker_that_never_acknowledges_gets_online_after_the_wait(self):
        broker = _Mqtt5Broker(answer_suback=False)
        with mock.patch.object(mp.events, "emit"):
            pub = self.connect(broker)
            self.assertTrue(_until(lambda: pub.hass.loop.call_later.called))
            time.sleep(0.3)
            self.assertEqual(self._online(broker), [])
            delay, overdue, *args = pub.hass.loop.call_later.call_args.args
            self.assertEqual(delay, mp.SUBACK_WAIT_S)
            with self.assertLogs(mp._LOGGER, "ERROR"):
                overdue(*args)
            self.assertTrue(_until(lambda: self._online(broker)))
            self.assertIn("did not acknowledge", pub.stats["subscribe_error"])
            overdue(*args)  # once
            time.sleep(0.2)
        self.assertEqual(len(self._online(broker)), 1)


class OverdueDoubleTest(unittest.TestCase):
    def _connect(self, pub, client):
        pub._client = client
        pub._on_connect(client, None, None, 0, None)
        return pub.hass.loop.call_later.call_args.args

    def test_a_late_suback_clears_the_overdue_error_without_a_second_online(self):
        pub = camp._publisher()
        client = SubscribeDoubleTest.Client()
        _delay, overdue, *args = self._connect(pub, client)
        with mock.patch.object(mp.events, "emit"), self.assertLogs(mp._LOGGER, "ERROR"):
            overdue(*args)
        pub._on_subscribe(client, None, 7, _suback(1, 1, 1))
        self.assertEqual(pub.stats["subscribe_error"], "")
        self.assertEqual([p for p in client.published if p[0] == f"{BASE}/status"], [(f"{BASE}/status", "online", 1, True)])

    def test_the_timer_of_a_replaced_client_does_nothing(self):
        pub = camp._publisher()
        old = SubscribeDoubleTest.Client()
        _delay, overdue, *args = self._connect(pub, old)
        new = SubscribeDoubleTest.Client()
        self._connect(pub, new)
        overdue(*args)
        self.assertEqual(old.published, [])
        self.assertEqual(pub.stats["subscribe_error"], "")

    def test_nothing_goes_online_while_stopping(self):
        pub = camp._publisher()
        client = SubscribeDoubleTest.Client()
        self._connect(pub, client)
        pub._stopping = True
        pub._on_subscribe(client, None, 7, _suback(1, 1, 1))
        self.assertEqual(client.published, [])


class PasswordTextTest(unittest.IsolatedAsyncioTestCase):
    """m5: discovery announces a text entity in password mode as one; what is typed into it stays out of every record."""

    def setUp(self):
        self.pub = camp._publisher()
        modes = {"text.pw": "password", "text.plain": "text"}
        self.pub.hass.states.get = lambda eid: SimpleNamespace(attributes={"mode": modes[eid]}) if eid in modes else None
        self.pub._topics = {"text.pw": "t1", "text.plain": "t2", "text.later": "t3"}
        self.pub.stats.update(commands=0, last_command=None)
        self.tasks = []

        def create(coro):
            task = asyncio.ensure_future(coro)
            self.tasks.append(task)
            return task

        self.pub.hass.async_create_task = create

    def _command(self, object_id, value):
        self.pub._handle_message(SimpleNamespace(topic=f"{BASE}/cmd/text/{object_id}/value", payload=value.encode(), retain=False))

    def _visible(self):
        return json.dumps([list(self.pub.history), self.pub.stats, self.pub.recent_commands()], default=str)

    async def test_a_command_is_recorded_masked(self):
        self.pub.hass.services.async_call = mock.AsyncMock()
        self._command("pw", "hunter2")
        await asyncio.gather(*self.tasks)
        self.assertNotIn("hunter2", self._visible())
        self.assertEqual(self.pub.history[-1]["data"], "***")
        self.assertEqual(self.pub.history[-1]["state"], "ok")
        self.assertEqual(self.pub.stats["last_command"], f"{BASE}/cmd/text/pw/value = ***")
        self.pub.hass.services.async_call.assert_awaited_once_with("text", "set_value", {"entity_id": "text.pw", "value": "hunter2"}, blocking=True)

    async def test_the_service_error_quoting_the_value_is_masked(self):
        self.pub.hass.services.async_call = mock.AsyncMock(side_effect=ValueError("Value hunter2 for text.pw is too short (minimum length 8)"))
        with self.assertLogs(mp._LOGGER, "ERROR") as logs:
            self._command("pw", "hunter2")
            await asyncio.gather(*self.tasks)
        self.assertNotIn("hunter2", self._visible() + "".join(logs.output))
        self.assertEqual(self.pub.history[-1]["error"], "ValueError: Value *** for text.pw is too short (minimum length 8)")

    async def test_a_plain_text_entity_is_recorded_as_sent(self):
        self.pub.hass.services.async_call = mock.AsyncMock()
        self._command("plain", "hello")
        await asyncio.gather(*self.tasks)
        self.assertEqual(self.pub.history[-1]["data"], "hello")

    async def test_an_entity_without_a_state_is_read_from_its_registry_capabilities(self):
        entry = SimpleNamespace(capabilities={"mode": "password", "min": 0, "max": 100})
        registry = SimpleNamespace(async_get=lambda eid: entry if eid == "text.later" else None)
        self.pub.hass.services.async_call = mock.AsyncMock()
        with mock.patch.object(mp.er, "async_get", return_value=registry):
            self._command("later", "hunter2")
            await asyncio.gather(*self.tasks)
        self.assertNotIn("hunter2", self._visible())

    async def test_a_service_call_setting_it_is_recorded_masked(self):
        self.pub.hass.services.has_service = lambda d, s: True
        self.pub.hass.services.supports_response = lambda d, s: mp.SupportsResponse.NONE
        self.pub.hass.services.async_call = mock.AsyncMock(side_effect=ValueError("Value hunter2 for text.pw is too long"))
        self.pub.hass.loop.call_soon_threadsafe = lambda f: f()
        with mock.patch.object(mp.MqttPublisher, "_call_target_problem", return_value=None), \
                mock.patch.object(mp.MqttPublisher, "_publish_result") as result, self.assertLogs(mp._LOGGER, "WARNING"):
            self.pub._on_call("text/set_value", json.dumps({"entity_id": "text.pw", "value": "hunter2", "_id": 1}))
            await asyncio.gather(*self.tasks)
        self.assertNotIn("hunter2", self._visible())
        self.assertIn("***", self.pub.history[-1]["data"])
        self.assertEqual(self.pub.history[-1]["error"], "ValueError: Value *** for text.pw is too long")
        self.assertIn("hunter2", result.call_args.args[2]["error"])  # the caller, who sent it, gets the service's own words

    async def test_a_call_by_area_masks_when_a_published_text_entity_is_a_password(self):
        registry = SimpleNamespace(async_get=lambda eid: None)
        with mock.patch.object(mp.er, "async_get", return_value=registry):
            self.pub._on_call("text/set_value", json.dumps({"area_id": "hall", "value": "hunter2"}))
        self.assertNotIn("hunter2", self._visible())
        for task in self.tasks:
            task.cancel()


class MovedIntoOversizedDeviceTest(unittest.TestCase):
    """D2: the device that took an entity over is sent again 5 s later.  While the old owner's config cannot be published
    (over the broker's maximum), the map keeps the entity there, so every pass saw the move again and scheduled the next."""

    def _run(self, discovery_map, groups):
        pub = camp._publisher(discovery_enabled=True)
        pub._orphan_sweep_due, pub._boot_components = False, None
        pub._blocks = {}
        pub._broker_max_packet = 4096
        pub._discovery_map = discovery_map
        scheduled = []
        pub.hass.loop.call_later = lambda delay, fn, *args: scheduled.append((fn, args))
        passes = 0
        with mock.patch.object(mp.MqttPublisher, "_announced_groups", return_value=(groups, {"mirrored": 0, "disabled": 0})), \
                mock.patch.object(mp.events, "emit"), self.assertLogs(mp._LOGGER, "ERROR"):
            pub._publish_discovery_all()
            first = len(scheduled)
            while scheduled and passes < 5:
                fn, args = scheduled.pop()
                fn(*args)
                passes += 1
        return pub, first, passes, scheduled

    def test_an_oversized_old_owner_does_not_loop(self):
        small = {"platform": "sensor", "unique_id": "u", "state_topic": "t"}
        big = {**small, "json_attributes_template": "x" * 5000}
        discovery_map = {"dev_b": {"sensor.e3": small}, "dev_a": {"sensor.e1": small, "sensor.e2": small}}
        groups = {"dev_a": ({"name": "a"}, {"sensor.e2": big}), "dev_b": ({"name": "b"}, {"sensor.e3": small, "sensor.e1": small})}
        pub, first, passes, scheduled = self._run(discovery_map, groups)
        self.assertEqual(first, 1)  # the move itself is sent again once
        self.assertEqual((passes, scheduled), (1, []), "the follow-up pass scheduled yet another one")
        self.assertIn("homeassistant/device/dev_a/config", pub.stats["last_oversized"])

    def test_an_oversized_new_owner_does_not_loop(self):
        small = {"platform": "sensor", "unique_id": "u", "state_topic": "t"}
        big = {**small, "json_attributes_template": "x" * 5000}
        discovery_map = {"dev_a": {"sensor.e1": small, "sensor.e2": small}}
        groups = {"dev_a": ({"name": "a"}, {"sensor.e2": small}), "dev_b": ({"name": "b"}, {"sensor.e1": big})}
        _pub, _first, passes, scheduled = self._run(discovery_map, groups)
        self.assertLessEqual(passes, 1)
        self.assertEqual(scheduled, [])

class WhitespaceCallTest(unittest.TestCase):
    """C11: b"" was refused, b"  " ran as {}."""

    def test_a_whitespace_payload_is_refused_like_an_empty_one(self):
        for payload in (b"", b"  ", b"\n\t "):
            with self.subTest(payload=payload):
                pub = camp._publisher()
                pub.hass.async_create_task = mock.Mock()
                pub._handle_message(SimpleNamespace(topic=f"{BASE}/call/script/turn_on", payload=payload, retain=False))
                pub.hass.async_create_task.assert_not_called()
                self.assertEqual(pub.history[-1]["state"], "rejected")
                self.assertIn("empty payload", pub.history[-1]["error"])
                topic, answer, _qos, retain = pub._client.published[-1]
                self.assertEqual((topic, retain), (f"{BASE}/result/script/turn_on", False))
                self.assertFalse(json.loads(answer)["ok"])


class LivePrefixMoveTest(unittest.TestCase):
    """C13: the documents live under the base topic, which a new discovery prefix does not move."""

    def _move(self, new_prefix="homeassistant", new_base=BASE):
        from custom_components.integration_manager import discovery as disc

        pub = camp._publisher(enabled=False, discovery_prefix="homeassistant")
        pub._key_provider = lambda: new_base
        pub._last_wanted = new_base
        pub._pending_clears, pub._discovery_map, pub._blocks = set(), {}, {}
        pub._registry_timer = pub._services_timer = None
        pub._republish_interval = pub.config.republish_interval_s
        new = mp.MqttConfig(enabled=False, discovery_prefix=new_prefix)
        pub._load = lambda: new
        pub._disconnect = lambda publish_offline=True: None
        pub.publish_health = lambda: None
        retained = {
            f"{BASE}/demo/sensor/x": json.dumps({"published_at": "t", "integration": "demo"}).encode(),
            "homeassistant/device/hass_camp_dev/config": json.dumps({"origin": disc.origin(BASE + "_")}).encode(),
        }
        scanned, cleared = [], []

        def scan(_self, _suffix, topics, min_s=2.0):
            scanned.extend(t for t, _q in topics)
            return {t: p for t, p in retained.items()
                    if any(mqtt.topic_matches_sub(sub, t) for sub, _q in topics)}

        async def executor(func, *args):
            return func(*args)

        pub.hass.async_add_executor_job = executor
        with mock.patch.object(mp.MqttPublisher, "_retained_scan", scan), \
                mock.patch.object(mp.MqttPublisher, "_clear_topics", lambda _self, _suffix, topics: cleared.extend(topics)), \
                mock.patch.object(mp.MqttPublisher, "_remember_identity"):
            asyncio.run(pub._async_reconnect_locked())
        return scanned, cleared

    def test_a_new_prefix_clears_only_the_discovery_configs(self):
        scanned, cleared = self._move(new_prefix="ha2")
        self.assertEqual(cleared, ["homeassistant/device/hass_camp_dev/config"])
        self.assertNotIn(f"{BASE}/#", scanned)

    def test_a_new_base_topic_still_clears_the_documents(self):
        _scanned, cleared = self._move(new_base="hass_other")
        self.assertEqual(set(cleared), {f"{BASE}/demo/sensor/x", "homeassistant/device/hass_camp_dev/config"})


if __name__ == "__main__":
    unittest.main()
