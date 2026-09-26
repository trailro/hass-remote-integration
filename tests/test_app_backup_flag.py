"""The Supervisor's hot backup of the app walks backups/ (0.25.2): a backup HRI deletes between the walk's listing and
its read fails the app's part of the Home Assistant backup (securetar AddFileError).  app/config.yaml backup_pre sets
backupkit.APP_BACKUP_FLAG and backup_post clears it; while it is set (and not stale), pruning, the removal of dead
partial backups and a delete from the UI or API wait.  A plain Docker install never has the flag."""

import os
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest

import backupkit
from tests.test_ha_app import APP_CONFIG, _yaml
from tests.test_review2_lifecycle import _post, _view
from tests.test_review_backup import _volume


def _flag(cfg, age=0.0):
    path = os.path.join(cfg, backupkit.APP_BACKUP_FLAG)
    with open(path, "w", encoding="utf-8"):
        pass
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


class DeferTest(unittest.TestCase):
    def setUp(self):
        self.cfg = _volume()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        self.names = []
        for i in range(3):
            rec = backupkit.create(self.cfg, f"b{i}", "2026.9.3")
            path = os.path.join(self.cfg, backupkit.BACKUP_DIR, rec["name"])
            os.utime(path, (time.time() - 3600 * (3 - i),) * 2)
            self.names.append(rec["name"])

    def _left(self):
        return sorted(b["name"] for b in backupkit.list_backups(self.cfg))

    def test_no_flag_prunes(self):
        """A plain Docker install, or no Home Assistant backup running: as before."""
        self.assertFalse(backupkit.app_backup_running(self.cfg))
        self.assertEqual(len(backupkit.prune(self.cfg, 1)), 2)
        self.assertEqual(len(self._left()), 1)

    def test_a_running_backup_defers_pruning(self):
        _flag(self.cfg)
        self.assertTrue(backupkit.app_backup_running(self.cfg))
        self.assertEqual(backupkit.prune(self.cfg, 1), [])
        self.assertEqual(self._left(), sorted(self.names))
        os.remove(os.path.join(self.cfg, backupkit.APP_BACKUP_FLAG))  # backup_post
        self.assertEqual(len(backupkit.prune(self.cfg, 1)), 2)

    def test_a_stale_flag_is_ignored(self):
        """A backup that never ran its backup_post (the Supervisor restarted mid-backup) must not stop pruning for
        good; nor a flag dated far ahead (a clock set back since)."""
        for age in (backupkit.APP_BACKUP_FLAG_STALE_S + 60, -(backupkit.APP_BACKUP_FLAG_STALE_S + 60)):
            _flag(self.cfg, age)
            self.assertFalse(backupkit.app_backup_running(self.cfg), age)
        _flag(self.cfg, backupkit.APP_BACKUP_FLAG_STALE_S - 60)
        self.assertTrue(backupkit.app_backup_running(self.cfg))
        _flag(self.cfg, backupkit.APP_BACKUP_FLAG_STALE_S + 60)
        self.assertEqual(len(backupkit.prune(self.cfg, 1)), 2)

    def test_a_new_backup_leaves_dead_partials_while_flagged(self):
        bdir = os.path.join(self.cfg, backupkit.BACKUP_DIR)
        dead = os.path.join(bdir, "20200101-000000.zip")
        open(dead, "w").close()  # an empty reservation of a backup killed while writing
        os.utime(dead, (time.time() - backupkit.PARTIAL_STALE_S - 60,) * 2)
        _flag(self.cfg)
        backupkit.create(self.cfg, "during", "2026.9.3")
        self.assertTrue(os.path.exists(dead))
        os.remove(os.path.join(self.cfg, backupkit.APP_BACKUP_FLAG))
        backupkit.create(self.cfg, "after", "2026.9.3")
        self.assertFalse(os.path.exists(dead))

    def test_a_delete_waits(self):
        view = _view(self.cfg)
        _flag(self.cfg)
        res = _post(view, self.names[0], "delete")
        self.assertFalse(res["ok"])
        self.assertIn("Home Assistant backup", res["error"])
        self.assertTrue(os.path.exists(os.path.join(self.cfg, backupkit.BACKUP_DIR, self.names[0])))
        _flag(self.cfg, backupkit.APP_BACKUP_FLAG_STALE_S + 60)
        self.assertEqual(_post(view, self.names[0], "delete"), {"ok": True})

    def test_the_flag_is_in_no_backup(self):
        """Set exactly while the Supervisor walks the folder: in the app's backup, every restore would bring it back
        (and pruning would wait up to APP_BACKUP_FLAG_STALE_S); in HRI's own, a restore would too."""
        _flag(self.cfg)
        self.assertIn(backupkit.APP_BACKUP_FLAG, backupkit.DISPOSABLE_GLOBS)
        self.assertIn(backupkit.APP_BACKUP_FLAG, backupkit.APP_BACKUP_EXCLUDE_GLOBS)
        self.assertNotIn(backupkit.APP_BACKUP_FLAG, {rel for _path, rel in backupkit.iter_files(self.cfg)})


@unittest.skipUnless(APP_CONFIG.is_file() and shutil.which("sh"), "app/ not copied next to the tests, or no sh")
class AppCommandsTest(unittest.TestCase):
    """app/config.yaml backup_pre and backup_post, run as the Supervisor runs them: `docker exec` of the string split
    with shlex (aiodocker Containers.exec), no shell of its own.  A command that exits non-zero fails the app's part
    of the Home Assistant backup (App._backup_command), so both always exit 0."""

    def setUp(self):
        self.cfg = _yaml(APP_CONFIG)
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)

    def _run(self, key, config_dir):
        argv = [a.replace("/config/", config_dir.rstrip("/") + "/") for a in shlex.split(self.cfg[key])]
        return subprocess.run(argv, capture_output=True, text=True, timeout=30)

    def test_the_keys(self):
        self.assertNotIn("backup", self.cfg)  # hot: cold would stop the app, and ignores backup_pre/backup_post
        for key in ("backup_pre", "backup_post"):
            self.assertIn(f"/config/{backupkit.APP_BACKUP_FLAG}", self.cfg[key], key)

    def test_pre_sets_and_post_clears_the_flag(self):
        cfg = os.path.join(self.root, "config")
        os.makedirs(os.path.join(cfg, backupkit.STATE_DIR))
        self.assertEqual(self._run("backup_pre", cfg).returncode, 0)
        self.assertTrue(backupkit.app_backup_running(cfg))
        self.assertEqual(self._run("backup_post", cfg).returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(cfg, backupkit.APP_BACKUP_FLAG)))
        self.assertEqual(self._run("backup_post", cfg).returncode, 0)  # nothing to clear

    def test_they_never_fail_the_backup(self):
        blocked = os.path.join(self.root, "file")
        open(blocked, "w").close()  # /config/integration_manager cannot be made: a file stands where /config is
        for key in ("backup_pre", "backup_post"):
            self.assertEqual(self._run(key, blocked).returncode, 0, key)


if __name__ == "__main__":
    unittest.main()
