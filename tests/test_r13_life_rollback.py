"""Review round 13 (lifecycle), F1: a full rollback cut in two by Cancel restore.

_rollback_full schedules the restore of the pre-update backup (.storage and custom_components), then start()
selects the older version.  Cancel restore dropped that restore and the backup's protection but left the older
version selected: at the restart the older code ran on the config entries the newer version had migrated
(migration_error).  Cancelling it by hand is refused now; starting the version the rollback left undoes the whole
rollback instead, restore included."""

import asyncio
import json
import os
import shutil
import unittest
from types import SimpleNamespace
from unittest import mock

import backupkit
from custom_components.integration_manager import backup_views, installer as inst_mod
from custom_components.integration_manager.installer import Installer, State
from tests.test_review_backup import _volume, _zip

READABLE = "2025.12.0"  # older than the Home Assistant that boots next (ha.json of _volume): the restore is allowed


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class FullRollbackVersusCancelRestoreTest(unittest.TestCase):

    def setUp(self):
        cfg = self.cfg = _volume()
        self.addCleanup(shutil.rmtree, cfg, ignore_errors=True)
        _zip(cfg, "pre.zip", {"ha_version": READABLE})  # taken before the switch from v1 to v2
        for patch in (mock.patch.object(inst_mod.events, "emit"), mock.patch.object(backup_views.events, "emit"),
                      mock.patch.object(inst_mod.change_report, "snapshot", lambda hass, domain: {"entities": {}, "services": []})):
            patch.start()
            self.addCleanup(patch.stop)

        async def job(fn, *args):
            await asyncio.sleep(0)
            return fn(*args)

        inst = self.inst = object.__new__(Installer)
        inst.hass = SimpleNamespace(config=SimpleNamespace(config_dir=cfg, components={"demo"}), async_add_executor_job=job,
                                    is_running=True, loop=mock.Mock(), async_create_task=mock.Mock())
        inst.config_dir, inst.state_dir = cfg, os.path.join(cfg, backupkit.STATE_DIR)
        inst.state_file = os.path.join(inst.state_dir, "state.json")
        inst.versions_dir = os.path.join(inst.state_dir, "versions")
        inst.state = State(domain="demo", installed={"demo": {
            "versions": {"v1": {"installed_at": "a", "version": "1"}, "v2": {"installed_at": "b", "version": "2"}},
            "running_tag": "v2", "previous_tag": "v1", "pre_update_backup": "pre.zip"}})
        for tag in ("v1", "v2"):
            _write(os.path.join(inst._version_dir("demo", tag), "manifest.json"), json.dumps({"domain": "demo", "version": tag[1:]}))
            _write(os.path.join(inst._version_dir("demo", tag), "__init__.py"), f"code = {tag!r}\n")
        inst.busy = False
        inst.settings = SimpleNamespace(backup_keep=50, int_=lambda key, lo=0, hi=0: 300, bool_=lambda key: False)
        inst._smoke_handle = inst._smoke_pending = None
        inst._smoke_waiting, inst._smoke_rechecked = {}, set()
        inst._abandoned_switch, inst._restart_before_uninstall = {}, {}
        inst._requirements_for = mock.AsyncMock(return_value=[])
        inst._install_requirements = lambda reqs, force=False: []
        inst._apply_patches = lambda domain: "n/a"
        inst._loadable = mock.AsyncMock(return_value=True)
        inst._enable_entries = mock.AsyncMock(return_value=[])
        inst._entries_of = lambda domain: []

        async def async_backup(label=""):
            return await job(backupkit.create, cfg, label)

        inst.async_backup = async_backup
        inst._ensure_deployed("demo", "v2")
        # v2 is what this process imported and runs, on the config entries it migrated
        inst._loaded_tags, inst._code_hash = {"demo": "v2"}, {"demo": inst._tree_hash("demo")}
        inst._save_state()

    def rollback(self):
        res = asyncio.run(self.inst.rollback_full("demo"))
        self.assertTrue(res["ok"], res)
        self.assertTrue(backupkit.pending(self.cfg))
        self.assertEqual(self.inst.running_tag, "v1")
        return res

    def cancel(self):
        view = backup_views.RestoreCancelView(self.inst.hass, self.inst)
        view.json = lambda d: d
        return asyncio.run(backup_views.RestoreCancelView.post.__wrapped__(view, None, {}))

    def on_disk(self):
        """What the next boot works from: state.json, the deployed code and the scheduled restore."""
        with open(self.inst.state_file, encoding="utf-8") as fh:
            state = json.load(fh)
        return {"tag": state["installed"]["demo"]["running_tag"], "code": _read(os.path.join(self.inst._component_dir("demo"), "__init__.py")),
                "restore": (backupkit._pending_meta(self.cfg) or {}).get("name") if backupkit.pending(self.cfg) else None,  # noqa: SLF001
                "rollback_backup": state.get("rollback_backup")}

    def test_cancel_restore_does_not_split_a_full_rollback(self):
        self.rollback()
        res = self.cancel()
        self.assertFalse(res["ok"], res)
        self.assertIn("full rollback", res["error"])
        self.assertIn("demo v2", res["error"], "the answer says how to undo the rollback")
        # the older code boots together with the configuration it was taken with
        self.assertEqual(self.on_disk(), {"tag": "v1", "code": "code = 'v1'\n", "restore": "pre.zip", "rollback_backup": "pre.zip"})
        self.assertIn("pre.zip", self.inst.protected_backups())
        self.assertFalse(self.inst.busy)

    def test_starting_the_version_the_rollback_left_undoes_it(self):
        self.rollback()
        res = asyncio.run(self.inst.start("demo", "v2"))
        self.assertTrue(res["ok"], res)
        # the newer code with the configuration it migrated, nothing restored at the restart
        self.assertEqual(self.on_disk(), {"tag": "v2", "code": "code = 'v2'\n", "restore": None, "rollback_backup": None})
        rec = self.inst.state.installed["demo"]
        self.assertEqual((rec["previous_tag"], rec["pre_update_backup"]), ("v1", "pre.zip"), "the way back to v1 is what it was")
        self.assertFalse(res["restart_required"])  # the code this process runs
        self.assertFalse(self.inst.state.restart_required)
        self.assertIsNone(self.inst.state.rollback_at)
        self.assertEqual(self.inst.state.pending_smoke, {"domain": "demo", "tag": "v2", "can_rollback": False})
        self.assertFalse(self.inst.busy)

    def test_the_restore_is_dropped_only_once_the_newer_version_is_recorded(self):
        # a kill between the two leaves the restore of pre.zip over a state.json naming v2: the boot restores the
        # older configuration and the reconcile deploys v2, which migrates it forward again (never v1 on v2's entries)
        self.rollback()
        seen = []
        real = Installer._cancel_own_restore

        def cancel_own_restore(inst, zip_name):
            seen.append(self.on_disk()["tag"])
            return real(inst, zip_name)

        with mock.patch.object(Installer, "_cancel_own_restore", cancel_own_restore):
            self.assertTrue(asyncio.run(self.inst.start("demo", "v2"))["ok"])
        self.assertEqual(seen, ["v2"])
        self.assertFalse(backupkit.pending(self.cfg))

    def test_any_other_start_is_still_refused_while_the_rollback_restores(self):
        self.rollback()
        for tag in ("v1", None):
            res = asyncio.run(self.inst.start("demo", tag))
            self.assertFalse(res["ok"], res)
            self.assertIn("restore", res["error"])
        self.assertEqual(self.on_disk(), {"tag": "v1", "code": "code = 'v1'\n", "restore": "pre.zip", "rollback_backup": "pre.zip"})

    def test_a_restore_by_hand_is_still_cancelled(self):
        _zip(self.cfg, "manual.zip", {"ha_version": READABLE})
        backupkit.schedule_restore(self.cfg, "manual.zip")
        self.assertEqual(self.cancel(), {"ok": True, "cancelled": True})
        self.assertFalse(backupkit.pending(self.cfg))


if __name__ == "__main__":
    unittest.main()
