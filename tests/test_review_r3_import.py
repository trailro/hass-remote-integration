"""Fourth review, R3-03: an import never turned an entity back on.

Home Assistant ships some entities off - ``entity_registry_enabled_default =
False`` gives them ``disabled_by = INTEGRATION``: signal strength, diagnostics,
a device's spare channels.  An operator who wants one turns it on, which clears
the flag on their main instance, and the backup records it as ``null``.

The alignment knew how to *disable* (``disabled_by = USER``) and nothing else,
and it waited for the entity's first state - which a disabled entity never
produces.  Worse, the prune at every start read "map says on, registry says off
by the integration" as *satisfied* and threw the map entry away, so not even a
later alignment could find it.  After a cutover, or after a clean-start rebuild
following a Home Assistant downgrade, exactly those entities were missing on
the main instance with nothing said anywhere.

The registries here are Home Assistant's own, loaded from the Home Assistant
this container runs: ``disabled_by`` is the real enum, the create event is the
real one, and enabling an entity goes through the code the main instance runs.

The second half covers ``ha_import.apply`` with ``running`` set and
``align=True`` - every other test of it runs with both off, so the paths that
only exist there (the entry that does not load and the undo behind it, a store
that follows a re-numbered entry, a failing ``align_existing``) had none.
"""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import frame

from custom_components.integration_manager import ha_import

DISABLER = er.RegistryEntryDisabler


def _want(entity_id, **over):
    """One entry of the map the import builds from the backup's registry."""
    return {"entity_id": entity_id, "name": None, "icon": None, "disabled_by": None, "hidden_by": None, **over}


class RegistryCase(unittest.IsolatedAsyncioTestCase):
    """A real Home Assistant core with a real (empty) device and entity registry."""

    async def asyncSetUp(self):
        self.cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        os.makedirs(os.path.join(self.cfg, ha_import.STATE_DIR))
        self.hass = HomeAssistant(self.cfg)
        frame.async_setup(self.hass)
        dr.async_setup(self.hass)
        await dr.async_load(self.hass, load_empty=True)
        await er.async_load(self.hass, load_empty=True)
        self.reg = er.async_get(self.hass)
        self.addAsyncCleanup(self.hass.async_stop)

    def aligner(self, entities, domain="demo", start=True):
        with open(os.path.join(self.cfg, ha_import.MAP_FILE), "w", encoding="utf-8") as fh:
            json.dump({"domains": {domain: {"entities": entities, "devices": {}}}}, fh)
        al = ha_import.RegistryAligner(self.hass)
        if start:
            al.async_start()
        self.addCleanup(lambda: al._save_handle and al._save_handle.cancel())
        return al

    def create(self, unique_id="uid1", object_id="signal_strength", disabled_by=DISABLER.INTEGRATION, domain="sensor"):
        return self.reg.async_get_or_create(domain, "demo", unique_id, suggested_object_id=object_id,
                                            disabled_by=disabled_by)

    def entry_for(self, unique_id="uid1", domain="sensor"):
        eid = self.reg.async_get_entity_id(domain, "demo", unique_id)
        return self.reg.async_get(eid) if eid else None


class CreateEventTest(RegistryCase):
    """What the fix rests on: the registry does announce an entity it created disabled."""

    async def test_the_create_event_fires_for_an_entity_its_integration_ships_disabled(self):
        seen = []
        self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, lambda ev: seen.append(dict(ev.data)))
        entry = self.create()
        await self.hass.async_block_till_done()
        self.assertEqual([e["action"] for e in seen], ["create"])
        self.assertEqual(seen[0]["entity_id"], entry.entity_id)
        self.assertIs(self.entry_for().disabled_by, DISABLER.INTEGRATION)
        # and the reason the first-state path could never reach it
        self.assertIsNone(self.hass.states.get(entry.entity_id))


class EnableOnCreateTest(RegistryCase):
    async def test_an_entity_the_operator_had_enabled_is_enabled_as_it_is_created(self):
        al = self.aligner({"sensor:uid1": _want("sensor.signal_strength")})
        self.create()
        await self.hass.async_block_till_done()
        self.assertIsNone(self.entry_for().disabled_by)
        self.assertEqual(al.maps.get("demo", {}).get("entities"), {})
        self.assertEqual(al._pending, set())

    async def test_the_id_and_the_name_come_over_with_the_flag(self):
        self.aligner({"sensor:uid1": _want("sensor.hub_rssi", name="Hub signal", icon="mdi:wifi")})
        self.create(object_id="signal_strength")
        await self.hass.async_block_till_done()
        entry = self.entry_for()
        self.assertEqual((entry.entity_id, entry.name, entry.icon), ("sensor.hub_rssi", "Hub signal", "mdi:wifi"))
        self.assertIsNone(entry.disabled_by)

    async def test_an_entity_the_other_instance_had_off_is_still_turned_off(self):
        self.aligner({"sensor:uid1": _want("sensor.signal_strength", disabled_by="user")})
        entry = self.create(disabled_by=None)
        self.hass.states.async_set(entry.entity_id, "-64")  # it is enabled here, so a state does come
        await self.hass.async_block_till_done()
        self.assertIs(self.entry_for().disabled_by, DISABLER.USER)

    async def test_the_other_instances_own_integration_flag_is_left_where_it_is(self):
        self.aligner({"sensor:uid1": _want("sensor.signal_strength", disabled_by="integration")})
        self.create()
        await self.hass.async_block_till_done()
        self.assertIs(self.entry_for().disabled_by, DISABLER.INTEGRATION)

    async def test_a_flag_that_is_not_the_operators_is_not_undone(self):
        """CONFIG_ENTRY / DEVICE / HASS are not a choice anybody made about this
        entity, so an import must not clear them the way it clears INTEGRATION."""
        for flag in (DISABLER.CONFIG_ENTRY, DISABLER.DEVICE, DISABLER.HASS):
            with self.subTest(flag=flag):
                uid = f"uid-{flag.value}"
                self.aligner({f"sensor:{uid}": _want(f"sensor.{flag.value}_one")})
                self.create(unique_id=uid, object_id=f"{flag.value}_one", disabled_by=flag)
                await self.hass.async_block_till_done()
                self.assertIs(self.entry_for(uid).disabled_by, flag)

    async def test_an_entity_that_is_not_disabled_still_waits_for_its_first_state(self):
        """The create event comes before the platform has added the entity, so a
        rename there would leave the state machine on the old id - unchanged."""
        al = self.aligner({"sensor:uid1": _want("sensor.hub_rssi")})
        entry = self.create(disabled_by=None)
        await self.hass.async_block_till_done()
        self.assertEqual(self.entry_for().entity_id, "sensor.signal_strength")
        self.assertEqual(al._pending, {entry.entity_id})
        self.hass.states.async_set(entry.entity_id, "-64")
        await self.hass.async_block_till_done()
        self.assertEqual(self.entry_for().entity_id, "sensor.hub_rssi")


class RestartTest(RegistryCase):
    """A manager restarted after the entity was already in the registry: the
    create event is long gone, so the start prune and align_existing are all
    that is left of the map."""

    async def test_the_start_prune_no_longer_forgets_what_still_has_to_be_enabled(self):
        self.create()
        al = self.aligner({"sensor:uid1": _want("sensor.signal_strength")}, start=False)
        self.assertEqual(al.prune_satisfied(), 0)
        self.assertEqual(list(al.maps["demo"]["entities"]), ["sensor:uid1"])

    async def test_the_start_prune_still_drops_an_entity_that_is_already_right(self):
        self.create(disabled_by=None)
        al = self.aligner({"sensor:uid1": _want("sensor.signal_strength")}, start=False)
        self.assertEqual(al.prune_satisfied(), 1)
        self.assertEqual(al.maps["demo"]["entities"], {})

    async def test_align_existing_reaches_an_entity_that_has_no_state(self):
        self.create()
        al = self.aligner({"sensor:uid1": _want("sensor.hub_rssi", name="Hub signal")}, start=False)
        res = al.align_existing()
        self.assertEqual(res["entities"], 1)
        self.assertEqual(res["pending_entities"], 0)
        entry = self.entry_for()
        self.assertIsNone(entry.disabled_by)
        self.assertEqual((entry.entity_id, entry.name), ("sensor.hub_rssi", "Hub signal"))

    async def test_a_whole_start_enables_it_end_to_end(self):
        """What an operator sees after a cutover: HA starts, the integration has
        already put its entities in the registry, the manager comes up."""
        self.create()
        al = self.aligner({"sensor:uid1": _want("sensor.signal_strength")}, start=False)
        al.async_start()
        al._on_started(None)  # EVENT_HOMEASSISTANT_STARTED
        self.assertEqual(al.align_existing()["entities"], 1)
        self.assertIsNone(self.entry_for().disabled_by)


def _executor(fn, *args):
    async def run():
        return fn(*args)
    return run()


class ApplyRunningAlignTest(unittest.TestCase):
    """ha_import.apply with running set and align on."""

    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        self.src = os.path.join(self.cfg, ha_import.EXTRACT_DIR, ".storage")
        os.makedirs(self.src)
        self.storage = os.path.join(self.cfg, ".storage")
        os.makedirs(self.storage)
        self.write(self.src, "hub.e1", "from the backup")
        with open(os.path.join(self.src, "core.entity_registry"), "w", encoding="utf-8") as fh:
            json.dump({"data": {"entities": [{"entity_id": "sensor.hub_rssi", "platform": "hub", "config_entry_id": "e1",
                                              "unique_id": "u1", "name": "Hub signal", "disabled_by": None}]}}, fh)
        with open(os.path.join(self.src, "core.device_registry"), "w", encoding="utf-8") as fh:
            json.dump({"data": {"devices": [{"identifiers": [["hub", "d1"]], "config_entries": ["e1"],
                                             "name_by_user": "Hub", "disabled_by": None}]}}, fh)
        self.removed = []
        self.aligner = mock.Mock()
        self.aligner.align_existing.return_value = {"entities": 1, "devices": 1, "pending_entities": 0, "pending_devices": 0}

    def write(self, where, name, text):
        with open(os.path.join(where, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def hass(self, existing=None, loads=True):
        async def async_add(entry):
            # async_add sets the entry up; what this test needs of it is the state it leaves behind
            entry._async_set_state(hass, ConfigEntryState.LOADED if loads else ConfigEntryState.SETUP_ERROR,
                                   None if loads else "the hub refused the key")

        async def async_remove(entry_id):
            self.removed.append(entry_id)

        flow = SimpleNamespace(async_progress_by_handler=lambda *a, **k: [])
        config_entries = SimpleNamespace(async_entries=lambda _d=None: list(existing or []),
                                         async_get_entry=lambda i: next((e for e in (existing or []) if e.entry_id == i), None),
                                         async_add=async_add, async_remove=async_remove, flow=flow)
        hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg), config_entries=config_entries,
                               data={}, async_add_executor_job=lambda fn, *a: _executor(fn, *a))
        return hass

    def apply(self, hass, **over):
        summary = {"domains": {"hub": {"entries": [{"entry_id": "e1", "data": {}, "title": "Hub"}],
                                       "storage_files": ["hub.e1"]}}}
        kwargs = dict(align=True, copy_storage=True, running="hub", cleanup=False)
        kwargs.update(over)
        with mock.patch.object(ha_import, "load_summary", return_value=summary), \
                mock.patch.object(ha_import, "_forget_cached_stores"), \
                mock.patch.object(ha_import.loader, "async_get_integration",
                                  mock.AsyncMock(return_value=SimpleNamespace(is_built_in=False))):
            return asyncio.run(ha_import.apply(hass, self.aligner, "hub", "e1", None, None, **kwargs))

    def test_the_map_of_the_backups_registry_is_merged_before_the_entry_is_added(self):
        res = self.apply(self.hass())
        merged = self.aligner.merge_map.call_args[0][0]
        self.assertEqual(merged["domain"], "hub")
        self.assertEqual(merged["entities"], {"sensor:u1": {"entity_id": "sensor.hub_rssi", "name": "Hub signal",
                                                            "icon": None, "disabled_by": None, "hidden_by": None}})
        self.assertEqual(merged["devices"], {'["hub", "d1"]': {"name_by_user": "Hub", "disabled_by": None}})
        self.assertEqual(res["alignment"], self.aligner.align_existing.return_value)

    def test_an_entry_that_never_loads_is_removed_again_and_everything_it_touched_undone(self):
        self.write(self.storage, "hub.e1", "this volume's own")
        with self.assertRaises(ValueError) as caught:
            self.apply(self.hass(loads=False))
        self.assertIn("the hub refused the key", str(caught.exception))
        self.assertEqual(len(self.removed), 1)
        # the store this volume had is back, the imported one and its .pre-import gone
        self.assertEqual(os.listdir(self.storage), ["hub.e1"])
        with open(os.path.join(self.storage, "hub.e1"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "this volume's own")
        # and the map keys the import had merged do not stay pending for an entry that is not there
        self.aligner.drop_keys.assert_called_once_with("hub", ["sensor:u1"], ['["hub", "d1"]'])

    def test_a_store_named_after_a_taken_entry_id_follows_the_new_one(self):
        taken = SimpleNamespace(entry_id="e1", domain="hub", unique_id="other")
        res = self.apply(self.hass(existing=[taken]), allow_existing=True)
        self.assertEqual(len(res["copied_storage"]), 1)
        name = res["copied_storage"][0]
        self.assertTrue(name.startswith("hub."), name)
        self.assertNotEqual(name, "hub.e1")
        self.assertEqual(name, f"hub.{res['entry_id']}")
        self.assertTrue(os.path.isfile(os.path.join(self.storage, name)))

    def test_a_failing_align_existing_is_reported_and_does_not_undo_the_import(self):
        self.aligner.align_existing.side_effect = RuntimeError("registry busy")
        res = self.apply(self.hass())
        self.assertEqual(res["alignment_error"], "RuntimeError: registry busy")
        self.assertNotIn("alignment", res)
        self.assertEqual(res["copied_storage"], ["hub.e1"])
        self.aligner.drop_keys.assert_not_called()

    def test_a_reauth_the_entry_asks_for_is_not_read_as_a_failure(self):
        hass = self.hass(loads=False)
        hass.config_entries.flow.async_progress_by_handler = \
            lambda *a, **k: [{"context": {"source": "reauth", "entry_id": "e1"}}]
        res = self.apply(hass)
        self.assertEqual(self.removed, [])
        self.assertIn("not loaded yet", res["note"])


if __name__ == "__main__":
    unittest.main()
