"""Fixes from the third review round: login files and backups, concurrent backups, old pending restores, auth input."""

import json
import os
import tempfile
import threading
import unittest
import zipfile

import backupkit
from custom_components.integration_manager import events
from custom_components.integration_manager.auth import Auth


class BackupContentTest(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        for rel in (".storage/core.config_entries", "integration_manager/state.json", "integration_manager/auth_key",
                    "integration_manager/auth_revoked", "integration_manager/settings.json"):
            os.makedirs(os.path.dirname(os.path.join(self.cfg, rel)), exist_ok=True)
            with open(os.path.join(self.cfg, rel), "w", encoding="utf-8") as fh:
                fh.write("{}")

    def test_login_files_stay_out_of_backups(self):
        rels = {rel for _path, rel in backupkit.iter_files(self.cfg)}
        self.assertIn("integration_manager/settings.json", rels)
        self.assertNotIn("integration_manager/auth_key", rels)
        self.assertNotIn("integration_manager/auth_revoked", rels)

    def test_concurrent_backups_with_one_label_both_succeed(self):
        results, errors = [], []

        def run():
            try:
                results.append(backupkit.create(self.cfg, "same"))
            except Exception as err:  # noqa: BLE001
                errors.append(err)

        threads = [threading.Thread(target=run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        names = {r["name"] for r in results}
        self.assertEqual(len(names), 4)
        for name in names:
            backupkit.validate(os.path.join(self.cfg, backupkit.BACKUP_DIR, name))
        self.assertEqual([n for n in os.listdir(os.path.join(self.cfg, backupkit.BACKUP_DIR)) if n.endswith(".tmp")], [])

    def test_restore_scheduled_by_an_older_manager_still_knows_its_version(self):
        rec = backupkit.create(self.cfg, "old")
        with zipfile.ZipFile(os.path.join(self.cfg, backupkit.BACKUP_DIR, rec["name"]), "a") as zf:
            zf.writestr("backup-info.json", json.dumps({"ha_version": "2026.9.2"}))
        backupkit.schedule_restore(self.cfg, rec["name"], ["storage"])
        meta_path = os.path.join(self.cfg, backupkit.PENDING_META)
        with open(meta_path, encoding="utf-8") as fh:
            meta = json.load(fh)
        meta.pop("ha_version")  # as 0.10.0 wrote it
        with open(meta_path, "w", encoding="utf-8") as fh:
            json.dump(meta, fh)
        self.assertEqual(backupkit.pending_ha_version(self.cfg), "2026.9.2")

    def test_unrecorded_restore_is_marked_applied_not_applied_again(self):
        rec = backupkit.create(self.cfg, "x")
        backupkit.schedule_restore(self.cfg, rec["name"], ["storage"])
        result = backupkit.apply_pending(self.cfg, log=lambda _m: None, record=lambda _r: False)
        self.assertTrue(result["ok"])
        # applied: renamed to the applied marker instead of staying scheduled, so the next boot records it but never re-applies
        self.assertFalse(backupkit.pending(self.cfg))
        self.assertTrue(os.path.isfile(os.path.join(self.cfg, backupkit.APPLIED_META)))


class AuthInputTest(unittest.TestCase):
    def test_non_ascii_cookie_and_password_are_refused_without_errors(self):
        auth = Auth("pw", b"k" * 32)
        self.assertFalse(auth.valid_session("9999999999.é"))
        self.assertFalse(auth.check_password("\udcff\udcfe"))


class EventKindsTest(unittest.TestCase):
    def test_every_emitted_kind_is_filterable(self):
        self.assertTrue({"auth", "rebuild", "change"} <= set(events.KINDS))
