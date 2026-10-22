"""The registries an integration shares with every other one, merged per domain when the environment builder's
combined change goes back (backupkit.merge_domain_store): the integration's items as the switch's backup has them,
every other item as it is now, in a schema the older Home Assistant reads."""

import json
import os
import shutil
import tempfile
import unittest
import zipfile

import backupkit


def _doc(data, version=1, minor=1, key="x"):
    return json.dumps({"version": version, "minor_version": minor, "key": key, "data": data}).encode()


ENTRIES_OLD = {"entries": [{"entry_id": "d1", "domain": "demo", "version": 2, "data": {"v": "A"}},
                           {"entry_id": "m1", "domain": "mqtt", "data": {"broker": "before"}}]}
ENTRIES_LIVE = {"entries": [{"entry_id": "m1", "domain": "mqtt", "data": {"broker": "now"}},
                            {"entry_id": "d1", "domain": "demo", "version": 3, "data": {"v": "B"}},
                            {"entry_id": "d2", "domain": "demo", "data": {"made by": "B"}},
                            {"entry_id": "i1", "domain": "integration_manager", "data": {}}]}
MINE = {"d1", "d2"}


def merge(name, live, older, domain="demo", mine=MINE):
    raw, note = backupkit.merge_domain_store(name, live, older, domain, mine)
    return json.loads(raw), note


class ConfigEntriesTest(unittest.TestCase):
    def test_the_domain_from_the_backup_the_rest_live(self):
        doc, note = merge(".storage/core.config_entries", _doc(ENTRIES_LIVE), _doc(ENTRIES_OLD))
        self.assertEqual(note, "")
        self.assertEqual({e["entry_id"]: e["data"] for e in doc["data"]["entries"]},
                         {"m1": {"broker": "now"}, "i1": {}, "d1": {"v": "A"}})  # d2 was made by the version that is left

    def test_a_live_file_of_a_newer_major_comes_back_whole_and_says_so(self):
        doc, note = merge(".storage/core.config_entries", _doc(ENTRIES_LIVE, version=2), _doc(ENTRIES_OLD))
        self.assertEqual(doc["version"], 1)
        self.assertEqual({e["entry_id"] for e in doc["data"]["entries"]}, {"d1", "m1"})
        self.assertIn("newer major version", note)

    def test_a_newer_minor_keeps_the_live_items_and_version(self):
        doc, _ = merge(".storage/core.config_entries", _doc(ENTRIES_LIVE, minor=5), _doc(ENTRIES_OLD, minor=3))
        self.assertEqual((doc["version"], doc["minor_version"]), (1, 5))
        self.assertIn("i1", {e["entry_id"] for e in doc["data"]["entries"]})


class EntityRegistryTest(unittest.TestCase):
    OLD = {"entities": [{"entity_id": "sensor.d", "platform": "demo", "config_entry_id": "d1", "name": "A"},
                        {"entity_id": "switch.helper", "platform": "template", "config_entry_id": "d1", "name": "A helper"},
                        {"entity_id": "sensor.m", "platform": "mqtt", "config_entry_id": "m1", "name": "before"}],
           "deleted_entities": [{"entity_id": "sensor.gone", "platform": "demo", "config_entry_id": "d1"}]}
    LIVE = {"entities": [{"entity_id": "sensor.m", "platform": "mqtt", "config_entry_id": "m1", "name": "now"},
                         {"entity_id": "sensor.d", "platform": "demo", "config_entry_id": "d1", "name": "B"},
                         {"entity_id": "sensor.d2", "platform": "demo", "config_entry_id": "d2"},
                         {"entity_id": "light.new", "platform": "hue", "config_entry_id": "h1"}],
            "deleted_entities": [{"entity_id": "sensor.m_old", "platform": "mqtt", "config_entry_id": "m1"},
                                 {"entity_id": "sensor.d", "platform": "demo", "config_entry_id": "d1"}]}

    def test_entities_of_the_domain_or_its_entries_from_the_backup(self):
        doc, _ = merge(".storage/core.entity_registry", _doc(self.LIVE), _doc(self.OLD))
        self.assertEqual({e["entity_id"]: e.get("name") for e in doc["data"]["entities"]},
                         {"sensor.m": "now", "light.new": None, "sensor.d": "A", "switch.helper": "A helper"})
        self.assertEqual({e["entity_id"] for e in doc["data"]["deleted_entities"]}, {"sensor.m_old", "sensor.gone"})

    def test_a_migrated_live_registry_comes_back_whole(self):
        doc, note = merge(".storage/core.entity_registry", _doc(self.LIVE, version=2), _doc(self.OLD))
        self.assertEqual({e["entity_id"] for e in doc["data"]["entities"]}, {"sensor.d", "switch.helper", "sensor.m"})
        self.assertTrue(note)


class DeviceRegistryTest(unittest.TestCase):
    OLD = {"devices": [{"id": "dev_d", "config_entries": ["d1"], "name": "A"},
                       {"id": "dev_shared", "config_entries": ["d1", "m1"], "name": "shared"},
                       {"id": "dev_m", "config_entry_id": "m1", "name": "before"}],
           "deleted_devices": []}
    LIVE = {"devices": [{"id": "dev_m", "config_entry_id": "m1", "name": "now"},
                        {"id": "dev_d", "config_entry_id": "d1", "primary_config_entry": "d1", "name": "B"},
                        {"id": "dev_d2", "config_entry_id": "d2", "name": "made by B"},
                        {"id": "dev_shared", "config_entries": ["m1", "d2"], "name": "shared now"}],
            "deleted_devices": [{"id": "dev_x", "config_entry_id": "d2"}, {"id": "dev_y", "config_entry_id": "m1"}]}

    def test_devices_only_of_the_domain_from_the_backup_shared_ones_live(self):
        doc, _ = merge(".storage/core.device_registry", _doc(self.LIVE), _doc(self.OLD))
        devices = {d["id"]: d for d in doc["data"]["devices"]}
        self.assertEqual(set(devices), {"dev_m", "dev_shared", "dev_d"})
        self.assertEqual(devices["dev_m"]["name"], "now")
        self.assertEqual(devices["dev_d"]["name"], "A")
        self.assertEqual((devices["dev_shared"]["name"], sorted(devices["dev_shared"]["config_entries"])), ("shared now", ["d1", "m1"]))
        self.assertEqual({d["id"] for d in doc["data"]["deleted_devices"]}, {"dev_y"})


class StorageForVersionTest(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp(prefix="hri-merge-")
        self.addCleanup(shutil.rmtree, self.cfg, True)
        bdir = os.path.join(self.cfg, backupkit.BACKUP_DIR)
        os.makedirs(bdir)
        for name, entries, extra in (("old.zip", ENTRIES_OLD, {".storage/demo.data": _doc({"v": "A"})}),
                                     ("now.zip", ENTRIES_LIVE, {".storage/demo.data": _doc({"v": "B"}), ".storage/counter": _doc({"n": 1})})):
            with zipfile.ZipFile(os.path.join(bdir, name), "w") as zf:
                zf.writestr(backupkit.MARKER, "{}")
                zf.writestr("backup-info.json", json.dumps({"ha_version": "2026.9.4"}))
                zf.writestr(".storage/core.config_entries", _doc(entries))
                for n, raw in extra.items():
                    zf.writestr(n, raw)

    def test_the_merge_and_the_own_stores(self):
        rec, taken = backupkit.storage_for_version(self.cfg, "now.zip", "old.zip", "2026.9.4", "fallback",
                                                   (".storage/demo.data",), "demo")
        self.assertEqual(set(taken), {".storage/core.config_entries", ".storage/demo.data"})
        with zipfile.ZipFile(os.path.join(self.cfg, backupkit.BACKUP_DIR, rec["name"])) as zf:
            entries = json.loads(zf.read(".storage/core.config_entries"))["data"]["entries"]
            self.assertEqual({e["entry_id"] for e in entries}, {"m1", "i1", "d1"})
            self.assertEqual(json.loads(zf.read(".storage/demo.data"))["data"], {"v": "A"})
            self.assertEqual(json.loads(zf.read(".storage/counter"))["data"], {"n": 1})
            self.assertEqual(json.loads(zf.read("backup-info.json"))["storage_from"]["merged_for"], "demo")
        backupkit.validate(os.path.join(self.cfg, backupkit.BACKUP_DIR, rec["name"]))

    def test_without_a_domain_nothing_is_merged(self):
        _rec, taken = backupkit.storage_for_version(self.cfg, "now.zip", "old.zip", "2026.9.4", "fallback")
        self.assertEqual(taken, [])


if __name__ == "__main__":
    unittest.main()
