"""Eleventh review, MQTT side: the secret masking ran in quadratic time on a run of backslashes (N1), on paho's network
thread before any payload check, so one payload held the bridge down; an escaped value without its closing quote
lost its escaping (N12); stopping a client joined a network thread that never ends while a QoS 1 publish waits for
an acknowledgement (N13).  Every test fails on the tree before the fix."""

import json
import socket
import subprocess
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import paho.mqtt.client as mqtt

from custom_components.integration_manager import mqtt_publisher as mp
from tests import test_camp_publish as camp

FAST_S = 1.0  # what the masking of the largest payload may take; the tree before the fix took minutes


def _seconds(statement: str, timeout: float = 20) -> float:
    """Wall-clock time of the statement in a process of its own: a quadratic regex cannot be interrupted in this one."""
    code = ("import json, time\nfrom custom_components.integration_manager import mqtt_publisher as mp\n"
            f"t0 = time.monotonic()\n{statement}\nprint(time.monotonic() - t0)")
    try:
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return float("inf")
    if out.returncode:
        raise AssertionError(out.stderr.strip())
    return float(out.stdout.strip().splitlines()[-1])


class MaskingTimeTest(unittest.TestCase):
    """N1: `(?:\\\\*["'])?` before the name and `(?!\\\\+").` in the value made every position of a backslash run scan the
    rest of the run: 16 000 backslashes took 3.5 s, a 256 KB payload minutes."""

    def test_nested_json_backslashes(self):
        self.assertLess(_seconds('mp._mask_codes(json.dumps({"x": "\\\\" * 60000}))'), FAST_S)

    def test_a_256_kb_payload(self):
        statement = 'text = json.dumps({"x": "\\\\" * ((mp.CALL_MAX_BYTES - 16) // 2)}); assert len(text) <= mp.CALL_MAX_BYTES\nmp._mask_codes(text)'
        self.assertLess(_seconds(statement), FAST_S)

    def test_backslashes_after_a_secret_key(self):
        self.assertLess(_seconds('mp._mask_codes("code: \\\\\\"" + "\\\\" * 200000)'), FAST_S)
        self.assertLess(_seconds('mp._mask_codes("code" + "\\\\" * 200000 + ": 1")'), FAST_S)

    def test_the_history_row_of_a_256_kb_call(self):
        statement = ('pub = object.__new__(mp.MqttPublisher); pub.history = []\n'
                     'pub._remember("call", "script.turn_on", json.dumps({"x": "\\\\" * ((mp.CALL_MAX_BYTES - 16) // 2)}))')
        self.assertLess(_seconds(statement), FAST_S)


class _Spy:
    def __init__(self, rule):
        self.rule, self.read = rule, []

    def sub(self, repl, text):
        self.read.append(len(text))
        return self.rule.sub(repl, text)


class BoundedMaskingTest(unittest.TestCase):
    """N1: the network thread reads a bounded slice of the text with the text rule; the parsed keys still cover all of it."""

    def test_the_text_rule_reads_a_slice_of_a_history_row(self):
        pub = camp._publisher()
        whole, cut = _Spy(mp._CODE_VALUE), _Spy(mp._CODE_VALUE_CUT)
        with mock.patch.object(mp, "_CODE_VALUE", whole), mock.patch.object(mp, "_CODE_VALUE_CUT", cut):
            rec = pub._remember("call", "script.turn_on", '{"\\u0063ode": "4321", "x": "' + "y" * 100000 + '"}')
            pub._remember("cmd", "text.x/value", "not json " * 20000)
        self.assertNotIn("4321", rec["data"])
        self.assertEqual(whole.read, [])
        self.assertTrue(cut.read and max(cut.read) <= mp.MASK_SCAN_CHARS, cut.read)

    def test_a_value_the_cut_left_open_is_masked_to_the_end(self):
        pad = "x" * (mp.MASK_SCAN_CHARS - 40)
        for opened in ('"', "'", '\\"'):
            with self.subTest(quote=opened):
                masked = mp._mask_codes(f"{pad} code: {opened}hunter2 and more words {'z ' * 100}{opened}", mp.MASK_SCAN_CHARS)
                self.assertNotIn("hunter2", masked)
                self.assertNotIn("more", masked)
                self.assertLessEqual(len(masked), mp.MASK_SCAN_CHARS)

    def test_short_text_is_masked_as_before(self):
        self.assertEqual(mp._mask_codes('{"code": "1234", "x": 1}', mp.MASK_SCAN_CHARS), '{"code": "***", "x": 1}')
        text = json.dumps({"entity_id": "script.x", "params": json.dumps({"code": "1234", "mode": "away"})})
        masked = mp._mask_codes(text, mp.MASK_SCAN_CHARS)
        self.assertEqual(json.loads(json.loads(masked)["params"]), {"code": "***", "mode": "away"})


class UnterminatedEscapedValueTest(unittest.TestCase):
    """N12: `code: \\"aaaa…` without its closing quote came back as `code: "***"`."""

    def test_masked_with_its_escaping(self):
        # to the end since the thirteenth review: an escaped quote no longer ends it, so where it ends is not known
        self.assertEqual(mp._mask_codes('script.x code: \\"' + "a" * 2000 + " tail"), 'script.x code: \\"***\\"')
        self.assertEqual(mp._mask_codes('x {\\"code\\": \\"' + "a" * 2000), 'x {\\"code\\": \\"***\\"')

    def test_a_closed_escaped_value_still_ends_at_its_quote(self):
        self.assertEqual(mp._mask_codes('x {\\"code\\": \\"12 34\\", \\"mode\\": \\"away\\"}'), 'x {\\"code\\": \\"***\\", \\"mode\\": \\"away\\"}')
        self.assertEqual(mp._mask_codes('x {\\\\\\"pin\\\\\\": \\\\\\"1234\\\\\\"}'), 'x {\\\\\\"pin\\\\\\": \\\\\\"***\\\\\\"}')


class _SilentBroker:
    """MQTT 3.1.1 on a loopback socket: accepts the connection and answers pings, never acknowledges a publish."""

    def __init__(self):
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.conn = None
        self.disconnected = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _read(self, n):
        data = b""
        while len(data) < n:
            chunk = self.conn.recv(n - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def _serve(self):
        self.conn, _ = self.server.accept()
        try:
            while True:
                kind = self._read(1)[0] >> 4
                length, shift = 0, 0
                while True:
                    byte = self._read(1)[0]
                    length |= (byte & 0x7F) << shift
                    shift += 7
                    if not byte & 0x80:
                        break
                self._read(length)
                if kind == 1:  # CONNECT
                    self.conn.sendall(b"\x20\x02\x00\x00")
                elif kind == 12:  # PINGREQ
                    self.conn.sendall(b"\xd0\x00")
                elif kind == 14:  # DISCONNECT
                    self.disconnected.set()
                    return
        except (EOFError, OSError):
            return

    def close(self):
        for s in (self.conn, self.server):
            if s is not None:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                s.close()


class StopClientTest(unittest.TestCase):
    """N13: loop_stop() joined paho's thread, which ends only once no QoS 1 message waits for its acknowledgement:
    after a cleanup the broker did not confirm, the stop never returned and held its executor thread (and the
    connection lock of a reconnect or an identity move) for good."""

    def _stopped_within(self, client, seconds):
        done = threading.Event()
        threading.Thread(target=lambda: (mp.MqttPublisher._stop_client(client), done.set()), daemon=True).start()
        return done.wait(seconds)

    def test_a_publish_the_broker_never_acknowledged(self):
        broker = _SilentBroker()
        try:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="hri-r11-stop", protocol=mqtt.MQTTv311)
            connected = threading.Event()
            c.on_connect = lambda *a: connected.set()
            c.connect("127.0.0.1", broker.port, keepalive=30)
            c.loop_start()
            self.assertTrue(connected.wait(5))
            info = c.publish("hass_camp/light/x", "", qos=1, retain=True)
            time.sleep(0.3)
            self.assertFalse(info.is_published())
            self.assertTrue(self._stopped_within(c, 5), "the stop never returned")
            self.assertTrue(broker.disconnected.wait(2), "no DISCONNECT reached the broker")
        finally:
            broker.close()  # before the fix: the lost connection is what finally ends the wedged thread

    def test_a_broker_that_does_not_even_read_the_disconnect(self):
        closed = threading.Event()
        client = SimpleNamespace(disconnect=lambda: 0, loop_stop=lambda: closed.wait(30),
                                 socket=lambda: SimpleNamespace(close=closed.set))
        with mock.patch.object(mp, "STOP_JOIN_S", 0.2, create=True):
            self.assertTrue(self._stopped_within(client, 2), "the stop never returned")
        self.assertTrue(closed.is_set())

    def test_the_connection_is_stopped_that_way(self):
        pub = camp._publisher()
        client = pub._client = mock.Mock()
        with mock.patch.object(mp.MqttPublisher, "_stop_client") as stop:
            pub._disconnect(publish_offline=False)
        stop.assert_called_once_with(client)


if __name__ == "__main__":
    unittest.main()
