"""Review round 5: a Home Assistant version change must not drop what an older change prepared until its own
preparation went through.

The restore branch already worked that way (its archive is scheduled, and validated, before the older change's
clean start is dropped).  The rebuild and keep branches dropped first and prepared afterwards, so a failure in
between - a full volume, an unreadable backup - left the box with neither the older change nor the new one, and
the error said nothing about what had gone."""

import asyncio
import json
import os
import shutil
import unittest
from types import SimpleNamespace
from unittest import mock

import backupkit
from tests.test_review_backup import _volume, _zip

RUNNING = "2026.8.3"
OLDER = "2026.1.0"
NEWER = "2026.9.0"
READABLE = "2025.12.0"


class PreparationOrderTest(unittest.TestCase):

    def setUp(self):
        from custom_components.integration_manager import ha_import, ha_updater, views

        self.views, self.ha_import = views, ha_import
        self.cfg = cfg = _volume()
        self.addCleanup(shutil.rmtree, cfg, ignore_errors=True)
        _zip(cfg, "old.zip", {"ha_version": READABLE})
        p = mock.patch.object(views, "HA_VERSION", RUNNING)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(views.events, "emit")
        p.start()
        self.addCleanup(p.stop)

        async def executor(fn, *args):
            return fn(*args)

        self.hass = SimpleNamespace(config=SimpleNamespace(config_dir=cfg, path=lambda *p: os.path.join(cfg, *p)),
                                    async_add_executor_job=executor)
        self.updater = ha_updater.HaUpdater(self.hass)
        self.backups = []

        async def async_backup(label=""):
            rec = backupkit.create(cfg, label)
            self.backups.append(rec["name"])
            return rec

        self.installer = SimpleNamespace(hass=self.hass, busy=False, running="demo", running_tag="v2",
                                         settings=SimpleNamespace(backup_keep=50), async_backup=async_backup,
                                         protected_backups=lambda: set(), min_ha_of=lambda *_a: None)

    # ----- what an older change left behind -----

    def _older_restore(self, for_version=OLDER):
        backupkit.schedule_restore(self.cfg, "old.zip", ["storage"], for_version)
        self.assertTrue(backupkit.pending(self.cfg))

    def _older_clean_start(self, to=OLDER):
        """What stage_rebuild writes: the extracted source, its summary and the plan the entrypoint reads."""
        out = os.path.join(self.cfg, self.ha_import.EXTRACT_DIR, ".storage")
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "core.restore_state"), "w", encoding="utf-8") as fh:
            fh.write("{}")
        with open(os.path.join(self.cfg, self.ha_import.SUMMARY_FILE), "w", encoding="utf-8") as fh:
            json.dump({"type": self.ha_import.REBUILD_TYPE, "name": "earlier.zip", "domains": {}}, fh)
        with open(os.path.join(self.cfg, self.ha_import.REBUILD_FILE), "w", encoding="utf-8") as fh:
            json.dump({"stage": "reset", "to": to, "backup": "earlier.zip", "domain": "demo"}, fh)

    def _read(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def _clean_start_is_staged(self):
        return os.path.isfile(os.path.join(self.cfg, self.ha_import.REBUILD_FILE))

    def _change(self, target, mode, **kw):
        return self.views.async_change_ha_version(self.installer, self.updater, target, mode, "test", **kw)

    # ----- keep -----

    def test_keep_keeps_an_older_changes_preparations_when_ha_json_cannot_be_written(self):
        self._older_restore()
        self._older_clean_start()
        with mock.patch.object(self.updater, "set_desired", side_effect=OSError("No space left on device")):
            with self.assertRaises(OSError):
                asyncio.run(self._change(NEWER, "keep"))
        self.assertTrue(backupkit.pending(self.cfg), "the older change's restore was dropped for a change that never got recorded")
        self.assertEqual(backupkit.pending_for_version(self.cfg), OLDER)
        self.assertTrue(self._clean_start_is_staged(), "the older change's clean start was dropped for a change that never got recorded")
        self.assertFalse(self.installer.busy)

    def test_keep_drops_the_older_changes_preparations_once_it_is_recorded(self):
        self._older_restore()
        self._older_clean_start()
        result = asyncio.run(self._change(NEWER, "keep"))
        self.assertEqual(result["config"], "keep")
        self.assertFalse(backupkit.pending(self.cfg))
        self.assertFalse(self._clean_start_is_staged())
        self.assertEqual(self._read(os.path.join(self.cfg, backupkit.STATE_DIR, "ha.json"))["desired"], NEWER)

    # ----- rebuild -----

    def test_rebuild_keeps_an_older_restore_when_staging_fails(self):
        self._older_restore()
        with mock.patch.object(self.ha_import, "stage_rebuild", side_effect=OSError("No space left on device")):
            with self.assertRaises(OSError):
                asyncio.run(self._change(OLDER, "rebuild"))
        self.assertTrue(backupkit.pending(self.cfg), "the older change's restore was dropped although nothing was staged")
        self.assertEqual(backupkit.pending_for_version(self.cfg), OLDER)
        self.assertFalse(self.installer.busy)

    def test_rebuild_checks_the_backup_before_anything_is_dropped(self):
        self._older_clean_start()

        async def async_backup(label=""):
            path = os.path.join(self.cfg, backupkit.BACKUP_DIR, "torn.zip")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(b"not a zip at all")
            return {"name": "torn.zip"}

        self.installer.async_backup = async_backup
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(self._change(OLDER, "rebuild"))
        self.assertIn("zip", str(ctx.exception))
        self.assertTrue(self._clean_start_is_staged(), "the older change's clean start was dropped for a backup that cannot be read")
        self.assertTrue(os.path.isdir(os.path.join(self.cfg, self.ha_import.EXTRACT_DIR)))
        self.assertFalse(self.installer.busy)

    def test_rebuild_replaces_an_older_restore_and_an_older_clean_start(self):
        self._older_restore()
        self._older_clean_start()
        result = asyncio.run(self._change(OLDER, "rebuild"))
        self.assertEqual(result["config"], "rebuild")
        self.assertFalse(backupkit.pending(self.cfg), "the restore an older change scheduled would take the clean start's place at the boot")
        plan = self._read(os.path.join(self.cfg, self.ha_import.REBUILD_FILE))
        self.assertEqual((plan["to"], plan["backup"], plan["stage"]), (OLDER, self.backups[-1], "reset"))
        summary = self._read(os.path.join(self.cfg, self.ha_import.SUMMARY_FILE))
        self.assertEqual(summary["name"], self.backups[-1])

    # ----- restore (the branch that already had the guarantee) -----

    def test_restore_schedules_its_own_archive_before_anything_is_dropped(self):
        # the reference the other two branches now match: the older change's clean start goes only after this
        # change's restore is validated and scheduled, so a failure later never leaves the box with neither
        self._older_clean_start(to=NEWER)
        with mock.patch.object(self.updater, "set_desired", side_effect=OSError("No space left on device")):
            with self.assertRaises(OSError):
                asyncio.run(self._change(OLDER, "restore", restore_backup="old.zip"))
        self.assertTrue(backupkit.pending(self.cfg), "its own restore is scheduled: that is what this change prepared")
        self.assertEqual(backupkit.pending_for_version(self.cfg), OLDER)


if __name__ == "__main__":
    unittest.main()
