"""Boot recovery settles an interrupted import from what Home Assistant will load, and never leaves a record that
blocks every import action: core.config_entries on disk decides, the phase only says how sure the import was."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import backupkit
from tests.fakes import entrypoint_for


class ImportBootIntentTest(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp(prefix="hri-import-boot-")
        self.addCleanup(shutil.rmtree, self.cfg, True)
        self.ep = entrypoint_for(self, self.cfg)
        os.makedirs(self.ep.STATE_DIR, exist_ok=True)
        self.storage = os.path.join(self.cfg, ".storage")
        os.makedirs(self.storage)
        self.source = os.path.join(self.ep.STATE_DIR, "import-extracted")
        os.makedirs(self.source)
        self.put(self.source, "source", "retry source")
        self.put(self.storage, "hub.e1", "imported")
        self.put(self.storage, "hub.e1.pre-import", "original")
        self.journal = os.path.join(self.ep.STATE_DIR, "import-pending.json")
        self.intent = {"version": 1, "entry_id": "e1", "domain": "hub", "phase": "pending",
                       "stores": [{"name": "hub.e1", "had_original": True}]}

    def put(self, directory, name, value):
        with open(os.path.join(directory, name), "w", encoding="utf-8") as fh:
            fh.write(value)

    def prepare(self, phase, present):
        with open(self.journal, "w", encoding="utf-8") as fh:
            json.dump({**self.intent, "phase": phase}, fh)
        with open(os.path.join(self.storage, "core.config_entries"), "w", encoding="utf-8") as fh:
            json.dump({"data": {"entries": [{"entry_id": "e1", "domain": "hub"}] if present else []}}, fh)

    def boot(self):
        with mock.patch.object(self.ep, "log"):
            self.ep.clean_import_leftovers()

    def read(self, name):
        with open(os.path.join(self.storage, name), encoding="utf-8") as fh:
            return fh.read()

    def timeline(self):
        try:
            with open(os.path.join(self.ep.STATE_DIR, "events.jsonl"), encoding="utf-8") as fh:
                return [json.loads(line) for line in fh]
        except FileNotFoundError:
            return []

    def assert_kept_imported(self):
        self.assertEqual(self.read("hub.e1"), "imported")
        self.assertFalse(os.path.exists(os.path.join(self.storage, "hub.e1.pre-import")))
        self.assertFalse(os.path.exists(self.journal))
        self.assertFalse(os.path.exists(self.source))

    def assert_retained(self):
        self.assertTrue(os.path.isfile(self.journal))
        self.assertEqual(self.read("hub.e1"), "imported")
        self.assertEqual(self.read("hub.e1.pre-import"), "original")
        self.assertTrue(os.path.isfile(os.path.join(self.source, "source")))

    def test_verified_commit_survives_restart_before_original_cleanup(self):
        self.prepare("commit", present=True)
        self.boot()
        self.assertEqual(self.read("hub.e1"), "imported")
        self.assertFalse(os.path.exists(os.path.join(self.storage, "hub.e1.pre-import")))
        self.assertFalse(os.path.exists(self.journal))
        self.assertFalse(os.path.exists(self.source))

    def test_restart_during_async_add_with_the_entry_flushed_keeps_the_import(self):
        # HA's final write (or SAVE_DELAY) saved the entry; the stop cancelled the import before its check
        self.prepare("added", present=True)
        self.boot()
        self.assert_kept_imported()
        [event] = self.timeline()
        self.assertEqual(event["kind"], "restore")
        self.assertIn("imported stores are kept", event["message"])

    def test_restart_during_async_add_before_any_save_puts_the_original_back(self):
        self.prepare("added", present=False)
        self.boot()
        self.assertEqual(self.read("hub.e1"), "original")
        self.assertFalse(os.path.exists(self.journal))
        self.assertFalse(os.path.exists(self.source))
        self.assertIn("original stores were put back", self.timeline()[0]["message"])

    def test_a_saved_entry_keeps_its_stores_whatever_phase_was_recorded(self):
        for phase in ("pending", "uncertain", "rollback"):
            with self.subTest(phase=phase):
                self.setUp()
                self.prepare(phase, present=True)  # Home Assistant loads this entry: its stores must stay with it
                self.boot()
                self.assert_kept_imported()
                self.assertIn(f"had reached '{phase}'", self.timeline()[-1]["message"])

    def test_a_saved_entry_with_a_missing_store_still_finishes_and_says_so(self):
        os.remove(os.path.join(self.storage, "hub.e1"))
        self.prepare("commit", present=True)
        self.boot()
        self.assertFalse(os.path.exists(os.path.join(self.storage, "hub.e1.pre-import")))
        self.assertFalse(os.path.exists(self.journal))
        self.assertIn("not on the volume: hub.e1", self.timeline()[0]["message"])

    def test_an_unusable_original_does_not_hold_the_rollback(self):
        os.remove(os.path.join(self.storage, "hub.e1.pre-import"))
        os.makedirs(os.path.join(self.storage, "hub.e1.pre-import"))
        self.prepare("added", present=False)
        self.boot()
        self.assertFalse(os.path.exists(self.journal))
        self.assertIn("hub.e1.pre-import", self.timeline()[0]["message"])

    def test_a_round_trip_through_a_version_without_the_intent_has_nothing_left_to_recover(self):
        # 0.27.0 ignored the record: it put the original back elsewhere and removed the source
        self.intent["stores"] = [{"name": "hub.gone", "had_original": False}]
        shutil.rmtree(self.source)
        os.remove(os.path.join(self.storage, "hub.e1.pre-import"))
        self.prepare("added", present=False)
        self.boot()
        self.assertFalse(os.path.exists(self.journal))
        self.assertEqual(self.read("hub.e1"), "imported")  # not the import's: untouched
        self.assertIn("nothing left to recover", self.timeline()[0]["message"])

    def test_confirmed_entry_absence_restores_original_and_removes_new_store(self):
        self.intent["stores"].append({"name": "hub.new", "had_original": False})
        self.put(self.storage, "hub.new", "partial copy")
        self.prepare("rollback", present=False)
        self.boot()
        self.assertEqual(self.read("hub.e1"), "original")
        self.assertFalse(os.path.exists(os.path.join(self.storage, "hub.new")))
        self.assertFalse(os.path.exists(self.journal))

    def test_unreadable_or_malformed_disk_state_keeps_recovery_even_after_commit_marker(self):
        self.prepare("commit", present=True)
        saved_open = open
        path = os.path.join(self.storage, "core.config_entries")

        def unreadable(filename, *args, **kwargs):
            if filename == path:
                raise PermissionError("test unavailable store")
            return saved_open(filename, *args, **kwargs)

        with mock.patch.object(self.ep, "open", unreadable, create=True):
            self.boot()
        self.assert_retained()
        self.put(self.storage, "core.config_entries", "not JSON")
        self.boot()
        self.assert_retained()

    def test_a_corrupt_intent_keeps_every_original_as_an_orphan_and_overwrites_nothing(self):
        # without the record nothing says whether a loaded entry uses the imported store: putting the original back
        # over it would pull the store from under that entry
        self.prepare("pending", present=False)
        self.put(self.storage, "other.x.pre-import", "another original")
        self.put(self.storage, "other.x.pre-import.orphan", "an older orphan")
        with open(self.journal, "w", encoding="utf-8") as fh:
            fh.write("not JSON")
        self.boot()
        self.assertFalse(os.path.exists(self.journal))
        self.assertEqual(self.read("hub.e1"), "imported")
        self.assertEqual(self.read("hub.e1.pre-import.orphan"), "original")
        self.assertFalse(os.path.exists(os.path.join(self.storage, "hub.e1.pre-import")))
        self.assertFalse(os.path.exists(os.path.join(self.storage, "other.x")))
        self.assertEqual(self.read("other.x.pre-import.orphan"), "an older orphan")
        self.assertEqual(self.read("other.x.pre-import.orphan.1"), "another original")
        self.assertFalse(os.path.exists(self.source))
        [event] = self.timeline()
        self.assertIn("unreadable", event["message"])
        self.assertIn("hub.e1.pre-import.orphan", event["message"])
        self.boot()  # no record now: the legacy cleanup must not take an orphan for something to put back
        self.assertEqual(self.read("hub.e1"), "imported")
        self.assertEqual(self.read("hub.e1.pre-import.orphan"), "original")

    def test_a_corrupt_intent_stays_while_an_original_cannot_be_kept_aside(self):
        with open(self.journal, "w", encoding="utf-8") as fh:
            fh.write("not JSON")
        with mock.patch.object(self.ep.os, "replace", side_effect=PermissionError("test")):
            self.boot()
        self.assertTrue(os.path.isfile(self.journal))
        self.assertEqual(self.read("hub.e1"), "imported")
        self.assertEqual(self.read("hub.e1.pre-import"), "original")
        self.assertTrue(os.path.isfile(os.path.join(self.source, "source")))

    def test_an_unknown_phase_is_an_unreadable_record(self):
        self.prepare("sideways", present=True)
        self.boot()
        self.assertEqual(self.read("hub.e1.pre-import.orphan"), "original")
        self.assertIn("unreadable", self.timeline()[0]["message"])

    def test_the_timeline_line_does_not_join_a_torn_last_line(self):
        with open(os.path.join(self.ep.STATE_DIR, "events.jsonl"), "w", encoding="utf-8") as fh:
            fh.write('{"ts": "x", "kind": "boot", "mess')
        self.prepare("commit", present=True)
        self.boot()
        with open(os.path.join(self.ep.STATE_DIR, "events.jsonl"), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(json.loads(lines[1])["kind"], "restore")

    def test_marker_does_not_enter_manager_or_app_backup(self):
        self.assertTrue(backupkit._excluded("integration_manager/import-pending.json"))
        self.assertTrue(any(backupkit.fnmatch.fnmatch("integration_manager/import-pending.json", pattern)
                            for pattern in backupkit.APP_BACKUP_EXCLUDE_GLOBS))
