"""Review round 12 (m8): a scheduled restore whose archive copy is gone is dropped and recorded as failed."""

import os
import shutil
import unittest
from unittest import mock

import backupkit
from tests.fakes import entrypoint_for
from tests.test_r4_lifecycle import make_venv
from tests.test_review_backup import _volume, _zip

A, B = "2026.8.3", "2026.9.2"


class OrphanScheduleTest(unittest.TestCase):
    """m8: a restore-pending.json whose archive copy is gone stayed forever and protected its backup."""

    def setUp(self):
        self.cfg = _volume()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        _zip(self.cfg, "src.zip", {"ha_version": A})
        backupkit.schedule_restore(self.cfg, "src.zip", ["storage"])
        os.remove(backupkit.pending_archive(self.cfg))  # lost: removed by hand, a volume copied without it

    def test_apply_drops_it_records_why_and_releases_the_backup(self):
        self.assertIn("src.zip", backupkit.restore_needs(self.cfg))
        recorded = []
        result = backupkit.apply_pending(self.cfg, log=lambda _m: None, record=recorded.append)
        self.assertFalse(result["ok"])
        self.assertIn("gone", result["error"])
        self.assertEqual(recorded, [result])
        self.assertEqual(result["backup"], "src.zip")
        self.assertFalse(os.path.exists(os.path.join(self.cfg, backupkit.PENDING_META)))
        self.assertEqual(backupkit.restore_needs(self.cfg), set())
        self.assertEqual(backupkit.prune(self.cfg, keep=1, protect={"other.zip"}), [])  # the only backup: kept by count, not by a phantom schedule

    def test_a_boot_records_it_and_the_version_change_waiting_for_it_is_cancelled_once(self):
        ep = entrypoint_for(self, self.cfg)
        for v in (A, B):
            make_venv(self.cfg, v)
        state = {"current": B, "desired": A, "change": {"to": A, "mode": "restore", "backup": "src.zip"}}
        with mock.patch.object(ep, "log"):
            self.assertEqual(ep.apply_config_changes(state, A, B), B)
        self.assertFalse(state["last_restore"]["ok"])
        self.assertIn("gone", state["last_restore"]["error"])
        self.assertFalse(os.path.exists(os.path.join(self.cfg, backupkit.PENDING_META)))
        self.assertNotIn("src.zip", backupkit.restore_needs(self.cfg))

    def test_a_complete_schedule_is_left_alone(self):
        backupkit.schedule_restore(self.cfg, "src.zip", ["storage"])
        self.assertIsNone(backupkit.drop_orphan_schedule(self.cfg, log=lambda _m: None))
        self.assertTrue(backupkit.pending(self.cfg))


if __name__ == "__main__":
    unittest.main()
