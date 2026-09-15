"""MqttPublisher: manager commands, retained messages, _is_ours."""

import json
import unittest
import paho.mqtt.client as mqtt
from types import SimpleNamespace

from custom_components.integration_manager import discovery as disc
from custom_components.integration_manager.mqtt_publisher import MqttConfig, MqttPublisher

BASE = "hass_demo"


class FakeLoop:
    def __init__(self):
        self.calls = []

    def call_soon_threadsafe(self, cb):
        self.calls.append(cb)


class FakeManager:
    def __init__(self):
        self.actions = []

    async def async_action(self, action, rec=None):
        self.actions.append((action, rec))


def publisher(commands=True, manager=True):
    pub = object.__new__(MqttPublisher)
    pub.config = MqttConfig(manager_commands=commands)
    pub.history = []
    pub.manager = FakeManager() if manager else None
    pub._live_base = BASE
    pub._key_provider = lambda: BASE
    tasks = []
    pub.hass = SimpleNamespace(loop=FakeLoop(), async_create_task=tasks.append, tasks=tasks)
    return pub


def run_scheduled(pub):
    for cb in pub.hass.loop.calls:
        cb()
    for coro in pub.hass.tasks:
        try:
            coro.send(None)
        except StopIteration:
            pass


class ManagerCommandTest(unittest.TestCase):
    def assert_rejected(self, pub, error, answered):
        self.assertEqual(len(pub.history), 1)
        self.assertEqual(pub.history[0]["state"], "rejected")
        self.assertIn(error, pub.history[0]["error"])
        # a refusal with a payload is answered once on manager/result; the action itself never runs
        self.assertEqual(len(pub.hass.loop.calls), 1 if answered else 0)
        self.assertEqual(pub.manager.actions if pub.manager else [], [])

    def test_wrong_or_empty_payload(self):
        for payload in ("", "install", "RESTART"):
            pub = publisher()
            pub._on_manager_command("restart", payload)
            # an empty payload is what clearing a retained command looks like: no answer
            self.assert_rejected(pub, "payload must be 'restart'", answered=bool(payload))

    def test_unknown_action(self):
        pub = publisher()
        pub._on_manager_command("reboot", "restart")
        self.assert_rejected(pub, "unknown action 'reboot'", answered=True)

    def test_commands_off(self):
        pub = publisher(commands=False)
        pub._on_manager_command("backup", "backup")
        self.assert_rejected(pub, "manager_commands is off", answered=True)

    def test_no_manager(self):
        pub = publisher(manager=False)
        pub._on_manager_command("backup", "backup")
        self.assert_rejected(pub, "not set up", answered=False)

    def test_accepted(self):
        pub = publisher()
        pub._on_manager_command("install_integration", " install\n")
        self.assertEqual(pub.history[0]["state"], "running")
        self.assertEqual(len(pub.hass.loop.calls), 1)
        run_scheduled(pub)
        self.assertEqual(pub.manager.actions, [("install_integration", pub.history[0])])

    def test_handle_message_routes_manager_commands(self):
        pub = publisher()
        pub._handle_message(SimpleNamespace(topic=f"{BASE}/manager/cmd/check_updates", payload=b"check", retain=False))
        run_scheduled(pub)
        self.assertEqual([a for a, _ in pub.manager.actions], ["check_updates"])

    def test_retained_is_ignored(self):
        pub = publisher()
        with self.assertLogs("custom_components.integration_manager.mqtt_publisher", "WARNING"):
            pub._handle_message(SimpleNamespace(topic=f"{BASE}/manager/cmd/restart", payload=b"restart", retain=True))
        self.assertEqual(pub.history, [])
        self.assertEqual(pub.hass.loop.calls, [])


class ManagerResultTest(unittest.IsolatedAsyncioTestCase):
    async def test_result_on_a_dropped_connection_does_not_raise(self):
        pub = publisher()
        pub.manager = None
        pub._connected = True
        pub._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="hri-unit")  # never connected: publish() returns MQTT_ERR_NO_CONN

        async def executor(fn, *args):
            return fn(*args)

        pub.hass.async_add_executor_job = executor
        await pub.async_publish_manager_result({"action": "restart", "ok": True})


class IsOursTest(unittest.TestCase):
    def setUp(self):
        self.pub = object.__new__(MqttPublisher)

    def ours(self, topic, payload):
        if not isinstance(payload, bytes):
            payload = json.dumps(payload).encode()
        return self.pub._is_ours(topic, payload, BASE)

    def test_manager_document(self):
        self.assertTrue(self.ours(f"{BASE}/manager", {"updated_at": "x", "manager_version": "1.0.0"}))
        self.assertFalse(self.ours(f"{BASE}/manager", {"updated_at": "x"}))
        self.assertFalse(self.ours(f"{BASE}/manager", {"manager_version": "1.0.0"}))
        self.assertFalse(self.ours(f"{BASE}/manager", b"not json"))

    def test_manager_command(self):
        self.assertTrue(self.ours(f"{BASE}/manager/cmd/x", b"restart"))
        self.assertFalse(self.ours(f"{BASE}/manager/cmd/x", b""))

    def test_status(self):
        self.assertTrue(self.ours(f"{BASE}/status", b"online"))
        self.assertFalse(self.ours(f"{BASE}/status", b"hello"))

    def test_foreign(self):
        self.assertFalse(self.ours(f"{BASE}/something", {"foo": 1}))
        self.assertFalse(self.ours(f"{BASE}/something", [1, 2]))
        self.assertFalse(self.ours("homeassistant/device/x/config", {"origin": {"name": disc.origin(BASE + "_b_")["name"]}}))
        self.assertTrue(self.ours("homeassistant/device/x/config", {"origin": {"name": disc.origin(BASE + "_")["name"]}}))


if __name__ == "__main__":
    unittest.main()
