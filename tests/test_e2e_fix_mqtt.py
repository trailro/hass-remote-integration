"""Defects an end-to-end run found on a live stack (mosquitto 2.0.22, HA 2026.9.3, paho 2.1.0).

A-F1: a refused login (wrong password) was reported as "disconnected (Unspecified error)": paho 2.1 follows a
refused CONNACK with on_disconnect, which overwrote the reason, and only the disconnection reached the timeline.
A-F2: a value sent to a text entity in password mode from the Services page reached the log through text's own
"Value <value> ... is too long" error; the MQTT path already masked it.
A-F3: "Authorization: Bearer <token>" in call data kept the token in the command history: the unquoted value ended at
the first space, so only the scheme word was masked.
A-minor-1: with MQTT off, clearing stale documents after a start still waited 5 s and warned.
A-minor-2: with manager_discovery off, every connect cleared a manager device config the main HA never received,
which made it warn "No device components to cleanup" each time.
"""

import asyncio
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from custom_components.integration_manager import mqtt_publisher as mp
from custom_components.integration_manager import services_page
from tests.test_r3_mqtt import BASE, _publisher
from tests.test_review_r4_conn import _connected_publisher


# ----- A-F1 -----------------------------------------------------------------------------------------

def _paho_callback_order(protocol, connack: bytes) -> list[tuple[str, str]]:
    """What paho 2.1 itself calls, and in which order, for a CONNACK read off the wire."""
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=protocol)
    calls = []
    client.on_connect = lambda c, u, flags, rc, props: calls.append(("connect", str(rc)))
    client.on_disconnect = lambda c, u, flags, rc, props: calls.append(("disconnect", str(rc)))
    client._in_packet = {"remaining_length": len(connack), "packet": connack}
    rc = client._handle_connack()
    client._loop_rc_handle(rc)  # what paho's loop does with that answer
    return calls


class PahoRefusalOrderTest(unittest.TestCase):
    """The premise of the fix, read from the paho the container runs."""

    def test_mqtt5_refusal_is_followed_by_an_unspecified_disconnection(self):
        self.assertEqual(_paho_callback_order(mqtt.MQTTv5, bytes([0, 0x87, 0])),
                         [("connect", "Not authorized"), ("disconnect", "Unspecified error")])

    def test_mqtt311_refusal_is_followed_by_an_unspecified_disconnection(self):
        self.assertEqual(_paho_callback_order(mqtt.MQTTv311, bytes([0, 5])),
                         [("connect", "Not authorized"), ("disconnect", "Unspecified error")])


class RefusalReasonKeptTest(unittest.TestCase):
    def setUp(self):
        self.pub = _connected_publisher()
        self.pub._client = mock.Mock()
        self.pub._refused = False
        emit = mock.patch.object(mp.events, "emit")
        self.emit = emit.start()
        self.addCleanup(emit.stop)

    def refuse_then_drop(self, refusal, times=1):
        dropped = ReasonCode(PacketTypes.DISCONNECT, "Unspecified error")
        with mock.patch.object(mp._LOGGER, "error"), mock.patch.object(mp._LOGGER, "warning"):
            for _ in range(times):
                self.pub._on_connect(self.pub._client, None, None, refusal)
                self.pub._on_disconnect(self.pub._client, None, mqtt.DisconnectFlags(False), dropped)

    def test_the_status_keeps_the_refusal_mqtt5(self):
        self.refuse_then_drop(ReasonCode(PacketTypes.CONNACK, identifier=0x87))
        self.assertIn("Not authorized", self.pub.stats["connect_error"])
        self.assertNotIn("Unspecified error", self.pub.stats["connect_error"])
        self.assertIn("refused the login", self.pub.stats["connect_error"])

    def test_the_status_keeps_the_refusal_mqtt311(self):
        self.refuse_then_drop(mqtt.convert_connack_rc_to_reason_code(5))
        self.assertIn("Not authorized", self.pub.stats["connect_error"])
        self.assertNotIn("Unspecified error", self.pub.stats["connect_error"])

    def test_one_readable_timeline_event_for_a_retry_loop(self):
        self.refuse_then_drop(ReasonCode(PacketTypes.CONNACK, identifier=0x87), times=30)
        self.assertEqual(self.emit.call_args_list, [mock.call("mqtt", "the broker refused the login: Not authorized")])

    def test_a_disconnection_after_an_accepted_connection_is_still_reported(self):
        self.refuse_then_drop(ReasonCode(PacketTypes.CONNACK, identifier=0x87))
        self.pub._refused = False  # what the accepted CONNACK does (its subscribe path is not under test here)
        self.pub._connected_at = time.monotonic() - 3600
        self.emit.reset_mock()
        with mock.patch.object(mp._LOGGER, "warning"):
            self.pub._on_disconnect(self.pub._client, None, mqtt.DisconnectFlags(False),
                                    ReasonCode(PacketTypes.DISCONNECT, "Unspecified error"))
        self.assertIn("disconnected (Unspecified error)", self.pub.stats["connect_error"])
        self.assertEqual(len(self.emit.call_args_list), 1)

    def test_an_accepted_connack_ends_the_refusal(self):
        self.refuse_then_drop(ReasonCode(PacketTypes.CONNACK, identifier=0x87))
        self.pub._refused = True
        self.pub._stopping = True  # accepted, then nothing else of the connect path runs
        self.pub._on_connect(self.pub._client, None, None, 0)
        self.assertFalse(self.pub._refused)


# ----- A-F2 -----------------------------------------------------------------------------------------

SECRET = "s3cr3t-" + "x" * 70


def _post(body):
    async def payload():
        return body

    return SimpleNamespace(content_type="application/json", json=payload)


class ServicesPagePasswordValueTest(unittest.IsolatedAsyncioTestCase):
    def view(self, mode="password"):
        async def call(domain, service, data, **kwargs):
            raise ValueError(f"Value {data['value']} for text.probe_demo_secret is too long")

        states = {"text.probe_demo_secret": SimpleNamespace(attributes={"mode": mode})}
        hass = SimpleNamespace(
            services=SimpleNamespace(has_service=lambda d, s: True,
                                     supports_response=lambda d, s: services_page.SupportsResponse.NONE, async_call=call),
            states=SimpleNamespace(get=states.get, async_entity_ids=lambda domain: list(states)),
            async_create_task=lambda coro, *a, **k: asyncio.get_running_loop().create_task(coro))
        view = services_page.ServiceCallView(hass)
        self.addCleanup(setattr, type(view), "_in_flight", 0)
        return view

    async def call(self, view, body):
        with self.assertLogs(services_page._LOGGER, "WARNING") as logs:
            answer = json.loads((await view.post(_post(body))).body)
            await asyncio.sleep(0)  # the done callback
        return answer, "\n".join(logs.output)

    async def test_the_value_stays_out_of_the_log(self):
        for body in ({"domain": "text", "service": "set_value", "data": {"entity_id": "text.probe_demo_secret", "value": SECRET}},
                     {"domain": "text", "service": "set_value", "data": {"value": SECRET},
                      "target": {"entity_id": ["text.probe_demo_secret"]}},
                     {"domain": "text", "service": "set_value", "data": {"value": SECRET}, "target": {"area_id": "attic"}}):
            with self.subTest(body=body):
                answer, log = await self.call(self.view(), body)
                self.assertNotIn(SECRET, log)
                self.assertIn("Value *** for text.probe_demo_secret is too long", log)
                # README: the result sent back to the caller keeps it (the caller sent the value)
                self.assertIn(SECRET, answer["error"])

    async def test_a_text_entity_not_in_password_mode_is_logged_as_it_is(self):
        _answer, log = await self.call(self.view(mode="text"), {
            "domain": "text", "service": "set_value", "data": {"entity_id": "text.probe_demo_secret", "value": SECRET}})
        self.assertIn(SECRET, log)


# ----- A-F3 -----------------------------------------------------------------------------------------

TOKEN = "eyJhbGciOiJIUzI1NiJ9.abcDEF123"


class BearerInHistoryTest(unittest.TestCase):
    SHAPES = (f"Authorization: Bearer {TOKEN}", f"authorization=Basic {TOKEN}", f"token: Bearer {TOKEN}",
              json.dumps({"auth": f"Bearer {TOKEN}"}), f"password: {TOKEN}", f"Authorization: Bearer\t{TOKEN}")

    def test_the_credential_after_the_scheme_is_masked(self):
        for shape in self.SHAPES:
            for data in (shape, {"message": shape}, json.dumps({"message": shape})):
                with self.subTest(data=data):
                    pub = _publisher()
                    rec = pub._remember("call", "hri_probe.fail", data)
                    self.assertNotIn(TOKEN, rec["data"])
                    self.assertIn("***", rec["data"])
                    self.assertNotIn(TOKEN, mp._mask_codes(data if isinstance(data, str) else json.dumps(data)))

    def test_what_follows_the_value_stays_readable(self):
        self.assertEqual(mp._mask_text(f"Authorization: Bearer {TOKEN}, retry in 5 s"), 'Authorization: "***", retry in 5 s')
        self.assertEqual(mp._mask_text(f"token: {TOKEN} expired"), 'token: "***" expired')

    def test_a_scheme_word_alone_is_a_value(self):
        self.assertEqual(mp._mask_text("token: Bearer, then"), 'token: "***", then')
        self.assertEqual(mp._mask_text("token: Basically fine"), 'token: "***" fine')

    def test_linear_on_adversarial_text(self):
        shapes = (lambda n: "token: Bearer" + " " * n + ",", lambda n: ("token=Bearer " * n)[:n],
                  lambda n: ("pw:Bearer," * n)[:n], lambda n: "authorization: Basic " + "A" * n,
                  lambda n: "password: " + "Bearer " * (n // 7), lambda n: ("a:Basic\t" * n)[:n])
        for make in shapes:
            timings = []
            for n in (8192, 16384):
                text = make(n)
                started = time.perf_counter()
                for _ in range(5):
                    mp._mask_text(text)
                timings.append(time.perf_counter() - started)
            with self.subTest(sample=make(40)):
                self.assertLess(timings[1], 0.5)
                self.assertLess(timings[1] / max(timings[0], 1e-6), 4)  # twice the text, about twice the time


# ----- A-minor-1 ------------------------------------------------------------------------------------

class StaleDocsWithMqttOffTest(unittest.IsolatedAsyncioTestCase):
    async def test_mqtt_off_answers_at_once_and_quietly(self):
        pub = _publisher()
        pub._connected = False
        pub.config.enabled = False
        pub._clear_stale_docs = mock.Mock(return_value=7)
        loop = asyncio.get_running_loop()
        started = loop.time()
        with self.assertNoLogs(mp._LOGGER, "WARNING"):
            self.assertEqual(await pub.async_clear_stale_docs(), 0)
        self.assertLess(loop.time() - started, 0.2)
        pub._clear_stale_docs.assert_not_called()


# ----- A-minor-2 ------------------------------------------------------------------------------------

MANAGER = f"{BASE}_manager"
TOPIC = f"homeassistant/device/{MANAGER}/config"


class ManagerDeviceClearTest(unittest.IsolatedAsyncioTestCase):
    def publisher(self, announced, manager_discovery=False):
        pub = _publisher()
        pub.config.discovery_prefix, pub.config.discovery_enabled = "homeassistant", False
        pub.config.manager_discovery = manager_discovery
        pub._connected, pub._manager_absent_sent = True, False
        pub._manager_announced = announced
        pub._discovery_map, pub._blocks, pub._last_hash = {}, {}, {}
        pub._orphan_sweep_due, pub._boot_components, pub._live_prefix = False, None, BASE + "_"
        pub.stats = {"unchanged_skipped": 0}
        pub._manager_discovery = lambda: (MANAGER, {"identifiers": [MANAGER]}, {"sensor.health": {"platform": "sensor"}})
        pub._publish = mock.Mock(return_value=True)
        self.tasks = []
        pub.hass.async_create_background_task = lambda coro, name: self.tasks.append(asyncio.ensure_future(coro))
        pub.hass.async_add_executor_job = mock.AsyncMock(side_effect=lambda f, *a: f(*a))
        write = mock.patch.object(mp.writer, "write_nowait")
        self.write = write.start()
        self.addCleanup(write.stop)
        return pub

    def clears(self, pub):
        return [c for c in pub._publish.call_args_list if c.args[:2] == (TOPIC, None)]

    async def test_nothing_announced_nothing_cleared(self):
        pub = self.publisher(frozenset())
        for _ in range(3):  # every connect
            pub._manager_absent_sent = False
            pub._publish_manager_discovery()
        self.assertEqual(self.clears(pub), [])

    async def test_announced_is_cleared_once_and_forgotten(self):
        pub = self.publisher(frozenset({TOPIC}))
        pub._publish_manager_discovery()
        self.assertEqual(len(self.clears(pub)), 1)
        self.assertEqual(pub._manager_announced, frozenset())
        self.write.assert_called_once()
        self.assertEqual(self.write.call_args.args[1], {"announced": []})
        pub._manager_absent_sent = False  # the next connect
        pub._publish_manager_discovery()
        self.assertEqual(len(self.clears(pub)), 1)

    async def test_switched_on_then_off_removes_the_device(self):
        pub = self.publisher(frozenset(), manager_discovery=True)
        pub._publish_manager_discovery()
        self.assertEqual(pub._manager_announced, frozenset({TOPIC}))
        self.assertEqual(self.write.call_args.args[1], {"announced": [TOPIC]})
        pub.config.manager_discovery = False
        pub._publish_manager_discovery()
        self.assertEqual(len(self.clears(pub)), 1)

    async def test_without_a_record_the_broker_decides(self):
        for retained, cleared in (({}, 0), ({TOPIC: b'{"device": {}}'}, 1)):
            with self.subTest(retained=bool(retained)):
                pub = self.publisher(None)
                pub._retained_scan = mock.Mock(return_value=retained)
                pub._publish_manager_discovery()
                await asyncio.gather(*self.tasks)
                pub._retained_scan.assert_called_once_with("mgr", [(TOPIC, 1)])
                self.assertEqual(len(self.clears(pub)), cleared)
                self.assertEqual(pub._manager_announced, frozenset())

    async def test_a_scan_that_fails_clears_as_before(self):
        pub = self.publisher(None)
        pub._retained_scan = mock.Mock(side_effect=OSError("refused"))
        pub._publish_manager_discovery()
        await asyncio.gather(*self.tasks)
        self.assertEqual(len(self.clears(pub)), 1)

    def test_the_record_survives_a_restart(self):
        pub = _publisher()
        cfg = tempfile.mkdtemp()
        os.makedirs(os.path.join(cfg, "integration_manager"))
        pub.hass.config.path = lambda *p: os.path.join(cfg, *p)
        self.assertIsNone(pub._read_manager_announced())
        with open(pub._manager_announced_file(), "w") as f:
            json.dump({"announced": [TOPIC]}, f)
        self.assertEqual(pub._read_manager_announced(), frozenset({TOPIC}))


if __name__ == "__main__":
    unittest.main()
