"""External review: a full rollback the process did not survive.

The rollback schedules its restore BEFORE it switches the files back, and the new tag is written at
the end of start().  A kill in between leaves the old configuration restored at the next boot while
state.json still names the version the rollback rejected.
"""

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import backupkit
import jsonio
from custom_components.integration_manager.installer import Installer

BACKUP = "pre-update-hub-v1.zip"


def _hass(cfg):
    async def executor(fn, *args):
        return fn(*args)

    return SimpleNamespace(config=SimpleNamespace(config_dir=cfg, components=set()), async_add_executor_job=executor)


class InterruptedFullRollbackTest(unittest.TestCase):
    STATE = {"domain": "hub", "installed": {"hub": {"versions": {"v1": {}, "v2": {}}, "running_tag": "v2",
                                                    "previous_tag": "v1", "pre_update_backup": BACKUP}}}

    def setUp(self):
        self.cfg = tempfile.mkdtemp(prefix="hri-rollback-")
        self.addCleanup(shutil.rmtree, self.cfg, ignore_errors=True)
        os.makedirs(os.path.join(self.cfg, "integration_manager"))
        os.makedirs(os.path.join(self.cfg, "backups"))
        with open(os.path.join(self.cfg, "backups", BACKUP), "wb") as fh:
            fh.write(b"not a real zip: validate() is mocked")
        self._write_state(self.STATE)

    def _write_state(self, state):
        with open(os.path.join(self.cfg, "integration_manager", "state.json"), "w", encoding="utf-8") as fh:
            json.dump(state, fh)

    def _state_when_the_restore_was_scheduled(self):
        """state.json as a kill right after the schedule leaves it on the volume."""
        inst = Installer(_hass(self.cfg))
        snapshot = {}

        def schedule_restore(cfg, name, parts=None, for_version=None, force=False):
            with open(os.path.join(cfg, "integration_manager", "state.json"), encoding="utf-8") as fh:
                snapshot.update(json.load(fh))
            return os.path.join(cfg, "integration_manager", "restore-pending-test.zip")

        with mock.patch.object(backupkit, "validate", lambda path: {"ha_version": "2026.1.0"}), \
                mock.patch.object(backupkit, "pending", lambda cfg: False), \
                mock.patch.object(backupkit, "schedule_restore", schedule_restore), \
                mock.patch.object(Installer, "start", mock.AsyncMock(return_value={"ok": True})):
            res = asyncio.run(inst.rollback_full("hub"))
        self.assertTrue(res.get("ok"), res)
        self.assertTrue(snapshot, "the restore was scheduled before anything was written to state.json")
        return snapshot

    def _boot_after(self, state, last_restore):
        """The boot reconcile on that state, with what the entrypoint recorded about the restore.  The
        outcome is stamped now: it belongs to the boot after the intent, not to some earlier restore of the
        same archive (which the intent's own stamp now tells apart)."""
        last_restore = {**last_restore, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self._write_state(state)
        jsonio.write_json(os.path.join(self.cfg, "integration_manager", "ha.json"), {"last_restore": last_restore})
        inst = Installer(_hass(self.cfg))
        deployed = []

        async def nothing(*args, **kwargs):
            return []

        inst._ensure_deployed = lambda domain, tag, force=False: deployed.append(tag) or False
        inst._requirements_for = nothing
        inst._enable_entries = nothing
        inst._patch_rows = lambda domain: []
        inst._notify_patches = lambda domain, rows: None
        inst._apply_patches = lambda domain: "none"
        inst._tree_hash = lambda domain: None
        inst._schedule_smoke = lambda *args: None
        asyncio.run(inst.async_reconcile())
        return inst, deployed

    def test_the_boot_after_the_kill_keeps_the_restored_version(self):
        killed = self._state_when_the_restore_was_scheduled()
        inst, deployed = self._boot_after(killed, {"ok": True, "backup": BACKUP})
        self.assertEqual(deployed, ["v1"], "the rejected version was deployed over the restored configuration")
        self.assertEqual(inst.running_tag, "v1")
        self.assertIsNone(inst.state.installed["hub"].get("pre_update_backup"))

    def test_a_restore_that_did_not_happen_leaves_the_running_version_alone(self):
        killed = self._state_when_the_restore_was_scheduled()
        inst, deployed = self._boot_after(killed, {"ok": False, "backup": BACKUP})
        self.assertEqual(deployed, ["v2"], "nothing was restored: the version that runs is still the new one")
        self.assertEqual(inst.running_tag, "v2")

    def test_a_rollback_whose_start_failed_leaves_nothing_to_finish(self):
        inst = Installer(_hass(self.cfg))
        cancelled = []
        with mock.patch.object(backupkit, "validate", lambda path: {"ha_version": "2026.1.0"}), \
                mock.patch.object(backupkit, "pending", lambda cfg: False), \
                mock.patch.object(backupkit, "schedule_restore", lambda *a, **k: "restore-pending-test.zip"), \
                mock.patch.object(backupkit, "cancel_restore", lambda cfg, only_zip=None: cancelled.append(only_zip)), \
                mock.patch.object(Installer, "start", mock.AsyncMock(return_value={"ok": False, "error": "pip failed"})):
            res = asyncio.run(inst.rollback_full("hub"))
        self.assertFalse(res["ok"])
        self.assertEqual(cancelled, ["restore-pending-test.zip"])
        with open(os.path.join(self.cfg, "integration_manager", "state.json"), encoding="utf-8") as fh:
            self.assertIsNone(json.load(fh).get("pending_rollback"), "the next boot would roll back on its own")


if __name__ == "__main__":
    unittest.main()
