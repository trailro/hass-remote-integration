"""Twelfth review, MQTT side.  m3: a SUBACK refusing the command topics left the connection "connected" with no error,
and paho's own log was never enabled.  Every test fails on the tree before its fix."""

import socket
import threading
import time
import unittest
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


if __name__ == "__main__":
    unittest.main()
