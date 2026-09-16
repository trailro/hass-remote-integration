"""Review round 12 (m12): a backup holds at most MAX_MEMBERS files, and listing reads each backup once per file version."""

import os
import shutil
import unittest
import zipfile
from unittest import mock

import backupkit
from tests.test_review_backup import _volume, _zip

A, B = "2026.8.3", "2026.9.2"


class MemberCapTest(unittest.TestCase):
    """m12: a backup of hundreds of thousands of empty members took seconds and hundreds of MB to validate."""

    def setUp(self):
        self.cfg = _volume()
        self.addCleanup(shutil.rmtree, self.cfg, True)

    def test_validate_refuses_too_many_members_before_reading_them(self):
        path = _zip(self.cfg, "many.zip", {"ha_version": A})
        with zipfile.ZipFile(path, "a") as zf:
            for i in range(60):
                zf.writestr(f"custom_components/x/{i}", b"")
        with mock.patch.object(backupkit, "MAX_MEMBERS", 50, create=True), mock.patch.object(zipfile.ZipFile, "testzip", autospec=True, side_effect=zipfile.ZipFile.testzip) as testzip, \
                self.assertRaises(ValueError) as ctx:
            backupkit.validate(path)
        self.assertIn("more than 50 files", str(ctx.exception))
        testzip.assert_not_called()

    def test_a_backup_at_the_cap_is_valid(self):
        path = _zip(self.cfg, "ok.zip", {"ha_version": A})
        with mock.patch.object(backupkit, "MAX_MEMBERS", 3, create=True):  # marker, core.config_entries, + backup-info.json
            self.assertEqual(backupkit.validate(path)["ha_version"], A)

    def test_create_refuses_a_backup_validate_would_refuse(self):
        with mock.patch.object(backupkit, "MAX_MEMBERS", 5, create=True), self.assertRaises(ValueError) as ctx:
            backupkit.create(self.cfg, "big")
        self.assertIn("more than 5 files", str(ctx.exception))
        self.assertEqual(os.listdir(os.path.join(self.cfg, backupkit.BACKUP_DIR)), [])

    def test_listing_reads_each_backup_once_per_file_version(self):
        path = _zip(self.cfg, "a.zip", {"ha_version": A, "label": "one"})
        _zip(self.cfg, "b.zip", {"ha_version": A})
        opened = []
        real_init = zipfile.ZipFile.__init__

        def init(zf, file, *args, **kwargs):
            opened.append(os.path.basename(str(file)))
            return real_init(zf, file, *args, **kwargs)

        with mock.patch.object(zipfile.ZipFile, "__init__", init):
            first = backupkit.list_backups(self.cfg)
            self.assertEqual(sorted(opened), ["a.zip", "b.zip"])
            opened.clear()
            self.assertEqual(backupkit.list_backups(self.cfg), first)
            self.assertEqual(opened, [])
            first[0]["label"] = "changed by a caller"  # a caller's copy, not the memo
            self.assertNotEqual(backupkit.list_backups(self.cfg)[0]["label"], "changed by a caller")
            _zip(self.cfg, "a.zip", {"ha_version": B, "label": "rewritten, longer label"})
            opened.clear()
            again = {b["name"]: b for b in backupkit.list_backups(self.cfg)}
        self.assertEqual(opened, ["a.zip"])
        self.assertEqual((again["a.zip"]["ha_version"], again["a.zip"]["label"]), (B, "rewritten, longer label"))
        self.assertEqual(again["a.zip"]["bytes"], os.path.getsize(path))


if __name__ == "__main__":
    unittest.main()
