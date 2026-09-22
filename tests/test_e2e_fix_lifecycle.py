"""End-to-end run, lifecycle findings: the refusal of a restore or rebuild switch over a full rollback's restore,
a dev build without a GitHub repository that was never preflighted, a smoke-test record left behind by a start
that needs a restart, and a cancelled restore the timeline never recorded."""

import asyncio
import shutil
import unittest
from types import SimpleNamespace
from unittest import mock

import backupkit
from custom_components.integration_manager import backup_views, preflight
from tests.test_camp_preflight import _hass as _pf_hass, _installer as _pf_installer
from tests import test_review2_lifecycle_rollback_keep as _keep, test_review_cx_restart as _cx
from tests.test_review_backup import _hass, _volume, _zip


class RestoreOrRebuildOverAFullRollbackTest(unittest.TestCase):
    """C-F1: after a full rollback, config=restore / rebuild answered 'cancel it first', which the cancel refuses."""

    setUp = _keep.KeepSwitchAfterRollbackTest.setUp
    _write_storage = _keep.KeepSwitchAfterRollbackTest._write_storage
    _schedule = _keep.KeepSwitchAfterRollbackTest._schedule

    def _switch(self, mode):
        try:
            asyncio.run(self.views.async_change_ha_version(self.installer, self.updater, _keep.OLDER, mode, "test"))
        except ValueError as err:
            return str(err)
        return None

    def test_restore_and_rebuild_name_the_rollback_and_ask_for_the_restart(self):
        backup = self._schedule()
        for mode in ("restore", "rebuild"):
            refused = self._switch(mode)
            self.assertIsNotNone(refused, mode)
            self.assertIn(backup, refused, mode)
            self.assertIn("restart to finish the rollback first", refused, mode)
            self.assertNotIn("cancel it first", refused, mode)
        self.assertTrue(backupkit.pending(self.cfg))
        self.assertFalse(self.installer.busy)

    def test_a_restore_scheduled_by_hand_still_says_cancel_it_first(self):
        self._schedule(rollback=False)
        for mode in ("restore", "rebuild"):
            refused = self._switch(mode)
            self.assertIn("a restore scheduled on System is waiting for the restart: cancel it first", refused or "", mode)


BROKEN = {"__init__.py": "import imp\n", "legacy.py": 'print "x"\n'}
CLEAN = {"__init__.py": "x = 1\n"}
MANIFEST = {"domain": "demo", "version": "0.1", "config_flow": True, "requirements": []}


class DevBuildWithoutARepositoryTest(unittest.TestCase):
    """B-F1: install_local registers a new domain with repo "", and the gate skipped it as 'no GitHub repository'."""

    def _gate(self, files, target="local"):
        inst = _pf_installer(self, files, MANIFEST, target=target)
        inst.spec = lambda dom: {"repo": ""}  # what install_local registers for a domain the registry does not know
        preflight._REPORTS.clear()
        self.addCleanup(preflight._REPORTS.clear)
        with mock.patch.object(preflight, "_pip_dry_run", return_value={"ok": True, "install": [], "stderr": ""}):
            return asyncio.run(preflight.gate(_pf_hass(), inst, "demo", target))

    def test_a_broken_dev_build_is_blocked(self):
        res = self._gate(BROKEN)
        self.assertIsNone(res["skipped"], res)
        self.assertTrue(res["blocked"], res)
        blockers = " | ".join(res["report"]["blockers"])
        self.assertIn("does not compile", blockers)
        self.assertIn("imports imp", blockers)

    def test_a_clean_dev_build_passes_with_a_report(self):
        res = self._gate(CLEAN)
        self.assertFalse(res["blocked"], res)
        self.assertIsNone(res["skipped"])
        self.assertTrue(res["report"]["ok"])

    def test_a_release_without_a_repository_keeps_the_note(self):
        res = self._gate(BROKEN, target="2.0")
        self.assertEqual((res["blocked"], res["skipped"]), (False, "no GitHub repository known"))

    def test_run_without_a_repository_still_refuses_a_download(self):
        inst = SimpleNamespace(spec=lambda dom: {"repo": ""})
        with self.assertRaisesRegex(ValueError, "no GitHub repository known"):
            asyncio.run(preflight.run(None, inst, "demo", "1.0"))


class StaleSmokeRecordAfterARestartStartTest(unittest.TestCase):
    """B-F2: a start that needs a restart cancelled the older smoke timer but left its record in _smoke_pending."""

    setUp = _cx.ReinstalledRunningReferenceTest.setUp
    install = _cx.ReinstalledRunningReferenceTest.install
    start = _cx.ReinstalledRunningReferenceTest.start

    def test_the_answer_and_status_name_the_new_start_only(self):
        old_timer = mock.Mock()
        self.inst._smoke_handle = old_timer
        self.inst._smoke_pending = {"domain": "demo", "tag": "v6.0.0", "at": "2026-09-22T10:00:00", "auto_rollback": False}
        self.install("code = 'B'\n")
        res, _ = self.start()
        self.assertTrue(res["restart_required"], res)
        old_timer.cancel.assert_called_once()
        self.assertIsNone(self.inst._smoke_pending, "the cancelled timer's record is still reported as pending")
        self.assertIsNone(self.inst.smoke["pending"])
        self.assertEqual(res["smoke_test"], {"domain": "demo", "tag": "main", "can_rollback": False})


class CancelRestoreEventTest(unittest.TestCase):
    """B-minor: Cancel restore wrote nothing, so the timeline still read 'scheduled for the next restart'."""

    def _cancel(self, rollback=None):
        cfg = _volume()
        self.addCleanup(shutil.rmtree, cfg, True)
        _zip(cfg, "pre.zip", {"ha_version": "2026.8.3"})
        backupkit.schedule_restore(cfg, "pre.zip")
        installer = SimpleNamespace(state=SimpleNamespace(domain="demo", rollback_backup=rollback, installed={}),
                                    busy=False, running_tag="v1", _rollback_undo=None)
        view = object.__new__(backup_views.RestoreCancelView)
        view.hass, view.installer, view.json = _hass(cfg), installer, lambda d: d
        with mock.patch.object(backup_views.events, "emit") as emit:
            res = asyncio.run(backup_views.RestoreCancelView.post.__wrapped__(view, None, {}))
        return res, emit, cfg

    def test_a_cancel_is_recorded(self):
        res, emit, cfg = self._cancel()
        self.assertEqual(res, {"ok": True, "cancelled": True})
        self.assertFalse(backupkit.pending(cfg))
        emit.assert_called_once_with("restore", "pre.zip cancelled", backup="pre.zip")

    def test_a_refused_cancel_records_nothing(self):
        res, emit, cfg = self._cancel(rollback="pre.zip")
        self.assertFalse(res["ok"])
        self.assertTrue(backupkit.pending(cfg))
        emit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
