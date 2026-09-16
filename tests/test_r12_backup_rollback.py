"""Review round 12 (m13): putting the pre-restore copy back never writes through a symbolic link."""

import json
import os
import shutil
import tempfile
import unittest
import zipfile
from unittest import mock

import backupkit
import jsonio
from tests.test_review_backup import _volume, _zip

A = "2026.8.3"


class RollbackThroughLinkTest(unittest.TestCase):
    """m13: putting the pre-restore copy back wrote through a symbolic link to a directory the wipe left alone."""

    def test_rollback_replaces_a_linked_parent_directory_instead_of_writing_through_it(self):
        cfg = _volume()
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cfg, True)
        self.addCleanup(shutil.rmtree, outside, True)
        # the source has no custom_components, so the rollback's wipe leaves that tree as it is
        _zip(cfg, "src.zip", {"ha_version": A})
        pre = os.path.join(cfg, backupkit.BACKUP_DIR, "pre.zip")
        with zipfile.ZipFile(pre, "w") as zf:
            zf.writestr(backupkit.MARKER, "{}")
            zf.writestr(".storage/core.config_entries", json.dumps({"from": "pre"}))
            zf.writestr("custom_components/x/manifest.json", "pre")
            zf.writestr("backup-info.json", json.dumps({"ha_version": A}))
        shutil.rmtree(os.path.join(cfg, "custom_components", "x"))
        os.symlink(outside, os.path.join(cfg, "custom_components", "x"))  # planted on the volume
        backupkit.schedule_restore(cfg, "src.zip")
        meta = backupkit._pending_meta(cfg)  # noqa: SLF001
        jsonio.write_json(os.path.join(cfg, backupkit.PENDING_META), {**meta, "pre_restore": "pre.zip"})  # a retry: its before-copy
        real_replace = os.replace
        failed = []

        def replace(src, dst):
            if "staging-restore-" in str(src) and "-rollback" not in str(src) and not failed:
                failed.append(src)
                raise OSError(28, "No space left on device")  # the restore's moves fail on a full disk
            return real_replace(src, dst)

        with mock.patch.object(backupkit.os, "replace", replace):
            result = backupkit.apply_pending(cfg, log=lambda _m: None, record=lambda r: True)
        self.assertTrue(failed)
        self.assertEqual(result.get("rolled_back_to"), "pre.zip", result)
        self.assertEqual(os.listdir(outside), [])
        linked = os.path.join(cfg, "custom_components", "x")
        self.assertFalse(os.path.islink(linked))
        with open(os.path.join(linked, "manifest.json"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "pre")
        self.assertEqual([n for n in os.listdir(os.path.join(cfg, backupkit.STATE_DIR)) if n.startswith("staging-")], [])


if __name__ == "__main__":
    unittest.main()
