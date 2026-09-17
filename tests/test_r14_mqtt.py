"""Fourteenth review, MQTT side.  N2: a text.set_value call by area, device, floor or label iterated the entity map on
paho's thread while the loop added entities to it: the RuntimeError escaped and the caller never got a result; any other
exception in the call path was silent too.  N3: the DEBUG line at the start of a call, and the warning of a failed call,
showed the value of a text entity in password mode.  a: a subscription refusal already reported was never reported
again, even for another broker.  b: the connect and subscribe errors of a connection outlived its deliberate end.
"online" published by a SUBACK (or its overdue timer) racing a deliberate disconnect went out after the retained
"offline", and stayed retained.  Every test fails on the tree before the fix, except the code field and the command of
DebugLogMaskingTest and the online of the live client, which must stay as they were."""

import asyncio
import json
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import mqtt_publisher as mp
from tests import test_camp_publish as camp
from tests.test_r12_mqtt import NOT_AUTHORIZED, SubscribeDoubleTest, _suback

BASE = camp.BASE
WAIT_S = 5


def _password_publisher():
    pub = camp._publisher()
    modes = {"text.pw": "password", "text.plain": "text"}
    pub.hass.states.get = lambda eid: SimpleNamespace(attributes={"mode": modes[eid]}) if eid in modes else None
    pub._topics = {"text.pw": "t1", "text.plain": "t2"}
    return pub


def _results(pub, service="text/set_value"):
    return [json.loads(p[1]) for p in pub._client.published if p[0] == f"{BASE}/result/{service}"]


class _GateKey(str):
    """An entity id whose first startswith() lets another thread run (the loop adding an entity) before it returns."""

    inside: threading.Event
    resume: threading.Event

    def startswith(self, *args):
        if not self.inside.is_set():
            self.inside.set()
            self.resume.wait(WAIT_S)
        return str.startswith(self, *args)


class CallByAreaWhileTheLoopAddsEntitiesTest(unittest.TestCase):
    """N2 with two threads: paho's runs the call, the loop publishes a new entity in the middle of the scan."""

    def test_the_call_is_answered(self):
        pub = _password_publisher()
        key = _GateKey("text.plain")
        key.inside, key.resume = threading.Event(), threading.Event()
        pub._topics = {key: "t2", "text.pw": "t1"}
        pub.hass.services.has_service = lambda d, s: False
        pub.hass.async_create_task = lambda coro: asyncio.run(coro)
        msg = SimpleNamespace(topic=f"{BASE}/call/text/set_value", retain=False,
                              payload=json.dumps({"area_id": "hall", "value": "hunter2", "_id": "a1"}).encode())
        paho = threading.Thread(target=pub._on_message, args=(None, None, msg), name="paho-double")
        with mock.patch.object(mp._LOGGER, "error") as logged, mock.patch.object(mp._LOGGER, "warning"):
            paho.start()
            self.assertTrue(key.inside.wait(WAIT_S))
            pub._topics["text.new"] = "t3"  # _publish_state, on the loop
            key.resume.set()
            paho.join(WAIT_S)
        self.assertFalse(paho.is_alive())
        logged.assert_not_called()
        self.assertEqual(_results(pub), [{"id": "a1", "service": "text.set_value", "ok": False, "error": "unknown service text.set_value"}])
        self.assertNotIn("hunter2", json.dumps(list(pub.history)))


class AnyExceptionIsAnsweredTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pub = _password_publisher()
        self.tasks = []

        def create(coro):
            task = asyncio.ensure_future(coro)
            self.tasks.append(task)
            return task

        self.pub.hass.async_create_task = create

    def _message(self, payload, service="text/set_value"):
        self.pub._on_message(None, None, SimpleNamespace(topic=f"{BASE}/call/{service}", payload=json.dumps(payload).encode(), retain=False))

    async def test_on_paho_s_thread(self):
        with mock.patch.object(mp.MqttPublisher, "_password_value", side_effect=RuntimeError("dictionary changed size during iteration")), \
                self.assertLogs(mp._LOGGER, "ERROR") as logs:
            self._message({"area_id": "hall", "value": "hunter2", "_id": 7})
        [result] = _results(self.pub)
        self.assertEqual((result["id"], result["service"], result["ok"]), (7, "text.set_value", False))
        self.assertIn("RuntimeError", result["error"])
        self.assertEqual(self.pub.history[-1]["state"], "error")
        self.assertNotIn("hunter2", "".join(logs.output) + json.dumps(list(self.pub.history)))

    async def test_on_the_loop(self):
        self.pub.hass.services.has_service = lambda d, s: True
        self.pub.hass.services.supports_response = mock.Mock(side_effect=RuntimeError("the service went away"))
        with mock.patch.object(mp.MqttPublisher, "_call_target_problem", return_value=None), self.assertLogs(mp._LOGGER, "ERROR"):
            self._message({"entity_id": "text.plain", "value": "x", "_id": "b2"})
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.assertEqual([t.exception() for t in self.tasks], [None])
        [result] = _results(self.pub)
        self.assertEqual((result["id"], result["ok"]), ("b2", False))
        self.assertIn("RuntimeError", result["error"])
        self.assertEqual(self.pub.history[-1]["state"], "error")
        # a retry of the same _id is answered from history, not left "running"
        self._message({"entity_id": "text.plain", "value": "x", "_id": "b2"})
        self.assertEqual(self.pub.history[-1]["state"], "duplicate")
        self.assertIn("RuntimeError", _results(self.pub)[-1]["error"])


class DebugLogMaskingTest(unittest.IsolatedAsyncioTestCase):
    """N3: DEBUG on, a password-mode text value and a code field."""

    def setUp(self):
        self.pub = _password_publisher()
        self.pub.stats.update(commands=0, last_command=None)
        self.pub._topics["lock.door"] = "t3"
        self.tasks = []

        def create(coro):
            task = asyncio.ensure_future(coro)
            self.tasks.append(task)
            return task

        self.pub.hass.async_create_task = create
        self.pub.hass.services.has_service = lambda d, s: True
        self.pub.hass.services.supports_response = lambda d, s: mp.SupportsResponse.NONE

    async def _logs(self, send, error):
        self.pub.hass.services.async_call = mock.AsyncMock(side_effect=error)
        with mock.patch.object(mp.MqttPublisher, "_call_target_problem", return_value=None), \
                self.assertLogs(mp._LOGGER, "DEBUG") as logs:
            send()
            await asyncio.gather(*self.tasks)
        return "\n".join(logs.output)

    async def test_a_password_text_call(self):
        output = await self._logs(lambda: self.pub._on_call("text/set_value", json.dumps({"entity_id": "text.pw", "value": "hunter2"})),
                                  ValueError("Value hunter2 for text.pw is too long"))
        self.assertIn("start (response=False) data=", output)
        self.assertIn("failed", output)
        self.assertNotIn("hunter2", output)

    async def test_a_code_field(self):
        output = await self._logs(lambda: self.pub._on_call("lock/unlock", json.dumps({"entity_id": "lock.door", "code": "4711"})), None)
        self.assertIn("start (response=False) data=", output)
        self.assertNotIn("4711", output)

    async def test_a_password_text_command(self):
        msg = SimpleNamespace(topic=f"{BASE}/cmd/text/pw/value", payload=b"hunter2", retain=False)
        output = await self._logs(lambda: self.pub._handle_message(msg), ValueError("Value hunter2 for text.pw is too long"))
        self.assertNotIn("hunter2", output)

    async def test_a_password_text_payload_on_another_field(self):
        """End-to-end run: a value published to text/<entity>/set (not /value) was rejected but kept in clear."""
        msg = SimpleNamespace(topic=f"{BASE}/cmd/text/pw/set", payload=b"hunter2", retain=False)
        output = await self._logs(lambda: self.pub._handle_message(msg), None)
        self.assertNotIn("hunter2", output)
        self.assertNotIn("hunter2", json.dumps(list(self.pub.history), default=str))
        self.assertNotIn("hunter2", str(self.pub.stats.get("last_command")))


class RefusalReportedAgainAfterADisconnectTest(unittest.TestCase):
    """a and b."""

    def _refused(self, pub, client):
        pub._client = client
        pub._on_connect(client, None, None, 0, None)
        pub._on_subscribe(client, None, 7, _suback(NOT_AUTHORIZED, 1, 1))

    def test_another_broker_reports_the_same_refusal(self):
        pub = camp._publisher()
        with mock.patch.object(mp.events, "emit") as emit, self.assertLogs(mp._LOGGER, "ERROR") as logs:
            self._refused(pub, SubscribeDoubleTest.Client())
            pub._disconnect()
            pub._live_base = BASE
            pub.config = mp.MqttConfig(host="other-broker")
            self._refused(pub, SubscribeDoubleTest.Client())
        self.assertEqual(len(logs.output), 2)
        self.assertEqual(len([c for c in emit.call_args_list if "refused the subscription" in c.args[1]]), 2)

    def test_a_clean_disconnect_clears_the_errors(self):
        pub = camp._publisher()
        with mock.patch.object(mp.events, "emit"), self.assertLogs(mp._LOGGER, "ERROR"):
            self._refused(pub, SubscribeDoubleTest.Client())
        self.assertTrue(pub.stats["subscribe_error"] and pub.stats["connect_error"])
        pub._disconnect()
        self.assertEqual((pub.stats["subscribe_error"], pub.stats["connect_error"]), ("", ""))

    def test_a_disconnect_that_was_never_connected_clears_them_too(self):
        pub = camp._publisher()
        pub._client = None
        pub.stats["connect_error"] = "base topic hass_x already carries 3 retained topics that are not ours"
        pub._disconnect()
        self.assertEqual(pub.stats["connect_error"], "")

    def test_an_error_disconnect_keeps_its_error(self):
        pub = camp._publisher()
        client = SubscribeDoubleTest.Client()
        pub._client = client
        with mock.patch.object(mp.events, "emit"), mock.patch.object(mp._LOGGER, "warning"):
            pub._on_disconnect(client, None, SimpleNamespace(is_disconnect_packet_from_server=False), 7, None)
        self.assertIn("reconnecting", pub.stats["connect_error"])


class _PahoLikeClient(SubscribeDoubleTest.Client):
    """Threaded paho, as far as the order on the wire goes: what another thread publishes is acknowledged only once the
    network thread's running callback returned (wait_for_publish), and nothing is sent after disconnect()."""

    def __init__(self, on_offline, callback_done):
        super().__init__()
        self.wire: list[tuple[str, str]] = []
        self.on_offline, self.callback_done = on_offline, callback_done

    def publish(self, topic, payload=None, qos=0, retain=False):
        info = super().publish(topic, payload, qos, retain)
        if self.wire is not None:
            self.wire.append((topic, payload))
        if payload == "offline":
            self.on_offline()
            info.wait_for_publish = lambda timeout=None: self.callback_done.wait(WAIT_S)
        return info

    def disconnect(self):
        self.wire = self.wire + [("DISCONNECT", None)]
        self.sent, self.wire = self.wire, None

    def retained_status(self):
        return [p for t, p in self.sent if t == f"{BASE}/status"][-1]


class _ReleaseGate:
    """The subscribing lock; the thread named `name` is held right after its first release until `resume` is set."""

    def __init__(self, name):
        self._lock, self.name = threading.Lock(), name
        self.released, self.resume = threading.Event(), threading.Event()

    def __enter__(self):
        self._lock.acquire()

    def __exit__(self, *exc):
        self._lock.release()
        if threading.current_thread().name == self.name and not self.released.is_set():
            self.released.set()
            self.resume.wait(WAIT_S)


class NoOnlineAfterACleanOfflineTest(unittest.TestCase):
    def _connected(self, gate_resume):
        pub = camp._publisher()
        callback_done = threading.Event()
        client = _PahoLikeClient(gate_resume.set, callback_done)
        pub._client = client
        with mock.patch.object(mp.events, "emit"):
            pub._on_connect(client, None, None, 0, None)
        return pub, client, callback_done

    def test_a_suback_racing_the_disconnect(self):
        gate = _ReleaseGate("paho-double")
        pub, client, callback_done = self._connected(gate.resume)
        pub._subscribing_lock = gate

        def suback():
            pub._on_subscribe(client, None, 7, _suback(1, 1, 1))
            callback_done.set()

        paho = threading.Thread(target=suback, name="paho-double")
        paho.start()
        self.assertTrue(gate.released.wait(WAIT_S))
        pub._disconnect()  # an executor thread: settings saved
        paho.join(WAIT_S)
        self.assertEqual(client.retained_status(), "offline", client.sent)

    def test_the_overdue_timer_racing_the_disconnect(self):
        resume = threading.Event()
        pub, client, callback_done = self._connected(resume)
        reported = threading.Event()

        def emit(*_args):  # _subscribe_failed, between the checks and "online"
            reported.set()
            resume.wait(WAIT_S)

        def overdue():
            pub._suback_overdue(client, 7)
            callback_done.set()

        loop = threading.Thread(target=overdue, name="loop-double")
        with mock.patch.object(mp.events, "emit", side_effect=emit), mock.patch.object(mp._LOGGER, "error"):
            loop.start()
            self.assertTrue(reported.wait(WAIT_S))
            pub._disconnect()
            loop.join(WAIT_S)
        self.assertEqual(client.retained_status(), "offline", client.sent)

    def test_online_still_follows_the_suback_of_the_live_client(self):
        pub, client, _ = self._connected(threading.Event())
        pub._on_subscribe(client, None, 7, _suback(1, 1, 1))
        self.assertEqual(client.wire, [(f"{BASE}/status", "online")])


if __name__ == "__main__":
    unittest.main()
