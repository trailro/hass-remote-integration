"""External review at 70c3e8c, MQTT side (paho 2.1.0 and aiohttp as installed in the container).

MQTT-1  a hand-edited mqtt.json with "qos": 1.0, "port": 8883.0 or true passed _sane: 1.0 == 1 and true == 1.
        REAL: paho's publish() range check takes 1.0, then _send_publish computes `qos << 1` and raises TypeError
        for every document on a live socket; socket.getaddrinfo refuses the float port ("Int or String expected").
        Fixed: only an int is kept, anything else falls back to the default with the warning the README promises.
MQTT-2  a retained command refused while the clear could not go out (an identity move) still armed the echo
        window, so the next genuine empty command on that topic was swallowed as our own echo.  REAL, narrow.
MQTT-3  two containers running the same integration on one broker share client id, topics and origin.  REAL and
        by design: the base topic is hass_<domain>, not a setting.  Documentation, plus the disconnect hint now
        names the client id.  mosquitto's MQTT 5 "session taken over" carries no properties, and paho 2.1 reads a
        DISCONNECT reason only when remaining_length > 2, so that reason code never reaches on_disconnect.
MQTT-4  a pending cleanup keyed with the broker user (and TLS) never matched again once either changed, although
        _recorded_elsewhere and the README say host and port decide.  REAL.  The key is (base, host, port) now;
        the file format did not change, so older files read into the new key as they are.
Cutover an enable with no main Home Assistant configured answered like a checked one.  Now says checked: false.

Every test of a fix fails on the tree before it; the ones whose docstring or name says "pinned", "still" or "kept"
hold either way and pin what must not change.
"""

import asyncio
import json
import os
import shutil
import socket
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from custom_components.integration_manager import mqtt_publisher as mp
from custom_components.integration_manager import parity
from tests import test_review_mqttfix as fix
from tests import test_view_handlers as views
from tests.test_r13_mqtt import KEPT, OURS, _Broker, _Case
from tests.test_r9_mqtt import _call, _parent


# ----- MQTT-1: a number that only compares equal to an int -------------------------------------------

class HandEditedNumbersTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.pub = object.__new__(mp.MqttPublisher)
        self.pub.path = os.path.join(self.dir, "mqtt.json")

    def load(self, **values):
        with open(self.pub.path, "w", encoding="utf-8") as fh:
            json.dump(values, fh)
        return self.pub._load()

    def test_a_float_or_a_switch_falls_back_to_the_default_with_a_warning(self):
        for name, value in (("qos", 1.0), ("qos", True), ("port", 8883.0), ("port", True),
                            ("republish_interval_s", 600.0), ("full_republish_interval_min", 90.0)):
            with self.subTest(name=name, value=value):
                with self.assertLogs(mp._LOGGER, "WARNING"):
                    config = self.load(**{name: value})
                default = mp.MqttConfig.__dataclass_fields__[name].default
                self.assertIs(type(getattr(config, name)), int)
                self.assertEqual(getattr(config, name), default)

    def test_an_int_in_range_is_kept_without_a_warning(self):
        with self.assertNoLogs(mp._LOGGER, "WARNING"):
            config = self.load(qos=1, port=8883, republish_interval_s=600, full_republish_interval_min=90)
        self.assertEqual((config.qos, config.port, config.republish_interval_s, config.full_republish_interval_min), (1, 8883, 600, 90))

    def test_what_is_loaded_goes_through_the_real_paho(self):
        """What the float did before the fix, on paho itself: every QoS 1 document raised TypeError."""
        config = self.load(qos=1.0, port=8883.0)
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="x", protocol=mqtt.MQTTv5)
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        client._sock = ours  # a live socket: paho only builds the packet header when it can send it
        self.assertEqual(client.publish("hass_x/a", "{}", qos=config.qos).rc, mqtt.MQTT_ERR_SUCCESS)
        socket.getaddrinfo("localhost", config.port)  # raised OSError("Int or String expected") for 8883.0


# ----- MQTT-2: the echo window for a clear that never went out -------------------------------------

class ClearNotSentArmsNoEchoTest(unittest.TestCase):
    def test_a_real_empty_command_after_a_retained_one_refused_during_a_move_runs(self):
        async def run():
            calls = []
            pub = fix._publisher(asyncio.get_running_loop(), calls, qos=1)  # MQTT 3.1.1: no noLocal, the echo window applies
            pub._topics = {"text.note": f"{fix.BASE}/x/text/note"}
            pub.hass.states.get.return_value = mock.Mock(attributes={})
            topic = f"{fix.BASE}/cmd/text/note/value"
            pub._moving = True  # an identity move: _publish sends nothing
            with self.assertLogs(mp._LOGGER, "WARNING"):
                pub._handle_message(fix.Msg(topic, "hello", retain=True))
            pub._moving = False
            self.assertEqual(pub._client.published, [])  # the clear did not go out, so no echo is coming
            pub._handle_message(fix.Msg(topic, ""))  # the main HA blanking the text entity
            for _ in range(6):
                await asyncio.sleep(0)
            return pub, calls
        pub, calls = asyncio.run(run())
        self.assertEqual(calls, [("text", "set_value", {"entity_id": "text.note", "value": ""})])
        self.assertEqual(pub._cleared_cmds, {})


# ----- MQTT-3: the same client id from a second container ------------------------------------------

class SameClientIdHintTest(unittest.TestCase):
    def test_a_drop_right_after_the_connack_names_the_client_id(self):
        pub = fix._publisher()
        pub.stats.update(subscribe_error="", protocol=None)
        pub._client.protocol = mqtt.MQTTv5
        pub._stale_client = lambda client: False
        pub._connected_at = time.monotonic() - 2  # the other container reconnected two seconds later
        normal = ReasonCode(PacketTypes.DISCONNECT, "Normal disconnection")  # what paho makes of mosquitto's 0x8E
        with self.assertLogs(mp._LOGGER, "WARNING"), mock.patch.object(mp.events, "emit") as emit:
            pub._on_disconnect(pub._client, None, SimpleNamespace(is_disconnect_packet_from_server=True), normal)
        for text in (pub.stats["connect_error"], emit.call_args.args[1]):
            self.assertIn("closed the connection", text)
            self.assertIn(f"client id {fix.BASE}", text)
            self.assertIn("same integration on this broker", text)

    def test_paho_drops_a_disconnect_reason_without_properties(self):
        """Pinned: why the reason code is no signal - mosquitto sends 0x8E alone (remaining length 1)."""
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="x", protocol=mqtt.MQTTv5)
        seen = []
        client.on_disconnect = lambda c, u, flags, reason, props: seen.append(str(reason))
        client._in_packet = {"remaining_length": 1, "packet": bytes([0x8E])}
        client._handle_disconnect()
        self.assertNotEqual(seen, ["Session taken over"])


# ----- MQTT-4: host and port decide which broker a pending removal waits for -------------------------

class PendingKeyIsHostAndPortTest(_Case):
    async def _changed(self, **config):
        await self.fail_uninstall()  # the user and TLS of the uninstall are recorded with it
        pub = self.publisher(**config)  # a restart on the same broker with another user / TLS
        self.assertEqual(pub.retained_cleanup_pending()[0]["other_broker"], False)
        self.assertNotIn("waiting for the MQTT settings", pub.retained_cleanup_pending()[0]["error"])
        broker = _Broker({**OURS, **KEPT})
        with mock.patch.object(mp.MqttPublisher, "_throwaway_client", lambda self, *a: broker.client(*a)):
            await pub._on_cleanup_timer(None)
        self.assertEqual(sorted(broker.cleared), sorted(OURS))
        self.assertEqual(self.on_disk(), {})
        self.assertEqual(pub._cleanup_pending, {})

    async def test_another_user_on_the_same_broker_runs_it(self):
        await self._changed(username="b")

    async def test_tls_turned_on_for_the_same_broker_runs_it(self):
        await self._changed(tls=True)

    async def test_another_port_still_waits(self):
        await self.fail_uninstall()
        pub = self.publisher(port=self.port + 1 if self.port < 65535 else self.port - 1)
        (entry,) = pub.retained_cleanup_pending()
        self.assertTrue(entry["other_broker"])
        self.assertIn("waiting for the MQTT settings", entry["error"])


# ----- the cutover enable with no main Home Assistant to check ------------------------------------

class UncheckedEnableTest(unittest.TestCase):
    def test_no_parent_configured_says_unchecked(self):
        res, emitted = _call(views._view(parent=False), "enable")
        self.assertTrue(res["ok"])
        self.assertIs(res["forced"], False)
        self.assertIs(res["checked"], False)
        self.assertIs(emitted[-1][1]["checked"], False)
        self.assertIn("unchecked", emitted[-1][0][1])

    def test_a_checked_enable_and_a_forced_one(self):
        client = _parent([[{"components": ["mqtt"]}], parity.ParentCommandFailed("config_entries/get: Unknown command."), [[]]])
        with mock.patch.object(parity, "_parent_client", return_value=client):
            res, emitted = _call(views._view(), "enable")
        self.assertIs(res["checked"], True)
        self.assertNotIn("checked", emitted[-1][0][1])
        res, emitted = _call(views._view(), "enable", {"force": True})
        self.assertIs(res["checked"], False)
        self.assertIn("forced", emitted[-1][0][1])
        self.assertNotIn("unchecked", emitted[-1][0][1])


if __name__ == "__main__":
    unittest.main()
