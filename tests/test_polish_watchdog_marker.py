"""The app's Watchdog marker survives HRI's own restores.  An HRI restore wiped integration_manager/, so restoring a
backup without integration_manager/app-watchdog-enabled (one made by 0.24.x, by a Docker install) removed it, and the
next app start turned the Watchdog on again although the user had turned it off (docs/app.md: "it stays off")."""

import fnmatch
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from unittest import mock

import backupkit
from tests.fakes import entrypoint_for

MARKER = f"{backupkit.STATE_DIR}/app-watchdog-enabled"


def _volume(test):
    cfg = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, cfg, True)
    for d in (".storage", backupkit.STATE_DIR):
        os.makedirs(os.path.join(cfg, d))
    for rel, text in ((backupkit.MARKER, "{}"), (f"{backupkit.STATE_DIR}/ha.json", json.dumps({"current": "2026.8.3"})),
                      (MARKER, "2026-09-20T10:00:00\n")):
        with open(os.path.join(cfg, rel), "w", encoding="utf-8") as fh:
            fh.write(text)
    return cfg


def _backup(cfg, name, members):
    os.makedirs(os.path.join(cfg, backupkit.BACKUP_DIR), exist_ok=True)
    with zipfile.ZipFile(os.path.join(cfg, backupkit.BACKUP_DIR, name), "w") as zf:
        zf.writestr(backupkit.MARKER, "{}")
        zf.writestr(".storage/core.config_entries", "{}")
        for rel in members:
            zf.writestr(rel, "2026-01-01T00:00:00\n")


class WatchdogMarkerRestoreTest(unittest.TestCase):
    def test_the_marker_is_kept_live(self):
        cfg = _volume(self)
        ep = entrypoint_for(self, cfg)
        self.assertEqual(os.path.relpath(ep.APP_WATCHDOG_MARKER, cfg), MARKER)
        self.assertTrue(any(fnmatch.fnmatch(MARKER, g) for g in backupkit.KEEP_LIVE_GLOBS))

    def test_a_restore_of_a_backup_without_it_keeps_the_live_marker(self):
        cfg = _volume(self)
        _backup(cfg, "old.zip", ())  # made by 0.24.x or a Docker install: no marker
        backupkit.schedule_restore(cfg, "old.zip", force=True)
        result = backupkit.apply_pending(cfg, log=lambda *_: None)
        self.assertTrue(result["ok"], result)
        self.assertTrue(os.path.isfile(os.path.join(cfg, MARKER)))
        ep = entrypoint_for(self, cfg)
        with mock.patch.object(ep, "log"), mock.patch.object(ep.urllib.request, "urlopen") as urlopen:
            ep.enable_app_watchdog("t0ken")
        urlopen.assert_not_called()  # turned off by the user: stays off

    def test_a_backup_neither_holds_nor_restores_it(self):
        cfg = _volume(self)
        rec = backupkit.create(cfg, "x")
        with zipfile.ZipFile(os.path.join(cfg, backupkit.BACKUP_DIR, rec["name"])) as zf:
            self.assertNotIn(MARKER, zf.namelist())
        os.remove(os.path.join(cfg, MARKER))
        _backup(cfg, "with.zip", (MARKER,))  # an archive that carries one (a volume copied by other tools)
        backupkit.schedule_restore(cfg, "with.zip", force=True)
        self.assertTrue(backupkit.apply_pending(cfg, log=lambda *_: None)["ok"])
        self.assertFalse(os.path.exists(os.path.join(cfg, MARKER)))


if __name__ == "__main__":
    unittest.main()
