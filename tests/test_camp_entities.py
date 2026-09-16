"""Campaign findings on the entity side of the bridge: a device change that
never reaches discovery, the device config left behind when a config entry
goes, unguarded command payloads, retained commands, and the availability
command-only platforms lost with their state topic."""

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from homeassistant.core import State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.integration_manager import discovery as disc
from custom_components.integration_manager import mqtt_publisher as mp

BASE = "hass_demo"


class Loop:
    """Enough of an event loop to see what a debounce armed."""

    def __init__(self):
        self.later = []

    def call_later(self, delay, cb):
        handle = SimpleNamespace(cancel=lambda: None)
        self.later.append((delay, cb, handle))
        return handle

    def call_soon_threadsafe(self, cb):
        cb()


def _publisher(**config):
    pub = object.__new__(mp.MqttPublisher)
    pub.config = mp.MqttConfig(**{"enabled": True, "discovery_enabled": True, **config})
    pub._connected, pub._moving, pub._stopping = True, False, False
    pub._live_base, pub._live_prefix, pub._key_provider = BASE, None, lambda: BASE
    pub._registry_timer = None
    pub.history = []
    pub.hass = mock.Mock()
    pub.hass.loop = Loop()
    # the tests assert on the call, so the mock stays; closing the coroutine keeps
    # "never awaited" warnings out of the run
    pub.hass.async_create_task = mock.Mock(side_effect=lambda coro, *a, **k: getattr(coro, "close", lambda: None)())
    return pub


class DeviceRegistryTest(unittest.IsolatedAsyncioTestCase):
    """D1: renaming a device changes only the discovery device block, so the
    entity-registry listener never hears about it."""

    async def test_start_subscribes_to_the_device_registry(self):
        pub = _publisher(enabled=False)
        pub._unsub, pub._republish_unsub = [], None
        pub.hass.async_add_executor_job = mock.AsyncMock(return_value=False)
        with mock.patch.object(mp, "async_track_time_interval"), mock.patch.object(mp, "async_dispatcher_connect"):
            await pub.async_start()
        listened = [call.args[0] for call in pub.hass.bus.async_listen.call_args_list]
        self.assertIn(dr.EVENT_DEVICE_REGISTRY_UPDATED, listened)

    def test_a_device_update_arms_the_discovery_refresh(self):
        pub = _publisher()
        event = SimpleNamespace(data={"action": "update", "device_id": "d1", "changes": {"name_by_user": None}})
        with mock.patch.object(mp.MqttPublisher, "_async_discovery_refresh", return_value="coro") as refresh:
            pub._on_device_registry(event)
            self.assertEqual(len(pub.hass.loop.later), 1)
            delay, cb, _ = pub.hass.loop.later[0]
            cb()
        self.assertEqual(delay, 3)  # the same debounce the entity registry uses
        refresh.assert_called_once_with()

    def test_nothing_is_armed_while_discovery_is_off(self):
        pub = _publisher(discovery_enabled=False)
        pub._on_device_registry(SimpleNamespace(data={"action": "update", "device_id": "d1"}))
        self.assertEqual(pub.hass.loop.later, [])


def _entry(entity_id="sensor.a"):
    return SimpleNamespace(entity_id=entity_id, domain=entity_id.split(".")[0], platform="demo", device_id="dev1",
                           disabled_by=None, disabled=False, capabilities=None, unit_of_measurement=None, name=None,
                           original_name="A", icon=None, original_icon=None, entity_category=None, device_class=None,
                           original_device_class=None, unique_id="a", area_id=None, labels=set(),
                           config_entry_id="e1", translation_key=None)


class LastEntityOfADeviceTest(unittest.TestCase):
    """D2: deleting a config entry takes the last entity of each of its
    devices; the device config must go with it, not become an empty one."""

    def setUp(self):
        pub = _publisher()
        pub.rules = mock.Mock()
        pub.rules.for_entity.return_value = {}
        pub.rules.apply_component.side_effect = lambda comp, rule: comp
        pub._collision_warned, pub._default_id_warned = set(), set()
        pub.hass.states.async_all.return_value = []
        pub.hass.config.components = {"demo"}
        pub._last_hash, pub.stats = {}, {"cleared": 0, "published": 0, "unchanged_skipped": 0}
        pub._orphan_sweep_due, pub._boot_components = False, None
        pub._topics = {"sensor.a": f"{BASE}/demo/sensor/a"}
        pub._pending_clears = set()
        self.did = f"{pub.prefix}dev1"
        pub._discovery_map = {self.did: {"sensor.a": {"platform": "sensor", "unique_id": f"{BASE}_sensor.a"}}}
        pub._blocks = {self.did: {"identifiers": [self.did], "name": "Device"}}
        self.published = []
        pub._publish = lambda topic, payload, retain=True, qos=None: self.published.append((topic, payload)) or True
        self.pub = pub

    def _remove(self):
        registry = mock.Mock(entities={})  # the entry is already gone when the event fires
        registry.async_get.return_value = None
        with mock.patch.object(er, "async_get", return_value=registry):
            self.pub._on_registry(SimpleNamespace(data={"action": "remove", "entity_id": "sensor.a"}))

    def test_the_device_config_is_cleared_and_forgotten(self):
        self._remove()
        topic = self.pub._discovery_topic(self.did)
        self.assertIn((topic, None), self.published)
        self.assertEqual([p for t, p in self.published if t == topic], [None])  # no empty config first
        self.assertNotIn(self.did, self.pub._discovery_map)
        self.assertNotIn(self.did, self.pub._blocks)

    def test_a_device_that_stays_keeps_getting_the_removal_form(self):
        self.pub._discovery_map[self.did]["sensor.b"] = {"platform": "sensor", "unique_id": f"{BASE}_sensor.b"}
        self._remove()
        topic = self.pub._discovery_topic(self.did)
        config = json.loads(next(p for t, p in self.published if t == topic))
        self.assertEqual(config["components"]["sensor_a"], {"platform": "sensor"})
        self.assertIn("sensor_b", config["components"])
        self.assertIn(self.did, self.pub._discovery_map)


class CommandPayloadTest(unittest.TestCase):
    """D3: the size/depth scan that protects service calls, on commands too."""

    def setUp(self):
        self.pub = _publisher()
        self.pub._topics = {"text.note": f"{BASE}/demo/text/note"}
        self.pub.stats = {"commands": 0, "last_command": ""}
        self.published = []
        self.pub._publish = lambda topic, payload, retain=True, qos=None: self.published.append((topic, payload)) or True

    def _send(self, payload, topic=f"{BASE}/cmd/text/note/value"):
        self.pub._handle_message(SimpleNamespace(topic=topic, payload=payload, retain=False))

    def test_an_oversized_command_is_refused(self):
        with self.assertLogs(mp._LOGGER, level="WARNING"):
            self._send(b"x" * (4 * 1024 * 1024))
        self.assertEqual(self.pub.history[-1]["state"], "rejected")
        self.assertEqual(self.pub.history[-1]["error"], "bad payload: larger than 256 KB")
        self.assertEqual(self.pub.hass.loop.later, [])
        self.pub.hass.async_create_task.assert_not_called()

    def test_a_deeply_nested_command_is_refused(self):
        with self.assertLogs(mp._LOGGER, level="WARNING"):
            self._send(b"[" * 100 + b"]" * 100)
        self.assertIn("nested deeper than", self.pub.history[-1]["error"])

    def test_an_ordinary_command_still_runs(self):
        with mock.patch.object(mp.disc, "command_to_service", return_value=("text", "set_value", {})):
            self._send(b"hello")
        self.assertEqual(self.pub.history[-1]["state"], "running")


class RetainedCommandTest(unittest.TestCase):
    """D7: a retained command is never executed from the retained store, and
    the topic must not stay on the broker to arrive again at every connect."""

    def setUp(self):
        self.pub = _publisher()
        self.published = []
        self.pub._publish = lambda topic, payload, retain=True, qos=None: self.published.append((topic, payload, qos)) or True

    def test_the_topic_is_cleared(self):
        topic = f"{BASE}/cmd/switch/main/state"
        with self.assertLogs(mp._LOGGER, level="WARNING"):
            self.pub._handle_message(SimpleNamespace(topic=topic, payload=b"ON", retain=True))
        self.assertEqual(self.published, [(topic, None, 1)])
        self.assertEqual(self.pub.history, [])  # still never executed

    def test_a_retained_manager_command_is_cleared_too(self):
        topic = f"{BASE}/manager/cmd/restart"
        with self.assertLogs(mp._LOGGER, level="WARNING"):
            self.pub._handle_message(SimpleNamespace(topic=topic, payload=b"restart", retain=True))
        self.assertEqual(self.published, [(topic, None, 1)])


class CommandOnlyAvailabilityTest(unittest.TestCase):
    """D4: button/scene/notify have no state to mirror, but MQTT availability
    does not depend on state_topic."""

    def _component(self, entity_id):
        hass = mock.Mock()
        registry = mock.Mock()
        registry.async_get.return_value = None
        with mock.patch.object(er, "async_get", return_value=registry):
            return disc.build_component(hass, State(entity_id, "unknown"), f"{BASE}/demo/{entity_id.replace('.', '/')}",
                                        f"{BASE}/cmd", f"{BASE}_")

    def test_both_availability_entries_survive(self):
        for entity_id in ("button.b", "scene.s", "notify.n"):
            with self.subTest(entity_id=entity_id):
                comp = self._component(entity_id)
                self.assertNotIn("state_topic", comp)
                self.assertEqual(len(comp["availability"]), 2)
                # the second entry is the entity's own document: it marks the
                # button unavailable when its source is
                self.assertEqual(comp["availability"][1]["topic"], f"{BASE}/demo/{entity_id.replace('.', '/')}")
                self.assertEqual(comp["availability_mode"], "all")


if __name__ == "__main__":
    unittest.main()
