"""Third external review, MQTT side.

M2   A token URL (the camera proxy's ?token=) was left out only as a top-level string attribute: nested in a list or
     a dict, or as the state itself, it reached the retained document.
M12  The throwaway client's MQTT 5 attempt, the stop of the refused client and the MQTT 3.1.1 retry shared one
     deadline: a stop that took its time left the retry none, and the first scan of a 3.1.1 broker failed.
M14  The stale-document sweep spared <base>/manager only because its document happens to carry no published_at key.
M15  A paho client let go by _disconnect kept handing inbound commands to _on_message through the "offline" publish
     and its stop: the service ran, its answer went to a client that no longer exists.
"""

import json
import socket
import threading
import time
import unittest
from unittest import mock

import paho.mqtt.client as mqtt
from homeassistant.core import State

from custom_components.integration_manager import mqtt_publisher as mp
from tests import test_camp_publish as camp
from tests.test_e2e_pub_published import _Case

TOKEN = "t" * 64


# ----- M2 ----------------------------------------------------------------------------------------------------

class NestedTokenTest(_Case):
    def _doc(self, state, attrs):
        return self.pub.build_document(State("camera.front", state, attrs))[1]

    def test_a_token_url_inside_a_list_of_dicts_is_left_out(self):
        doc = self._doc("idle", {"sources": [{"name": "a", "entity_picture": f"/api/camera_proxy/camera.a?token={TOKEN}"},
                                             {"name": "b", "entity_picture": "/local/b.png"}]})
        self.assertNotIn(TOKEN, mp._dumps(doc))
        self.assertEqual(doc["attributes"]["sources"], [{"name": "a"}, {"name": "b", "entity_picture": "/local/b.png"}])

    def test_a_token_url_as_a_list_item_or_nested_access_token_is_left_out(self):
        doc = self._doc("idle", {"pictures": ["/local/x.png", f"/api/image_proxy/image.y?cache=1&access_token={TOKEN}"],
                                 "extra": {"access_token": TOKEN, "kept": 1, "deeper": {"url": f"/p?TOKEN={TOKEN}"}}})
        self.assertNotIn(TOKEN, mp._dumps(doc))
        self.assertEqual(doc["attributes"]["pictures"], ["/local/x.png"])
        self.assertEqual(doc["attributes"]["extra"], {"kept": 1, "deeper": {}})

    def test_a_state_that_is_a_token_url_is_masked(self):
        doc = self._doc(f"/api/camera_proxy/camera.front?token={TOKEN}&w=640", {})
        self.assertNotIn(TOKEN, mp._dumps(doc))
        self.assertEqual(doc["state"], "/api/camera_proxy/camera.front?token=***&w=640")

    def test_a_rotated_nested_token_does_not_change_the_document(self):
        first = self._doc("idle", {"sources": [{"entity_picture": f"/p?token={'a' * 64}"}]})
        second = self._doc("idle", {"sources": [{"entity_picture": f"/p?token={'b' * 64}"}]})
        for doc in (first, second):
            doc.pop("published_at")
            doc.pop("last_changed"), doc.pop("last_updated"), doc.pop("last_reported")
        self.assertEqual(first, second)

    def test_an_entity_without_a_token_is_published_as_before(self):
        attrs = {"friendly_name": "Front", "list": [1, "two", {"three": 3.0, "t": ("x", "y")}], "d": {"token": "plain"},
                 "entity_picture": "/local/front.png", "none": None}
        self.assertEqual(self._doc("idle", attrs)["attributes"], attrs)
        self.assertEqual(self._doc("idle", attrs)["state"], "idle")
        nested = attrs["list"]
        self.assertIs(mp._published_attributes(attrs)["list"], nested, "nothing to take out: no copy")


# ----- M12 ---------------------------------------------------------------------------------------------------

def _read_packet(conn):
    first = conn.recv(1)
    if not first:
        return None
    mult, length = 1, 0
    while True:
        b = conn.recv(1)[0]
        length += (b & 127) * mult
        mult *= 128
        if not b & 128:
            break
    body = b""
    while len(body) < length:
        chunk = conn.recv(length - len(body))
        if not chunk:
            break
        body += chunk
    return body


class Mqtt311Broker:
    """Answers an MQTT 5 CONNECT the way a 3.1.1 broker does (CONNACK code 1, then closes); accepts a 3.1.1 one."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.conns = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            body = _read_packet(conn)
            if body[6] == 5:  # protocol level after the protocol name
                conn.sendall(bytes([0x20, 2, 0, 1]))
                conn.close()
                return
            conn.sendall(bytes([0x20, 2, 0, 0]))
            while _read_packet(conn) is not None:
                pass
        except OSError:
            pass

    def close(self):
        self.sock.close()
        for c in self.conns:
            c.close()


class ThrowawayBudgetTest(unittest.TestCase):
    def test_a_slow_stop_of_the_refused_client_does_not_eat_the_retry(self):
        broker = Mqtt311Broker()
        self.addCleanup(broker.close)
        pub = camp._publisher(host="127.0.0.1", port=broker.port, force_base_topic=True)
        pub._learned_for = None
        real_stop = mp.MqttPublisher._stop_client
        stopped = []

        def slow_stop(c):
            if c.protocol == mqtt.MQTTv5:
                time.sleep(2.0)  # a stop that has to wait out its STOP_JOIN_S
            real_stop(c)
            stopped.append(c.protocol)

        with mock.patch.object(mp.MqttPublisher, "_stop_client", staticmethod(slow_stop)):
            started = time.monotonic()
            c = pub._throwaway_client("probe", "scan", time.monotonic() + 1.5)
            took = time.monotonic() - started
            slow_stop(c)
            for _ in range(60):
                if len(stopped) == 2:
                    break
                time.sleep(0.05)
        self.assertEqual(c.protocol, mqtt.MQTTv311)
        self.assertLess(took, 1.5)
        self.assertEqual(sorted(stopped), [mqtt.MQTTv311, mqtt.MQTTv5], "the refused client is still stopped")


# ----- M14 ---------------------------------------------------------------------------------------------------

class StaleSweepKeepsTheManagerTest(unittest.TestCase):
    def test_the_manager_document_survives_the_sweep_whatever_it_says(self):
        pub = camp._publisher(host="broker", force_base_topic=True)
        manager = json.dumps({"manager_version": "1", "updated_at": "now",
                              "last_action": {"error": "document without published_at"}}).encode()
        gone = json.dumps({"published_at": "now", "integration": "demo"}).encode()
        pub._retained_scan = lambda *a, **k: {f"{camp.BASE}/manager": manager, f"{camp.BASE}/demo/sensor/gone": gone}
        cleared = []
        pub._clear_topics = lambda suffix, topics: cleared.extend(topics)
        self.assertEqual(pub._clear_stale_docs(), 1)
        self.assertEqual(cleared, [f"{camp.BASE}/demo/sensor/gone"])


# ----- M15 ---------------------------------------------------------------------------------------------------

class ReleasedClientRunsNothingTest(unittest.TestCase):
    def test_a_command_read_while_the_client_is_let_go_is_not_handled(self):
        pub = camp._publisher(host="broker", force_base_topic=True)
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="x", protocol=mqtt.MQTTv5)
        c.on_message = pub._on_message
        pub._client = c
        pub._handle_message = mock.Mock()
        msg = mqtt.MQTTMessage(topic=f"{camp.BASE}/call/light/turn_on".encode())
        msg.payload = b"{}"

        def publish_offline(*_a, **_k):
            c._handle_on_message(msg)  # paho's thread reads a command while "offline" goes out
            return mock.Mock(wait_for_publish=lambda _t: True)

        c.publish = publish_offline
        with mock.patch.object(mp.MqttPublisher, "_stop_client", staticmethod(lambda cl: cl._handle_on_message(msg))):
            pub._disconnect(publish_offline=True)
        pub._handle_message.assert_not_called()

    def test_the_current_client_still_hands_commands_on(self):
        pub = camp._publisher(host="broker", force_base_topic=True)
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="x", protocol=mqtt.MQTTv5)
        c.on_message = pub._on_message
        pub._client = c
        pub._handle_message = mock.Mock()
        c._handle_on_message(mqtt.MQTTMessage(topic=f"{camp.BASE}/call/light/turn_on".encode()))
        pub._handle_message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
