"""Fixes from the fourth review round: secret masking, masked import values, old change-report keys,
interrupted backups, service sections."""

import os
import tempfile
import time
import unittest

import backupkit
from custom_components.integration_manager import change_report as cr
from custom_components.integration_manager.diagnostics import scrub
from custom_components.integration_manager.ha_import import _unmask
from custom_components.integration_manager.services_catalog import _flat_fields


class ScrubTest(unittest.TestCase):
    def test_device_keys_and_non_string_secrets(self):
        out = scrub({"local_key": "abc", "noise_psk": "x", "encryption_key": "k", "api-key": "k", "pin": 1234,
                     "password": 123456, "tokens": ["a"], "auth": {"bearer": "t"}, "host": "10.0.0.2", "translation_key": "t"})
        for k in ("local_key", "noise_psk", "encryption_key", "api-key", "pin", "password", "tokens"):
            self.assertEqual(out[k], "***", k)
        self.assertEqual(out["auth"], {"bearer": "***"})
        self.assertEqual(out["host"], "10.0.0.2")
        self.assertEqual(out["translation_key"], "t")

    def test_not_secret_lookalikes(self):
        out = scrub({"pinned": "x", "ping": 3, "spinner": "y", "keep": 2, "use_ssl": False})
        self.assertEqual(out, {"pinned": "x", "ping": 3, "spinner": "y", "keep": 2, "use_ssl": False})

    def test_text_patterns(self):
        s = scrub("Authorization: Bearer abcdefgh.ijk mqtt://user:pw@host:1883 local_key=deadbeef")
        self.assertNotIn("abcdefgh", s)
        self.assertNotIn(":pw@", s)
        self.assertNotIn("deadbeef", s)
        self.assertIn("mqtt://user:***@host", s)


class UnmaskTest(unittest.TestCase):
    def test_list_with_an_item_removed(self):
        stored = {"hosts": [{"host": "a", "password": "p1"}, {"host": "b", "password": "p2"}]}
        given = scrub(stored)
        given["hosts"].pop(0)
        misses = []
        out = _unmask(given, stored, misses)
        self.assertEqual(out["hosts"], [{"host": "b", "password": "p2"}])
        self.assertEqual(misses, [])

    def test_reordered_list_pairs_by_content(self):
        stored = {"hosts": [{"host": "a", "password": "p1"}, {"host": "b", "password": "p3"}]}
        given = scrub(stored)
        given["hosts"].reverse()
        misses = []
        self.assertEqual(_unmask(given, stored, misses)["hosts"], [{"host": "b", "password": "p3"}, {"host": "a", "password": "p1"}])
        self.assertEqual(misses, [])

    def test_ambiguous_duplicates_are_refused(self):
        stored = {"hosts": [{"host": "a", "password": "p1"}, {"host": "a", "password": "p2"}, {"host": "c", "password": "p3"}]}
        given = scrub(stored)
        given["hosts"].pop(0)
        misses = []
        _unmask(given, stored, misses)
        self.assertTrue(misses)

    def test_non_string_secret_comes_back(self):
        stored = {"pin": 1234}
        self.assertEqual(_unmask(scrub(stored), stored), stored)

    def test_a_value_that_really_holds_stars(self):
        stored = {"note": "***Important***", "mask_placeholder": "********"}
        misses = []
        self.assertEqual(_unmask(dict(stored), stored, misses), stored)
        self.assertEqual(misses, [])

    def test_auth_block_masked_field_by_field(self):
        out = scrub({"auth": {"username": "u", "password": "p"}})
        self.assertEqual(out, {"auth": {"username": "u", "password": "***"}})
        self.assertEqual(scrub("Basic information about it"), "Basic information about it")


class ChangeReportOldKeysTest(unittest.TestCase):
    def test_before_snapshot_from_an_older_manager(self):
        rec = {"entity_id": "sensor.t", "name": "T", "unit_of_measurement": "°C"}
        pending = {"domain": "x", "before": {"entities": {"uid:abc": rec}, "services": {}}}
        after = {"entities": {"uid:sensor:abc": dict(rec)}, "services": {}}
        report = cr.build(pending, after)
        self.assertEqual(report.get("entities_added"), [])
        self.assertEqual(report.get("entities_removed"), [])


class InterruptedBackupTest(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.cfg, ".storage"))
        os.makedirs(os.path.join(self.cfg, "integration_manager"))
        for rel in (".storage/core.config_entries", "integration_manager/state.json"):
            with open(os.path.join(self.cfg, rel), "w", encoding="utf-8") as fh:
                fh.write("{}")
        self.bdir = os.path.join(self.cfg, backupkit.BACKUP_DIR)
        os.makedirs(self.bdir)

    def test_placeholder_not_listed_and_dead_partials_removed(self):
        old = time.time() - backupkit.PARTIAL_STALE_S - 10
        for n in ("20200101-000000.zip", ".20200101-000000.zip.abc.tmp"):
            p = os.path.join(self.bdir, n)
            open(p, "w").close()
            os.utime(p, (old, old))
        fresh = os.path.join(self.bdir, "20990101-000000.zip")
        open(fresh, "w").close()
        self.assertEqual(backupkit.list_backups(self.cfg), [])
        rec = backupkit.create(self.cfg, "t")
        names = set(os.listdir(self.bdir))
        self.assertEqual(names, {rec["name"], "20990101-000000.zip"})  # a fresh reservation may still be writing
        self.assertEqual([b["name"] for b in backupkit.list_backups(self.cfg)], [rec["name"]])


class ServiceSectionsTest(unittest.TestCase):
    def test_section_fields_are_flattened(self):
        desc = {"fields": {"brightness": {"selector": {"number": {}}},
                           "advanced_fields": {"collapsed": True, "fields": {"transition": {"selector": {"number": {}}}}}}}
        tr = {"fields": {"transition": {"name": "Transition"}}}
        out = _flat_fields(desc, tr)
        self.assertEqual(set(out), {"brightness", "transition"})
        self.assertEqual(out["transition"]["name"], "Transition")

    def test_malformed_values(self):
        self.assertEqual(_flat_fields({"fields": ["x"]}, {"fields": "y"}), {})
        self.assertEqual(_flat_fields({"fields": {"a": None}}, {}), {"a": {"name": None, "description": None}})



class HistoryOutOfBackupsTest(unittest.TestCase):
    def test_timeline_history_and_reports_not_backed_up_nor_restored(self):
        import zipfile

        cfg = tempfile.mkdtemp()
        for rel in (".storage/core.config_entries", "integration_manager/state.json", "integration_manager/events.jsonl",
                    "integration_manager/events.jsonl.1", "integration_manager/resource_history.json",
                    "integration_manager/change_reports.json"):
            os.makedirs(os.path.dirname(os.path.join(cfg, rel)), exist_ok=True)
            with open(os.path.join(cfg, rel), "w", encoding="utf-8") as fh:
                fh.write("{}")
        rels = {rel for _p, rel in backupkit.iter_files(cfg)}
        self.assertEqual(rels & {"integration_manager/events.jsonl", "integration_manager/events.jsonl.1",
                                 "integration_manager/resource_history.json", "integration_manager/change_reports.json"}, set())
        old = os.path.join(cfg, "old.zip")  # made before these files were excluded
        with zipfile.ZipFile(old, "w") as zf:
            zf.writestr("integration_manager/events.jsonl", "old")
            zf.writestr("integration_manager/state.json", "{}")
        with zipfile.ZipFile(old) as zf:
            self.assertEqual(backupkit._names(zf), ["integration_manager/state.json"])


class LatestVersionsOutOfBackupsTest(unittest.TestCase):
    def test_latest_versions_not_restored(self):
        self.assertTrue(backupkit._excluded("integration_manager/latest_versions.json"))


if __name__ == "__main__":
    unittest.main()
