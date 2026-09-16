"""Test campaign: the copy taken before a restore is not protected from pruning."""

import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace

from custom_components.integration_manager.installer import Installer


class ProtectedPreRestoreTest(unittest.TestCase):
    def _installer(self, last_restore):
        inst = object.__new__(Installer)
        inst.state_dir = tempfile.mkdtemp()
        inst.state = SimpleNamespace(installed={}, rollback_backup=None)
        with open(os.path.join(inst.state_dir, "ha.json"), "w", encoding="utf-8") as fh:
            json.dump({"last_restore": last_restore}, fh)
        return inst

    @staticmethod
    def _ago(seconds):
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - seconds))

    def test_the_copy_taken_before_the_restore_is_protected(self):
        inst = self._installer({"ok": True, "at": self._ago(60), "backup": "src.zip", "pre_restore": "pre.zip"})
        self.assertEqual({"src.zip", "pre.zip"}, inst.protected_backups())

    def test_an_undated_copy_stays_protected(self):
        inst = self._installer({"ok": True, "backup": "src.zip", "pre_restore": "pre.zip"})
        self.assertIn("pre.zip", inst.protected_backups())

    def test_a_stale_copy_gives_its_keep_slot_back(self):
        inst = self._installer({"ok": True, "at": self._ago(30 * 86400), "backup": "src.zip", "pre_restore": "pre.zip"})
        self.assertNotIn("pre.zip", inst.protected_backups())


if __name__ == "__main__":
    unittest.main()
