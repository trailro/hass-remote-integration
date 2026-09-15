"""Fourth external review: logout in the same second as a login, unique pending restore archives."""

import json
import os
import tempfile
import time
import unittest
import zipfile
from unittest import mock

import backupkit
from custom_components.integration_manager.auth import Auth


class LogoutGenerationTest(unittest.TestCase):
    def test_logout_login_logout_in_one_second(self):
        path = os.path.join(tempfile.mkdtemp(), "auth_revoked")
        auth = Auth("pw", b"k" * 32, path)
        with mock.patch("time.time", return_value=1_800_000_000.2):
            auth.revoke_all()
            cookie = auth.new_session()
            self.assertTrue(auth.valid_session(cookie))
            auth.revoke_all()
            self.assertFalse(auth.valid_session(cookie))
            reloaded = Auth("pw", b"k" * 32, path)
            reloaded.load_revoked()
            self.assertFalse(reloaded.valid_session(cookie))
            self.assertTrue(reloaded.valid_session(reloaded.new_session()))

    def test_stored_value_stays_an_epoch_for_older_images(self):
        path = os.path.join(tempfile.mkdtemp(), "auth_revoked")
        auth = Auth("pw", b"k" * 32, path)
        auth.revoke_all()
        with open(path, encoding="utf-8") as fh:
            self.assertGreaterEqual(int(fh.read()), int(time.time()))

    def test_cookie_of_the_old_format_is_refused(self):
        auth = Auth("pw", b"k" * 32)
        self.assertFalse(auth.valid_session(f"{int(time.time()) + 3600}.deadbeef"))


class PendingArchiveNameTest(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.cfg, ".storage"))
        os.makedirs(os.path.join(self.cfg, backupkit.STATE_DIR))
        bdir = os.path.join(self.cfg, backupkit.BACKUP_DIR)
        os.makedirs(bdir)
        with zipfile.ZipFile(os.path.join(bdir, "b.zip"), "w") as zf:
            zf.writestr(backupkit.MARKER, json.dumps({"installed": {}}))
            zf.writestr(".storage/core.config_entries", "{}")
            zf.writestr("backup-info.json", json.dumps({}))

    def test_a_failed_schedule_keeps_the_confirmed_one(self):
        with mock.patch("time.time", return_value=1_800_000_000.0):
            backupkit.schedule_restore(self.cfg, "b.zip")
            self.assertTrue(backupkit.pending(self.cfg))
            with mock.patch.object(backupkit, "write_json", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    backupkit.schedule_restore(self.cfg, "b.zip", ["storage"])
        self.assertTrue(backupkit.pending(self.cfg))
        self.assertIsNotNone(backupkit.pending_archive(self.cfg))

    def test_two_schedules_in_one_millisecond_get_different_archives(self):
        with mock.patch("time.time", return_value=1_800_000_000.0):
            first = backupkit.schedule_restore(self.cfg, "b.zip")
            second = backupkit.schedule_restore(self.cfg, "b.zip")
        self.assertNotEqual(os.path.basename(first), os.path.basename(second))
        self.assertTrue(os.path.isfile(second))


if __name__ == "__main__":
    unittest.main()
