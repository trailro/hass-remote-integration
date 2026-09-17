"""End-to-end campaign on 0.17.0, publisher side: a rename, an MQTT exclusion or a delete in the first five minutes after
a start (while the orphan sweep is due) came back on the main Home Assistant: the removal form went out, then the next
config of the device carried the old component again from what the previous process had announced."""

import collections
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from homeassistant.core import State
from homeassistant.helpers import entity_registry as er

from custom_components.integration_manager import discovery as disc
from custom_components.integration_manager import mqtt_publisher as mp
from custom_components.integration_manager.mqtt_rules import MqttRules

BASE = "hass_demo"


class Loop:
    """The debounces a registry event arms, run when the test says so."""

    def __init__(self):
        self.later = []

    def call_later(self, delay, cb, *args):
        handle = SimpleNamespace(cancel=lambda: self.later.remove(entry) if entry in self.later else None)
        entry = (delay, cb, args)
        self.later.append(entry)
        return handle

    def call_soon_threadsafe(self, cb, *args):
        cb(*args)


def _entry(entity_id):
    return SimpleNamespace(entity_id=entity_id, domain=entity_id.split(".")[0], platform="demo", device_id=None,
                           disabled_by=None, disabled=False, capabilities=None, unit_of_measurement="W", name=None,
                           original_name=entity_id, icon=None, original_icon=None, entity_category=None, device_class=None,
                           original_device_class="power", unique_id=entity_id, hidden=False, area_id=None, labels=set(),
                           config_entry_id="e1", translation_key=None, options={})


class Registry:
    """The entity registry of the container's HA: renames and deletes change what async_get answers."""

    def __init__(self, *ids):
        self.entities = {eid: _entry(eid) for eid in ids}

    def async_get(self, entity_id):
        return self.entities.get(entity_id)


class StartWindow(unittest.IsolatedAsyncioTestCase):
    """A process that started a moment ago: the previous one announced sensor.a and sensor.b on the demo device, the
    retained config says so (the boot components), and the orphan sweep is still five minutes away."""

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.registry = Registry("sensor.a", "sensor.b")
        self.states = {eid: State(eid, "5", {"unit_of_measurement": "W", "device_class": "power"}) for eid in self.registry.entities}
        pub = mp.MqttPublisher.__new__(mp.MqttPublisher)
        pub.config = mp.MqttConfig(enabled=True, discovery_enabled=True)
        pub._connected, pub._moving, pub._stopping = True, False, False
        pub._live_base, pub._live_prefix, pub._key_provider = BASE, None, lambda: BASE
        pub.rules = MqttRules(os.path.join(self.tmp, "mqtt_rules.json"))
        pub._collision_warned, pub._default_id_warned = set(), set()
        pub.hass = mock.Mock()
        pub.hass.data = {}
        pub.hass.loop = Loop()
        self.tasks = []
        pub.hass.async_create_task = self.tasks.append
        pub.hass.states.async_all.side_effect = lambda: list(self.states.values())
        pub.hass.states.get.side_effect = self.states.get
        pub.hass.config.components = {"demo"}
        pub.hass.is_running = True
        pub._last_hash, pub._blocks, pub._topics, pub._pending_clears = {}, {}, {}, set()
        pub.stats = collections.defaultdict(int)
        pub._health_last, pub.manager, pub._registry_timer, pub._services_timer = {}, None, None, None
        pub._discovery_map, pub._identity_sweep_due, pub._resync_excluded, pub._undiscover_due = {}, False, False, False
        pub._started_at, pub._last_full = time.time(), 0.0
        pub._orphan_sweep_due, pub._boot_removed = True, set()
        pub.publish_health = pub.publish_manager = pub._publish_manager_discovery = lambda *a, **k: None
        pub._publish_services = mock.AsyncMock()
        self.published = []
        pub._publish = lambda topic, payload, retain=True, qos=None: self.published.append((topic, payload)) or True
        self.pub = pub
        self.did, _block = disc.device_block(pub.hass, None, "demo", pub.prefix)
        self.patch = mock.patch.object(er, "async_get", return_value=self.registry)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        groups, _ = pub._group_by_device()
        pub._boot_components = {self.did: {mp._comp_key(eid): comp for eid, comp in groups[self.did][1].items()}}
        await pub.async_republish_all()  # the first full republish of this process: both announced
        self.assertEqual(set(self.config()["components"]), {"sensor_a", "sensor_b"})

    def config(self):
        topic = self.pub._discovery_topic(self.did)
        return json.loads([p for t, p in self.published if t == topic][-1])

    async def run_debounced(self):
        """The registry burst is over: the debounced discovery refresh runs (what the main HA gets 3 s later)."""
        while self.pub.hass.loop.later:
            _delay, cb, args = self.pub.hass.loop.later.pop(0)
            cb(*args)
        while self.tasks:
            await self.tasks.pop(0)

    def assert_removed_for_good(self, key):
        comps = self.config()["components"]
        self.assertNotIn("unique_id", comps.get(key, {}), f"{key} announced again: {comps.get(key)}")

    async def test_rename_in_the_window_stays_renamed(self):
        entry = self.registry.entities.pop("sensor.a")
        entry.entity_id = "sensor.renamed"
        self.registry.entities["sensor.renamed"] = entry
        self.states.pop("sensor.a")
        self.states["sensor.renamed"] = State("sensor.renamed", "5", {"unit_of_measurement": "W"})
        self.pub._on_registry(SimpleNamespace(data={"action": "update", "entity_id": "sensor.renamed", "old_entity_id": "sensor.a",
                                                    "changes": {"entity_id": "sensor.a"}}))
        self.assertEqual(self.config()["components"]["sensor_a"], {"platform": "sensor"})  # the removal form went out
        await self.run_debounced()
        self.assert_removed_for_good("sensor_a")
        self.assertIn("unique_id", self.config()["components"]["sensor_renamed"])
        await self.pub.async_republish_all()  # and at the next full republish, still in the window
        self.assert_removed_for_good("sensor_a")
        self.assertIn("unique_id", self.config()["components"]["sensor_b"])

    async def test_mqtt_exclude_in_the_window_stays_excluded(self):
        self.pub.rules.set("sensor.a", exclude=True)
        await self.pub.async_apply_rules()
        self.assert_removed_for_good("sensor_a")
        self.pub._on_registry(SimpleNamespace(data={"action": "update", "entity_id": "sensor.b", "changes": {"name": None}}))
        await self.run_debounced()
        self.assert_removed_for_good("sensor_a")
        self.assertIn("unique_id", self.config()["components"]["sensor_b"])

    async def test_excluding_the_integration_in_the_window_stays_excluded(self):
        new = mp.MqttConfig(enabled=True, discovery_enabled=True, exclude_integrations=["integration_manager", "demo"])
        self.pub._drop_newly_excluded(new)
        self.pub.config = new
        await self.pub.async_republish_all()
        topic = self.pub._discovery_topic(self.did)
        self.assertIsNone([p for t, p in self.published if t == topic][-1])  # the device config is cleared, never re-sent
        self.pub.config = mp.MqttConfig(enabled=True, discovery_enabled=True)  # included again: only what exists comes back
        self.registry.entities.pop("sensor.a")
        self.states.pop("sensor.a")
        await self.pub.async_republish_all()
        self.assertEqual(set(self.config()["components"]), {"sensor_b"})

    async def test_delete_in_the_window_stays_deleted(self):
        self.registry.entities.pop("sensor.a")
        self.states.pop("sensor.a")
        self.pub._on_registry(SimpleNamespace(data={"action": "remove", "entity_id": "sensor.a"}))
        self.assertEqual(self.config()["components"]["sensor_a"], {"platform": "sensor"})
        self.pub._on_registry(SimpleNamespace(data={"action": "update", "entity_id": "sensor.b", "changes": {"name": None}}))
        await self.run_debounced()
        self.assert_removed_for_good("sensor_a")
        await self.pub.async_republish_all()
        self.assert_removed_for_good("sensor_a")

    async def test_renamed_back_is_announced_again(self):
        await self.test_rename_in_the_window_stays_renamed()
        entry = self.registry.entities.pop("sensor.renamed")
        entry.entity_id = "sensor.a"
        self.registry.entities["sensor.a"] = entry
        self.states.pop("sensor.renamed")
        self.states["sensor.a"] = State("sensor.a", "5", {"unit_of_measurement": "W"})
        self.pub._on_registry(SimpleNamespace(data={"action": "update", "entity_id": "sensor.a", "old_entity_id": "sensor.renamed",
                                                    "changes": {"entity_id": "sensor.renamed"}}))
        await self.run_debounced()
        self.assertIn("unique_id", self.config()["components"]["sensor_a"])
        self.assert_removed_for_good("sensor_renamed")

    async def test_an_entity_still_setting_up_is_still_carried(self):
        """What the window is for: announced by the previous process, not here yet, and nothing removed it."""
        self.pub._discovery_map, self.pub._last_hash, self.published[:] = {}, {}, []  # a process that has not announced yet
        self.registry.entities["sensor.b"].platform = "slow"  # its integration has not loaded: no state, not announced
        self.states.pop("sensor.b")
        self.registry.entities["sensor.c"] = _entry("sensor.c")
        self.states["sensor.c"] = State("sensor.c", "5", {"unit_of_measurement": "W"})
        await self.pub.async_republish_all()
        self.assertIn("unique_id", self.config()["components"]["sensor_b"])
        self.pub.rules.set("sensor.a", exclude=True)
        await self.pub.async_apply_rules()
        self.assert_removed_for_good("sensor_a")
        self.assertIn("unique_id", self.config()["components"]["sensor_b"])  # a removal of another entity does not end it

    async def test_the_sweep_forgets_the_removals(self):
        await self.test_delete_in_the_window_stays_deleted()
        self.pub.config.discovery_enabled = False  # the sweep itself is covered elsewhere; here only its bookkeeping
        self.pub._retained_scan = lambda *a: {}
        self.pub.hass.async_add_executor_job = mock.AsyncMock(return_value={})
        await self.pub._async_sweep_orphans()
        self.assertEqual(self.pub._boot_removed, set())
