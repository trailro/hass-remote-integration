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


if __name__ == "__main__":
    unittest.main()
