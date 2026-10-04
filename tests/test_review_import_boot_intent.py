"""Boot recovery follows durable phase evidence and preserves uncertain imports."""
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

    def test_cancelled_save_verification_keeps_all_recovery_with_saved_entry(self):
        self.prepare("pending", present=True)  # cancellation after flush, before its verification/phase record
        self.boot()
        self.assert_retained()

    def test_failed_rollback_with_entry_still_on_disk_is_not_assumed_committed(self):
        self.prepare("uncertain", present=True)
        self.boot()
        self.assert_retained()

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

    def test_corrupt_intent_keeps_legacy_originals_and_source(self):
        self.prepare("pending", present=False)
        with open(self.journal, "w", encoding="utf-8") as fh:
            fh.write("not JSON")
        self.boot()
        self.assert_retained()

    def test_marker_does_not_enter_manager_or_app_backup(self):
        self.assertTrue(backupkit._excluded("integration_manager/import-pending.json"))
        self.assertTrue(any(backupkit.fnmatch.fnmatch("integration_manager/import-pending.json", pattern)
                            for pattern in backupkit.APP_BACKUP_EXCLUDE_GLOBS))
