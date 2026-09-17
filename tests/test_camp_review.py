"""Test campaign: the pre-restore copy's protection, the pre-2026.9 device
registry shape, a unique id lost between change-report snapshots, and a
malformed entry id in the source backup."""

import asyncio
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import change_report, devices_page, ha_import
from custom_components.integration_manager.installer import Installer


class ProtectedPreRestoreTest(unittest.TestCase):
    def _installer(self, last_restore):
        inst = object.__new__(Installer)
        inst.state_dir = tempfile.mkdtemp()
        inst.state = SimpleNamespace(installed={}, rollback_backup=None)
        with open(os.path.join(inst.state_dir, "ha.json"), "w", encoding="utf-8") as fh:
            json.dump({"last_restore": last_restore}, fh)
        return inst

    @staticmethod
    def _ago(seconds):
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - seconds))

    def test_the_copy_taken_before_the_restore_is_protected(self):
        inst = self._installer({"ok": True, "at": self._ago(60), "backup": "src.zip", "pre_restore": "pre.zip"})
        self.assertEqual({"pre.zip"}, inst.protected_backups())  # the source needs nothing once restored (test_e2e_bak)

    def test_an_undated_copy_stays_protected(self):
        inst = self._installer({"ok": True, "backup": "src.zip", "pre_restore": "pre.zip"})
        self.assertIn("pre.zip", inst.protected_backups())

    def test_a_stale_copy_gives_its_keep_slot_back(self):
        inst = self._installer({"ok": True, "at": self._ago(30 * 86400), "backup": "src.zip", "pre_restore": "pre.zip"})
        self.assertNotIn("pre.zip", inst.protected_backups())


def _device(device_id):
    return SimpleNamespace(id=device_id, name=f"Device {device_id}", name_by_user=None, manufacturer="ACME", model="M",
                           model_id=None, serial_number=None, sw_version=None, hw_version=None,
                           identifiers={("hub", device_id)}, connections=set(), via_device_id=None, area_id=None,
                           config_entries={"e1"}, disabled_by=None)


class _OldRegistry:
    """``DeviceRegistry.devices`` before HA 2026.9 is the id -> entry mapping
    itself, so iterating it yields device ids; there are no child devices."""

    def __init__(self, devices):
        self._by_id = {d.id: d for d in devices}
        self.devices = self._by_id

    def async_get(self, device_id):
        return self._by_id.get(device_id)


class _NewRegistry(_OldRegistry):
    """HA 2026.9+: a collection of entries, child devices next to them."""

    def __init__(self, devices, children=()):
        super().__init__(devices)
        self._by_id.update({c.id: c for c in children})
        self.devices = list(devices)
        self.child_devices = list(children)


class RegistryShapeTest(unittest.TestCase):
    def _rows(self, registry):
        hass = SimpleNamespace(states=SimpleNamespace(get=lambda _eid: None))
        publisher = SimpleNamespace(prefix="hass", _discovery_topic=lambda did: f"hass/device/{did}/config")
        with mock.patch.object(devices_page.dr, "async_get", return_value=registry), \
                mock.patch.object(devices_page.er, "async_get", return_value=SimpleNamespace(entities={})), \
                mock.patch.object(devices_page.ar, "async_get", return_value=SimpleNamespace()), \
                mock.patch.object(devices_page.disc, "device_block", lambda *a: ("d", {})):
            return devices_page.device_rows(hass, publisher)

    def test_the_device_page_reads_the_old_registry(self):
        self.assertEqual([r["id"] for r in self._rows(_OldRegistry([_device("a"), _device("b")]))], ["a", "b"])

    def test_the_device_page_still_reads_the_new_registry(self):
        rows = self._rows(_NewRegistry([_device("a")], [_device("c")]))
        self.assertEqual([r["id"] for r in rows], ["a", "c"])

    def _aligned(self, registry):
        aligner = ha_import.RegistryAligner.__new__(ha_import.RegistryAligner)
        aligner.hass = SimpleNamespace(states=SimpleNamespace(get=lambda _eid: None))
        aligner.maps = {}
        aligner.prune_satisfied = lambda: 0
        aligner._save = lambda: None
        seen = []
        aligner.align_device = lambda device_id: bool(seen.append(device_id))
        with mock.patch.object(ha_import.dr, "async_get", return_value=registry), \
                mock.patch.object(ha_import.er, "async_get", return_value=SimpleNamespace(entities={})):
            aligner.align_existing()
        return seen

    def test_import_alignment_walks_the_old_registry(self):
        self.assertEqual(self._aligned(_OldRegistry([_device("a"), _device("b")])), ["a", "b"])

    def test_import_alignment_still_walks_the_new_registry(self):
        self.assertEqual(self._aligned(_NewRegistry([_device("a")], [_device("c")])), ["a", "c"])


def _entity(entity_id, **kw):
    return {"entity_id": entity_id, "name": entity_id, "unit_of_measurement": None, "device_class": None,
            "state_class": None, "entity_category": None, **kw}


class ChangeReportKeyShapeTest(unittest.TestCase):
    def _build(self, before, after):
        return change_report.build({"before": {"entities": before, "services": {}}}, {"entities": after, "services": {}})

    def test_an_entity_that_lost_its_unique_id_is_the_same_entity(self):
        report = self._build({"uid:sensor:u1": _entity("sensor.x")}, {"eid:sensor.x": _entity("sensor.x")})
        self.assertEqual((report["entities_added"], report["entities_removed"]), ([], []))
        self.assertFalse(report["breaking"])

    def test_an_entity_that_gained_a_unique_id_is_still_the_same_entity(self):
        report = self._build({"eid:sensor.x": _entity("sensor.x")}, {"uid:sensor:u1": _entity("sensor.x")})
        self.assertEqual((report["entities_added"], report["entities_removed"]), ([], []))

    def test_an_entity_that_really_went_away_is_still_reported(self):
        report = self._build({"uid:sensor:u1": _entity("sensor.x")}, {"eid:sensor.y": _entity("sensor.y")})
        self.assertEqual([e["entity_id"] for e in report["entities_removed"]], ["sensor.x"])
        self.assertTrue(report["breaking"])

    def test_two_entries_never_collapse_onto_one_key(self):
        # a before-snapshot of an older manager ("uid:<unique_id>") next to one entity that already
        # carries the current "uid:<domain>:<unique_id>" spelling of the same unique id
        report = self._build({"uid:u1": _entity("sensor.x"), "uid:sensor:u1": _entity("sensor.y")},
                             {"uid:sensor:u1": _entity("sensor.y")})
        self.assertEqual(report["entities_before"], 2)
        self.assertEqual([e["entity_id"] for e in report["entities_removed"]], ["sensor.x"])


class ImportBadEntryIdTest(unittest.TestCase):
    def _apply(self, entries):
        cfg = tempfile.mkdtemp()
        src = os.path.join(cfg, ha_import.EXTRACT_DIR, ".storage")
        os.makedirs(src)
        os.makedirs(os.path.join(cfg, ".storage"))
        for name in ("hub.e1", "hub_shared"):
            with open(os.path.join(src, name), "w", encoding="utf-8") as fh:
                fh.write("{}")
        summary = {"domains": {"hub": {"entries": entries, "storage_files": ["hub.e1", "hub_shared"]}}}

        async def executor(fn, *args):
            return fn(*args)

        config_entries = SimpleNamespace(async_entries=lambda _d=None: [], async_get_entry=lambda _i: None)

        async def async_add(_entry):
            return None

        config_entries.async_add = async_add
        hass = SimpleNamespace(config=SimpleNamespace(config_dir=cfg), config_entries=config_entries,
                               async_add_executor_job=executor)
        with mock.patch.object(ha_import, "load_summary", return_value=summary):
            return asyncio.run(ha_import.apply(hass, None, "hub", "e1", None, None, align=False, copy_storage=True,
                                               running=False, cleanup=False))

    def test_a_malformed_id_in_the_backup_does_not_drop_a_valid_entrys_store(self):
        res = self._apply([{"entry_id": "e1", "data": {}}, {"entry_id": ".", "data": {}}])
        self.assertEqual(res["copied_storage"], ["hub.e1", "hub_shared"])

    def test_a_second_valid_entry_still_keeps_its_own_store(self):
        res = self._apply([{"entry_id": "e1", "data": {}}, {"entry_id": "e2", "data": {}}])
        self.assertEqual(res["copied_storage"], ["hub.e1", "hub_shared"])


if __name__ == "__main__":
    unittest.main()
