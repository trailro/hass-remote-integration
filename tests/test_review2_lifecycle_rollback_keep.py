"""Review 2, M-10: a keep switch to an older Home Assistant after a completed full rollback.

A full rollback schedules the pre-update backup (made on the running Home Assistant) for the next boot, with no
version of its own, and has already recorded the previous tag as running.  A keep switch to an older version
was accepted after it (only restore and rebuild looked at a restore scheduled by hand), and the boot of that
older version dropped the rollback's restore because it cannot read a configuration made on a newer one: the
previous code then booted on the .storage the rejected version had migrated.  These tests schedule exactly what
_rollback_full schedules, ask for the switch, and boot the result through the real entrypoint."""

import asyncio
import json
import os
import shutil
import unittest
from types import SimpleNamespace
from unittest import mock

import backupkit
from tests.fakes import entrypoint_for
from tests.test_review_backup import _volume

RUNNING = "2026.8.3"
OLDER = "2026.6.1"
NEWER = "2026.9.3"


class KeepSwitchAfterRollbackTest(unittest.TestCase):

    def setUp(self):
        from custom_components.integration_manager import ha_updater, views

        self.views = views
        self.cfg = cfg = _volume()
        self.addCleanup(shutil.rmtree, cfg, ignore_errors=True)
        for p in (mock.patch.object(views, "HA_VERSION", RUNNING), mock.patch.object(views.events, "emit")):
            p.start()
            self.addCleanup(p.stop)

        async def executor(fn, *args):
            return fn(*args)

        self.hass = SimpleNamespace(config=SimpleNamespace(config_dir=cfg, path=lambda *p: os.path.join(cfg, *p)),
                                    async_add_executor_job=executor)
        self.updater = ha_updater.HaUpdater(self.hass)

        async def async_backup(label=""):
            return backupkit.create(cfg, label, RUNNING)

        self.installer = SimpleNamespace(hass=self.hass, busy=False, running="demo", running_tag="v1",
                                         state=SimpleNamespace(rollback_backup=None),
                                         settings=SimpleNamespace(backup_keep=50), async_backup=async_backup,
                                         protected_backups=lambda: set(), min_ha_of=lambda *_a: None)

    def _write_storage(self, text):
        with open(os.path.join(self.cfg, ".storage", "core.config_entries"), "w", encoding="utf-8") as fh:
            fh.write(text)

    def _storage(self):
        with open(os.path.join(self.cfg, ".storage", "core.config_entries"), encoding="utf-8") as fh:
            return fh.read()

    def _schedule(self, rollback=True, made_on=RUNNING):
        """The pre-update backup, the storage the rejected version migrated, and the schedule _rollback_full writes
        (a hand-made restore on System: the same schedule without the rollback's record)."""
        self._write_storage("before the update")
        name = backupkit.create(self.cfg, "pre-update", made_on)["name"]
        self._write_storage("migrated by the rejected version")
        if rollback:
            backupkit.schedule_restore(self.cfg, name, ["storage", "custom_components"], None, True)
            self.installer.state.rollback_backup = name
        else:
            backupkit.schedule_restore(self.cfg, name)
        return name

    def _switch(self, target):
        try:
            asyncio.run(self.views.async_change_ha_version(self.installer, self.updater, target, "keep", "test"))
        except ValueError as err:
            return str(err)
        return None

    def _boot(self):
        """The next boot's apply_config_changes, on what ha.json says boots."""
        with open(os.path.join(self.cfg, backupkit.STATE_DIR, "ha.json"), encoding="utf-8") as fh:
            state = json.load(fh)
        wanted = state.get("desired") or state["current"]
        ep = entrypoint_for(self, self.cfg)
        with mock.patch.object(ep, "log"), mock.patch.object(ep, "save_state", return_value=True):
            ep.apply_config_changes(state, wanted, state["current"])
        return state

    def test_older_keep_after_a_full_rollback_is_refused_and_the_rollback_restores(self):
        backup = self._schedule()
        refused = self._switch(OLDER)
        state = self._boot()
        self.assertEqual(self._storage(), "before the update",
                         f"the boot dropped the full rollback's restore ({state.get('last_error')}): the previous code runs on the migrated .storage")
        self.assertTrue((state.get("last_restore") or {}).get("ok"))
        self.assertIsNotNone(refused, "a keep switch to an older Home Assistant was accepted over a full rollback's restore")
        self.assertIn(backup, refused)
        self.assertIn("restart", refused)

    def test_older_keep_over_a_hand_scheduled_restore_is_refused(self):
        self._schedule(rollback=False)
        refused = self._switch(OLDER)
        self.assertIsNotNone(refused)
        self.assertIn("cancel it first", refused)
        self.assertTrue(backupkit.pending(self.cfg))

    def test_newer_keep_after_a_full_rollback_is_allowed_and_the_restore_applies_on_it(self):
        self._schedule()
        self.assertIsNone(self._switch(NEWER))
        state = self._boot()
        self.assertEqual(state["desired"], NEWER)
        self.assertEqual(self._storage(), "before the update")
        self.assertTrue(state["last_restore"]["ok"])

    def test_older_keep_over_a_restore_the_target_can_read_is_allowed(self):
        self._schedule(rollback=False, made_on="2026.5.0")
        self.assertIsNone(self._switch(OLDER))
        state = self._boot()
        self.assertEqual(self._storage(), "before the update")
        self.assertTrue(state["last_restore"]["ok"])


if __name__ == "__main__":
    unittest.main()
