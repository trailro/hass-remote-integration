"""E2E campaign 0.17.0, integration lifecycle.

mb F1: Stop was accepted while a full rollback's restore waited for the restart; the restore brought the entries
back enabled and the integration ran with the manager recording nothing.  Stop, uninstall and removing a version
the rollback involves are refused now, and a boot that finds an installed integration with enabled entries while
nothing is recorded as running adopts it.
mb F2: a second full rollback advised Cancel restore, which is refused for a rollback's restore.
mb F3: starting a tag that is not in the store answered needs_force ("no manifest.json").
mb F5: a degraded version was kept, but its change report was dropped.
mb F7: the flow API answered 500 for an unknown config entry id.
ud 9: the "ha.json was corrupt" notification came back after every restore (the marker lived in state.json).
ud 6: an unparseable registry.json became {} without a log line."""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import backupkit
import jsonio
from homeassistant.config_entries import UnknownEntry

import custom_components.integration_manager as manager
from custom_components.integration_manager import flows as flows_mod, installer as inst_mod, preflight, views
from custom_components.integration_manager.installer import Installer, State
from tests import test_r13_life_rollback as r13


class _RollbackScheduled(unittest.TestCase):
    def setUp(self):
        r13.FullRollbackVersusCancelRestoreTest.setUp(self)
        res = asyncio.run(self.inst.rollback_full("demo"))
        self.assertTrue(res["ok"], res)
        self.assertTrue(backupkit.pending(self.cfg))
        self.assertEqual((self.inst.state.domain, self.inst.running_tag), ("demo", "v1"))

    def assert_rollback_refusal(self, res):
        self.assertFalse(res["ok"], res)
        self.assertIn("full rollback", res["error"])
        self.assertIn("restart to finish it", res["error"])
        self.assertIn("start demo v2 again to undo it", res["error"])
        self.assertNotIn("cancel", res["error"], "Cancel restore is refused for a rollback's restore")
        self.assertFalse(self.inst.busy)
        self.assertTrue(backupkit.pending(self.cfg), "the rollback's restore stays scheduled")


class ActionsDuringAFullRollbackTest(_RollbackScheduled):
    def test_stop_is_refused(self):
        res = asyncio.run(self.inst.stop())
        self.assert_rollback_refusal(res)
        self.assertEqual(self.inst.state.domain, "demo")
        self.assertEqual(self.inst.state.pending_smoke, {"domain": "demo", "tag": "v1", "can_rollback": False},
                         "the verdict after the rollback's restart is still scheduled")

    def test_uninstall_is_refused(self):
        res = asyncio.run(self.inst.uninstall("demo"))
        self.assert_rollback_refusal(res)
        self.assertIn("demo", self.inst.state.installed)

    def test_removing_a_version_the_rollback_involves_is_refused(self):
        for tag in ("v1", "v2"):  # the version it goes back to, and the one starting again undoes it
            with self.subTest(tag=tag):
                self.assert_rollback_refusal(asyncio.run(self.inst.remove_version("demo", tag)))
                self.assertIn(tag, self.inst.state.installed["demo"]["versions"])

    def test_a_second_full_rollback_gives_advice_that_works(self):
        self.assert_rollback_refusal(asyncio.run(self.inst.rollback_full("demo")))

    def test_install_names_the_rollback(self):
        self.assert_rollback_refusal(asyncio.run(self.inst.install("v3", "demo")))

    def test_without_the_undo_the_answer_only_says_restart(self):
        self.inst._rollback_undo = None  # a rollback whose way back cannot be started again
        res = asyncio.run(self.inst.stop())
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "a full rollback restores its backup at the next restart: restart to finish it")

    def test_undone_rollback_no_longer_refuses(self):
        self.assertTrue(asyncio.run(self.inst.start("demo", "v2"))["ok"])
        self.assertIsNone(self.inst.rollback_restore_refusal())

    def test_a_restore_by_hand_is_not_a_rollback(self):
        self.inst.state.rollback_backup = None
        self.assertIsNone(self.inst.rollback_restore_refusal())


class BootAdoptsEnabledEntriesTest(unittest.TestCase):
    """What F1 left behind (and a restore by hand after a stop still can): the restored .storage has the entry
    enabled, state.json records nothing running.  run.py sets that entry up at this boot anyway."""

    def setUp(self):
        self.cfg = tempfile.mkdtemp(prefix="hri-adopt-")
        self.addCleanup(shutil.rmtree, self.cfg, ignore_errors=True)
        patch = mock.patch.object(inst_mod.events, "emit")
        self.emit = patch.start()
        self.addCleanup(patch.stop)

    def boot(self, entries, installed=("hub",), marker="v1", suspended=None):
        state = {"domain": None, "installed": {d: {"versions": {"v1": {}, "v2": {}}, "running_tag": "v2"} for d in installed},
                 "suspended_entries": suspended}
        jsonio.write_json(os.path.join(self.cfg, "integration_manager", "state.json"), state)
        for d in installed:
            r13._write(os.path.join(self.cfg, "custom_components", d, "manifest.json"), json.dumps({"domain": d, "version": "1"}))
            r13._write(os.path.join(self.cfg, "custom_components", d, ".hri-tag"), f"{marker}\n2026-09-01T00:00:00\n")

        async def executor(fn, *args):
            return fn(*args)

        hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg, components=set()), async_add_executor_job=executor,
                               config_entries=SimpleNamespace(async_entries=lambda domain=None: [e for e in entries if domain in (None, e.domain)]))
        inst = Installer(hass)
        deployed = []

        async def nothing(*args, **kwargs):
            return []

        inst._ensure_deployed = lambda domain, tag, force=False: deployed.append((domain, tag)) or False
        inst._requirements_for = nothing
        inst._enable_entries = nothing
        inst._patch_rows = lambda domain: []
        inst._notify_patches = lambda domain, rows: None
        inst._apply_patches = lambda domain: "none"
        inst._tree_hash = lambda domain: None
        inst._schedule_smoke = mock.Mock()
        asyncio.run(inst.async_reconcile())
        return inst, deployed

    @staticmethod
    def entry(domain="hub", disabled_by=None, entry_id="e1"):
        return SimpleNamespace(domain=domain, disabled_by=disabled_by, entry_id=entry_id)

    def test_enabled_entries_are_adopted_as_running(self):
        inst, deployed = self.boot([self.entry()], suspended=["e1", "other"])
        self.assertEqual((inst.state.domain, inst.running_tag), ("hub", "v1"), "the deployed copy's marker names the tag")
        self.assertEqual(deployed, [("hub", "v1")])
        self.assertEqual(inst.state.suspended_entries, ["other"], "an entry enabled again is not the manager's to resume")
        with open(inst.state_file, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["domain"], "hub")
        self.assertIn("adopted at boot", self.emit.call_args_list[0].args[1])

    def test_an_unknown_marker_keeps_the_recorded_tag(self):
        inst, _ = self.boot([self.entry()], marker="v9")
        self.assertEqual((inst.state.domain, inst.running_tag), ("hub", "v2"))

    def test_disabled_entries_stay_stopped(self):
        inst, deployed = self.boot([self.entry(disabled_by="user")])
        self.assertIsNone(inst.state.domain)
        self.assertEqual(deployed, [])

    def test_two_candidates_adopt_nothing(self):
        with self.assertLogs(inst_mod._LOGGER, "ERROR"):
            inst, _ = self.boot([self.entry(), self.entry("other", entry_id="e2")], installed=("hub", "other"))
        self.assertIsNone(inst.state.domain)


class NotInTheStoreTest(unittest.TestCase):
    def test_the_gate_does_not_offer_force(self):
        from tests.test_preflight_gate import FakeInstaller, _gate

        res, run = _gate(FakeInstaller(), "v5.0.0", {"ok": False, "blockers": ["x"]})
        self.assertEqual(res, {"blocked": False, "report": None, "skipped": None})
        run.assert_not_awaited()

    def test_a_recorded_version_whose_directory_is_gone(self):
        from tests.test_camp_preflight import _hass, _installer

        inst = _installer(self, {"__init__.py": "x = 1\n"}, {"domain": "demo", "version": "2.0"})
        shutil.rmtree(inst._version_dir("demo", "2.0"))
        preflight._REPORTS.clear()
        res = asyncio.run(preflight.gate(_hass(), inst, "demo", "2.0"))
        self.assertFalse(res["blocked"], res)
        self.assertIsNone(res["skipped"])

    def test_run_start_answers_plainly(self):
        from custom_components.integration_manager import manage_views
        from tests.test_preflight_gate import FakeInstaller

        inst = FakeInstaller()
        inst.hass = None
        inst.start = mock.AsyncMock(return_value={"ok": False, "error": "probe v5.0.0 is not in the version store"})
        view = manage_views.RunView(inst, mock.Mock())
        view.json = lambda d: d
        preflight._REPORTS.clear()
        with mock.patch.object(preflight, "run", mock.AsyncMock(side_effect=AssertionError("no preflight"))):
            res = asyncio.run(manage_views.RunView.post.__wrapped__(view, None, {"domain": "probe", "tag": "v5.0.0"}, "start"))
        self.assertEqual(res, {"ok": False, "error": "probe v5.0.0 is not in the version store"})


class DegradedChangeReportTest(unittest.TestCase):
    def test_a_kept_degraded_version_gets_its_report(self):
        from tests.test_r12_install import SmokeHealthExceptionTest

        inst = SmokeHealthExceptionTest._installer(self, lambda grace: {"state": "degraded", "reason": "41 of 41 entities unavailable"})
        inst.state.pending_change = {"domain": "demo", "from_tag": "1.0", "to_tag": "2.0", "at": "x", "before": {"entities": {"a": {}}}}
        with mock.patch.object(inst_mod.events, "emit"):
            asyncio.run(inst._smoke_check("demo", "2.0", True))
        inst.async_finish_change_report.assert_awaited_once_with("demo", "2.0")
        inst.rollback_full.assert_not_awaited()
        self.assertEqual(inst.state.last_smoke["state"], "degraded")

    def test_an_error_still_drops_it(self):
        from tests.test_r12_install import SmokeHealthExceptionTest

        inst = SmokeHealthExceptionTest._installer(self, lambda grace: {"state": "error", "reason": "setup_error"})
        inst.settings = SimpleNamespace(int_=lambda key, lo, hi: 300, bool_=lambda key: False)
        inst.state.pending_change = {"domain": "demo", "from_tag": "1.0", "to_tag": "2.0", "at": "x", "before": {}}
        with mock.patch.object(inst_mod.events, "emit"):
            asyncio.run(inst._smoke_check("demo", "2.0", True))
        inst.async_finish_change_report.assert_not_awaited()
        self.assertIsNone(inst.state.pending_change)
