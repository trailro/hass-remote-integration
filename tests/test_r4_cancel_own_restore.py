"""A rollback cancels only its own scheduled restore, checked under the schedule lock."""

import json
import os
import tempfile
import unittest

import backupkit


def _schedule(cfg, zip_name):
    with open(os.path.join(cfg, backupkit.PENDING_META), "w", encoding="utf-8") as fh:
        json.dump({"zip": zip_name, "parts": ["storage"]}, fh)


class CancelOwnRestoreTest(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.cfg, "integration_manager"), exist_ok=True)

    def test_another_schedule_is_left_alone(self):
        _schedule(self.cfg, "restore-b.zip")
        self.assertFalse(backupkit.cancel_restore(self.cfg, only_zip="restore-a.zip"))
        self.assertEqual((backupkit._pending_meta(self.cfg) or {}).get("zip"), "restore-b.zip")

    def test_own_schedule_is_cancelled(self):
        _schedule(self.cfg, "restore-a.zip")
        backupkit.cancel_restore(self.cfg, only_zip="restore-a.zip")
        self.assertIsNone(backupkit._pending_meta(self.cfg))

    def test_without_only_zip_cancels_whatever_is_scheduled(self):
        _schedule(self.cfg, "restore-b.zip")
        backupkit.cancel_restore(self.cfg)
        self.assertIsNone(backupkit._pending_meta(self.cfg))


if __name__ == "__main__":
    unittest.main()
