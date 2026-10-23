"""The manual rollback path, after a rehearsal (HRI 0.28.3: Home Assistant 2026.10.2 with ramses_cc 0.61.1 back to
2026.9.4 with 0.60.4, then forward again with the environment builder).

1. backup_keep 5 pruned the backup a rollback restores: the switch's own pre-switch backup and the integration's
   pre-update backup went after one round trip.  What going back still needs is kept, and the list says why.
2. ``POST /api/installed/<domain>/rollback_full`` scheduled a version that does not run on the running Home
   Assistant (0.60.4 reads cv.vol, gone in 2026.10): it runs that version's preflight first.
3. ``POST /api/ha/rollback`` checked the running integration only when the previous venv had to be installed again.
4. A rollback with config=restore picked a month-old backup while the integration ran and the right one while it
   was stopped: the switch's own backup is the default either way, and a backup can be chosen.
5. Messages: the builder said a target venv that is installed "is built at the restart"; a refused full rollback
   did not say why nor what to do.
6. A logger raised to DEBUG: its DEBUG lines show under the page's level filter, and their volume never rotates the
   INFO and higher lines off the disk.
"""

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import backupkit
import logbuffer
from custom_components.integration_manager import ha_updater, logs_page, manage_views, preflight, views
from custom_components.integration_manager.ha_updater import HaUpdater
from custom_components.integration_manager.installer import Installer, State
from tests.test_log_follow import _Log
from tests.test_preflight_target_pins import TARGET as PIN_TARGET, _Base as _PinsBase
from tests.test_review_backup import _volume

HA = views.HA_VERSION
OLD = "2026.1.0"  # the version before the switch, older than any this suite runs on
MONTH_OLD = "2025.12.1"


def _venv(cfg, version):
    """A finished venv of ``version`` for this Python, as the entrypoint leaves it."""
    venv = os.path.join(cfg, f"venv-{version}")
    site = os.path.join(venv, "lib", f"python{sys.version_info[0]}.{sys.version_info[1]}", "site-packages", "homeassistant")
    os.makedirs(site)
    os.makedirs(os.path.join(venv, "bin"))
    for path in (os.path.join(venv, ".ok"), os.path.join(venv, "bin", "python"), os.path.join(site, "__init__.py")):
        open(path, "w").close()


class _Volume(unittest.TestCase):
    """A volume with backups made in order (one second apart), and the integration that ran for each."""

    def setUp(self):
        self.cfg = _volume()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        self.n = 0
        self.ha({"current": HA, "previous": OLD})

    def ha(self, data):
        with open(os.path.join(self.cfg, backupkit.STATE_DIR, "ha.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def backup(self, label, made_on, domain="ramses_cc", installed=("ramses_cc",)):
        with open(os.path.join(self.cfg, backupkit.MARKER), "w", encoding="utf-8") as fh:
            json.dump({"domain": domain, "installed": {d: {} for d in installed}}, fh)
        self.n += 1
        with mock.patch.object(backupkit.time, "strftime", return_value=f"20260101-0000{self.n:02d}"):
            return backupkit.create(self.cfg, label, made_on)["name"]

    def installer(self, **installed):
        inst = object.__new__(Installer)
        inst.state_dir = os.path.join(self.cfg, backupkit.STATE_DIR)
        inst.config_dir = self.cfg
        inst.state = State(domain="ramses_cc", installed=installed or {"ramses_cc": {}})
        return inst


class BackupsARollbackNeedsTest(_Volume):
    """1: a rollback and a re-upgrade made five backups, and backup_keep 5 pruned what going back restores."""

    def test_the_switch_backup_of_the_previous_version_survives_a_round_trip(self):
        month_old = self.backup("daily", OLD)
        pre_switch = self.backup(f"pre-ha-{HA}", OLD)
        for label in ("pre-update", "pre-ha-x", "pre-update", "pre-ha-y", "daily"):
            self.backup(label, HA)
        removed = backupkit.prune(self.cfg, 5, self.installer().protected_backups())
        self.assertNotIn(pre_switch, removed, "the restore source of a rollback to the previous version was pruned")
        self.assertIn(month_old, removed, "only what a rollback restores is kept beyond the count")

    def test_an_older_venv_keeps_its_restore_source_too(self):
        _venv(self.cfg, MONTH_OLD)
        self.ha({"current": HA})  # no previous recorded: the venv alone is a version that can be switched to
        source = self.backup("daily", MONTH_OLD)
        for _ in range(6):
            self.backup("daily", HA)
        reasons = self.installer().protected_backup_reasons()
        self.assertIn(source, reasons)
        self.assertIn(f"rollback to Home Assistant {MONTH_OLD}", reasons[source])

    def test_nothing_older_is_kept_once_no_version_needs_it(self):
        self.ha({"current": HA})  # no previous, no older venv
        pre_switch = self.backup(f"pre-ha-{HA}", OLD)
        for _ in range(6):
            self.backup("daily", HA)
        self.assertNotIn(pre_switch, self.installer().protected_backups())

    def test_the_pre_update_backup_the_smoke_backup_and_a_scheduled_restore_say_why(self):
        pre_update = self.backup("pre-update", HA)
        smoke = self.backup(f"pre-ha-{HA}", OLD)
        self.ha({"current": HA, "smoke": {"version": HA, "backup": smoke}})
        restore = self.backup("by-hand", HA)
        backupkit.schedule_restore(self.cfg, restore)
        reasons = self.installer(ramses_cc={"previous_tag": "0.60.4", "pre_update_backup": pre_update}).protected_backup_reasons()
        self.assertIn("full rollback of ramses_cc to 0.60.4", reasons[pre_update])
        self.assertIn(f"smoke test of Home Assistant {HA}", reasons[smoke])
        self.assertIn("scheduled restore", reasons[restore])

    def test_a_refused_deletion_says_why(self):
        pre_switch = self.backup(f"pre-ha-{HA}", OLD)
        inst = self.installer()
        inst.busy = False

        async def job(fn, *args):
            return fn(*args)

        hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg, path=lambda *p: os.path.join(self.cfg, *p)),
                               async_add_executor_job=job)
        from custom_components.integration_manager import backup_views

        view = backup_views.BackupActionView(hass, inst)
        view.json = lambda d, *a, **k: d
        r = asyncio.run(backup_views.BackupActionView.post.__wrapped__(view, None, {}, pre_switch, "delete"))
        self.assertFalse(r["ok"])
        self.assertIn(f"still needed: a rollback to Home Assistant {OLD} with config=restore restores it", r["error"])
        self.assertIn(f"remove the venv of {OLD} to free it", r["error"])
        self.assertTrue(os.path.isfile(os.path.join(self.cfg, backupkit.BACKUP_DIR, pre_switch)))

    def test_the_backups_list_says_why_a_backup_is_kept(self):
        pre_update = self.backup("pre-update", HA)
        other = self.backup("daily", HA)
        inst = self.installer(ramses_cc={"previous_tag": "0.60.4", "pre_update_backup": pre_update})
        inst.settings = SimpleNamespace(backup_keep=5)

        async def job(fn, *args):
            return fn(*args)

        hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg, path=lambda *p: os.path.join(self.cfg, *p)),
                               async_add_executor_job=job)
        from custom_components.integration_manager import backup_views

        view = backup_views.BackupsView(hass, inst)
        view.json = lambda d, *a, **k: d
        listed = {b["name"]: b for b in asyncio.run(view.get(None))["backups"]}
        self.assertIn("full rollback of ramses_cc", listed[pre_update]["kept_because"])
        self.assertNotIn("kept_because", listed[other])


class RestoreSourceTest(_Volume):
    """4: the backup a rollback with config=restore takes .storage from, whether the integration runs or not."""

    def setUp(self):
        super().setUp()
        self.month_old = self.backup("daily", OLD)  # made on the target while ramses_cc ran
        self.pre_switch = self.backup(f"pre-ha-{HA}", OLD, domain=None)  # the builder's switch: ramses_cc stopped at that moment

    def source(self, running):
        return ha_updater.restore_source(self.cfg, OLD, HA, None, running, {"ramses_cc"})

    def test_the_switch_backup_is_the_default_whether_the_integration_runs_or_not(self):
        for running in ("ramses_cc", None):
            with self.subTest(running=running):
                b, why = self.source(running)
                self.assertEqual(b["name"], self.pre_switch)
                self.assertIn(f"before the switch from Home Assistant {OLD} to {HA}", why)

    def test_a_switch_backup_of_another_integration_is_not_the_default(self):
        other = self.backup(f"pre-ha-{HA}", OLD, domain="xdom", installed=("xdom",))
        b, _why = self.source("ramses_cc")
        self.assertNotEqual(b["name"], other)
        self.assertEqual(b["name"], self.pre_switch)

    def test_without_a_switch_backup_the_newest_of_the_integration_says_so(self):
        os.remove(os.path.join(self.cfg, backupkit.BACKUP_DIR, self.pre_switch))
        b, why = self.source("ramses_cc")
        self.assertEqual(b["name"], self.month_old)
        self.assertIn("no backup taken before a switch", why)

    def test_a_switch_backup_from_before_the_integration_was_installed_is_not_the_default(self):
        os.remove(os.path.join(self.cfg, backupkit.BACKUP_DIR, self.pre_switch))
        bare = self.backup(f"pre-ha-{HA}", OLD, domain=None, installed=())  # nothing installed at the switch
        b, why = self.source("ramses_cc")
        self.assertEqual(b["name"], self.month_old, "a .storage without ramses_cc's config entries was the default")
        self.assertIn(f"{bare} was made before the integration was installed", why)

    def test_when_it_is_the_only_one_it_is_restored_with_a_warning(self):
        for name in (self.pre_switch, self.month_old):
            os.remove(os.path.join(self.cfg, backupkit.BACKUP_DIR, name))
        bare = self.backup(f"pre-ha-{HA}", OLD, domain=None, installed=())
        b, why = self.source("ramses_cc")
        self.assertEqual(b["name"], bare)
        self.assertIn("Warning", why)
        self.assertIn("ramses_cc's config entries are not in it", why)

    def test_one_made_while_the_integration_was_stopped_says_so(self):
        _b, why = self.source("ramses_cc")
        self.assertIn("ramses_cc was installed but stopped", why)

    def test_the_plan_on_system_names_it_and_why(self):
        with open(os.path.join(self.cfg, backupkit.MARKER), "w", encoding="utf-8") as fh:
            json.dump({"domain": "ramses_cc", "installed": {"ramses_cc": {}}}, fh)
        up = HaUpdater(SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg, path=lambda *p: os.path.join(self.cfg, *p))))
        plan = up._config_backups([OLD], HA)[OLD]
        self.assertEqual(plan["name"], self.pre_switch)
        self.assertIn("before the switch", plan["why"])


class _Rollback(_Volume):
    """POST /api/ha/rollback to OLD, whose venv is still on the volume, with ramses_cc 0.61.1 running."""

    def setUp(self):
        super().setUp()
        self.month_old = self.backup("daily", OLD)
        self.pre_switch = self.backup(f"pre-ha-{HA}", OLD, domain=None)
        self.newer = self.backup("daily", HA)
        self.theirs = self.backup("daily", OLD, domain="xdom", installed=("xdom",))
        with open(os.path.join(self.cfg, backupkit.MARKER), "w", encoding="utf-8") as fh:
            json.dump({"domain": "ramses_cc", "installed": {"ramses_cc": {}}}, fh)
        for p in (mock.patch.object(views.events, "emit"), mock.patch.object(backupkit, "prune", return_value=[])):
            p.start()
            self.addCleanup(p.stop)

        async def job(fn, *args):
            return fn(*args)

        self.hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg, path=lambda *p: os.path.join(self.cfg, *p), components=set()),
                                    async_add_executor_job=job)
        self.up = HaUpdater(self.hass)
        self.up.validate = mock.AsyncMock()
        self.up._venv_for_this_python = lambda version: True

        async def async_backup(label=""):
            return backupkit.create(self.cfg, label, HA)

        self.inst = SimpleNamespace(hass=self.hass, busy=False, running="ramses_cc", running_tag="0.61.1", _entries_of=lambda _d: [],
                                    component_dir=lambda d: os.path.join(self.cfg, "custom_components", d), config_dir=self.cfg,
                                    state=SimpleNamespace(rollback_backup=None), settings=SimpleNamespace(backup_keep=50),
                                    async_backup=async_backup, protected_backups=set, min_ha_of=lambda *_a: None)
        self.view = views.HaActionView(self.up, self.inst)
        self.view.json = lambda d, *a, **k: d

    def rollback(self, **body):
        async def payload():
            return body

        return asyncio.run(self.view.post(SimpleNamespace(content_type="application/json", json=payload), "rollback"))


class RollbackRestoreChoiceTest(_Rollback):
    """4: POST /api/ha/rollback with config=restore: the default, and a chosen backup (validated)."""

    def test_the_default_is_the_switch_backup_with_the_integration_running(self):
        r = self.rollback(config="restore")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["restore"], self.pre_switch, "a month-old backup was picked while the integration ran")
        self.assertIn("before the switch", r["restore_why"])
        self.assertEqual(backupkit._pending_meta(self.cfg)["name"], self.pre_switch)

    def test_a_chosen_backup_is_restored(self):
        r = self.rollback(config="restore", backup=self.month_old)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["restore"], self.month_old)
        self.assertEqual(r["restore_why"], "the backup chosen")
        self.assertEqual(backupkit.pending_parts(self.cfg), ["storage"])

    def test_a_chosen_backup_that_does_not_fit_is_refused_and_nothing_is_scheduled(self):
        pre_restore = self.backup("pre-restore", OLD)
        for name, words in ((self.newer, "newer than"), (self.theirs, "while xdom ran"), (pre_restore, "before a restore"),
                            ("nope.zip", "not a backup on this volume")):
            with self.subTest(name=name):
                r = self.rollback(config="restore", backup=name)
                self.assertFalse(r["ok"], r)
                self.assertIn(words, r["error"])
                self.assertFalse(backupkit.pending(self.cfg))
        r = self.rollback(config="keep", backup=self.month_old)
        self.assertFalse(r["ok"])
        self.assertIn("config=restore", r["error"])
        r = self.rollback(config="restore", backup=["x"])
        self.assertFalse(r["ok"])
        self.assertIn("name of a backup", r["error"])


class HaRollbackChecksTheIntegrationTest(_Rollback):
    """3: the previous venv is still there, and the running integration does not run on it."""

    def setUp(self):
        super().setUp()
        self.blockers = [f"ramses_cc 0.61.1 uses names Home Assistant {OLD} no longer has (x.py:1 uses homeassistant.x.y)"]

        async def switch_blockers(installer, version, integration=None):
            integration.extend(self.blockers)
            return list(self.blockers), []

        self.up.switch_blockers = mock.AsyncMock(side_effect=switch_blockers)
        self.up.dependency_check = mock.AsyncMock()

    def test_it_needs_force_and_says_to_stop_the_integration_first(self):
        r = self.rollback(config="keep")
        self.assertFalse(r["ok"])
        self.assertTrue(r["needs_force"])
        self.assertIn("no longer has", r["error"])
        self.assertIn("stop it first", r["advice"])
        self.assertIn("stop ramses_cc, roll back Home Assistant, then start", r["advice"])
        self.up.switch_blockers.assert_awaited()
        self.up.dependency_check.assert_not_awaited()  # its venv is installed: nothing to resolve
        self.assertIsNone(self.up._read().get("desired"))
        r = self.rollback(config="keep", force=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.up._read()["desired"], OLD)

    def test_a_blocker_that_is_not_the_integration_has_no_stop_advice(self):
        async def left(installer, version, integration=None):
            return [f"Home Assistant {version} was left automatically on t"], []

        self.up.switch_blockers = mock.AsyncMock(side_effect=left)
        r = self.rollback(config="keep")
        self.assertTrue(r["needs_force"])
        self.assertNotIn("advice", r)

    def test_without_a_blocker_it_goes_through(self):
        self.blockers = []
        r = self.rollback(config="keep")
        self.assertTrue(r["ok"], r)


class FullRollbackPreflightTest(unittest.IsolatedAsyncioTestCase):
    """2: the version a full rollback goes back to is checked against the running Home Assistant first."""

    async def asyncSetUp(self):
        async def job(fn, *args):
            return fn(*args)

        self.refusal = None
        self.inst = SimpleNamespace(hass=SimpleNamespace(async_add_executor_job=job), rollback_full_refusal=lambda d: self.refusal,
                                    rollback_full=mock.AsyncMock(return_value={"ok": True, "tag": "0.60.4"}),
                                    state=SimpleNamespace(installed={"ramses_cc": {"previous_tag": "0.60.4", "running_tag": "0.61.1",
                                                                                   "pre_update_backup": "pre.zip"}}))
        self.view = manage_views.InstalledActionView(self.inst, SimpleNamespace())
        self.view.json = lambda d, *a, **k: d

    async def post(self, body, blockers, kinds=(), min_ha=None):
        report = {"ok": not blockers, "blockers": blockers, "warnings": [], "blocker_kinds": list(kinds),
                  "versions": {"min_ha": min_ha}}
        gate = mock.AsyncMock(return_value={"blocked": bool(blockers), "report": report, "skipped": None})
        with mock.patch.object(preflight, "gate", gate), mock.patch.object(manage_views.events, "emit") as emit:
            r = await manage_views.InstalledActionView.post.__wrapped__(self.view, None, body, "ramses_cc", "rollback_full")
        return r, gate, emit

    async def test_a_name_this_home_assistant_no_longer_has_advises_rolling_it_back_first(self):
        r, gate, _emit = await self.post({}, [f"the integration uses names Home Assistant {HA} no longer has (cv.vol)"], ["ha_attributes"])
        self.assertFalse(r["ok"])
        self.assertTrue(r["needs_force"])
        self.assertIn("cv.vol", r["error"])
        self.assertIn("switch Home Assistant first", r["advice"])
        self.assertIn("then start 0.60.4", r["advice"])
        gate.assert_awaited_with(self.inst.hass, self.inst, "ramses_cc", "0.60.4")
        self.inst.rollback_full.assert_not_awaited()

    async def test_a_version_that_needs_a_newer_home_assistant_does_not_advise_a_rollback(self):
        r, _gate, _emit = await self.post({}, [f"needs Home Assistant >= 2099.1.0, target is {HA}"], ["min_ha"], "2099.1.0")
        self.assertTrue(r["needs_force"])
        self.assertIn("needs a newer Home Assistant (2099.1.0 or later)", r["advice"])
        self.assertNotIn("roll back Home Assistant", r["advice"])

    async def test_a_requirement_this_home_assistant_pins_has_no_advice(self):
        r, _gate, _emit = await self.post({}, [f"requirements cannot be resolved: x (serialx pinned by the running Home Assistant {HA}: ...)"],
                                          ["ha_pins"])
        self.assertTrue(r["needs_force"])
        self.assertNotIn("advice", r)

    async def test_text_that_names_home_assistant_alone_gives_no_advice(self):
        r, _gate, _emit = await self.post({}, ["dependency 'usb' is not available in this Home Assistant"])
        self.assertTrue(r["needs_force"])
        self.assertNotIn("advice", r)

    async def test_a_rollback_that_is_not_possible_is_refused_before_the_preflight(self):
        self.refusal = "full rollback not possible: no pre-update backup is recorded for going back to 0.60.4"
        r, gate, _emit = await self.post({}, ["x"], ["ha_attributes"])
        self.assertEqual(r, {"ok": False, "error": self.refusal})
        gate.assert_not_awaited()
        self.inst.rollback_full.assert_not_awaited()

    async def test_force_skips_the_check_and_says_so(self):
        r, gate, emit = await self.post({"force": True}, ["x"], ["ha_attributes"])
        self.assertTrue(r["ok"])
        gate.assert_not_awaited()
        self.inst.rollback_full.assert_awaited_with("ramses_cc", expect=("0.60.4", "pre.zip"))
        self.assertIn("overridden", emit.call_args.args[1])

    async def test_a_version_that_runs_here_goes_through(self):
        r, _gate, _emit = await self.post({}, [])
        self.assertTrue(r["ok"])
        self.inst.rollback_full.assert_awaited_with("ramses_cc", expect=("0.60.4", "pre.zip"))


class PreflightBlockerKindsTest(_PinsBase):
    """2: what the report says its blockers are about, for the advice above."""

    def test_a_pin_of_the_running_home_assistant_is_ha_pins(self):
        report = self.report()
        self.assertIn("ha_pins", report["blocker_kinds"])
        self.assertNotIn("ha_attributes", report["blocker_kinds"])

    def test_a_clean_report_has_none(self):
        self.assertEqual(self.report(PIN_TARGET)["blocker_kinds"], [])


class RefusedFullRollbackSaysWhyTest(unittest.TestCase):
    """5b"""

    def refusal(self, rec):
        inst = object.__new__(Installer)
        inst.state = State(domain="ramses_cc", installed={"ramses_cc": rec})
        inst.config_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, inst.config_dir, True)
        inst.hass = SimpleNamespace(async_add_executor_job=lambda fn, *a: asyncio.sleep(0, fn(*a)))
        return asyncio.run(inst._rollback_full("ramses_cc", False))["error"]

    def test_no_previous_version(self):
        err = self.refusal({"running_tag": "0.60.4", "versions": {"0.60.4": {}}})
        self.assertIn("no previous version of ramses_cc is recorded", err)
        self.assertIn("Install the version you want to go back to", err)

    def test_no_pre_update_backup(self):
        err = self.refusal({"running_tag": "0.61.1", "previous_tag": "0.60.4", "versions": {}})
        self.assertIn("no pre-update backup is recorded for going back to 0.60.4", err)
        self.assertIn("start it", err)

    def test_the_previous_version_was_removed_from_the_store(self):
        inst_cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, inst_cfg, True)
        os.makedirs(os.path.join(inst_cfg, backupkit.BACKUP_DIR))
        open(os.path.join(inst_cfg, backupkit.BACKUP_DIR, "pre.zip"), "wb").close()
        inst = object.__new__(Installer)
        inst.state = State(domain="ramses_cc", installed={"ramses_cc": {"running_tag": "0.61.1", "previous_tag": "0.60.4",
                                                                        "pre_update_backup": "pre.zip", "versions": {"0.61.1": {}}}})
        inst.config_dir = inst_cfg
        inst.hass = SimpleNamespace(async_add_executor_job=lambda fn, *a: asyncio.sleep(0, fn(*a)))
        err = asyncio.run(inst._rollback_full("ramses_cc", False))["error"]
        self.assertIn("0.60.4 was removed from the version store", err)
        self.assertIn("Install 0.60.4 again", err)


class FullRollbackRefusalsOnTheVolumeTest(_Volume):
    """F2, F4: a pre-update backup made on a newer Home Assistant than the one that boots next; a switch scheduled."""

    def refusal(self, made_on):
        backup = self.backup("pre-update", made_on)
        inst = self.installer(ramses_cc={"running_tag": "0.61.1", "previous_tag": "0.60.4", "pre_update_backup": backup,
                                         "versions": {"0.60.4": {}, "0.61.1": {}}})
        return inst.rollback_full_refusal("ramses_cc"), backup

    def test_a_backup_made_on_a_newer_home_assistant_is_refused_at_once(self):
        why, backup = self.refusal("2099.1.0")
        self.assertIn("Only a plain start of 0.60.4 is possible here", why)
        self.assertIn(f"its pre-update backup {backup} can be restored once Home Assistant is 2099.1.0 again", why)
        self.assertIsNone(self.refusal(HA)[0])

    def test_a_scheduled_switch_is_refused(self):
        self.ha({"current": HA, "previous": OLD, "desired": "2099.1.0"})
        why, _backup = self.refusal(HA)
        self.assertIn("a switch to Home Assistant 2099.1.0 is scheduled", why)

    def test_a_target_that_changed_since_its_check_is_refused(self):
        backup = self.backup("pre-update", HA)
        inst = self.installer(ramses_cc={"running_tag": "0.61.1", "previous_tag": "0.60.4", "pre_update_backup": backup,
                                         "versions": {"0.60.4": {}, "0.61.1": {}}})
        inst.hass = SimpleNamespace(async_add_executor_job=lambda fn, *a: asyncio.sleep(0, fn(*a)))
        r = asyncio.run(inst._rollback_full("ramses_cc", False, expect=("0.59.0", "older.zip")))
        self.assertFalse(r["ok"])
        self.assertIn("changed while it was checked (now 0.60.4", r["error"])


class RollbackFullRefusalTest(unittest.TestCase):
    """2: what the view asks before the preflight, from the record and the volume alone."""

    def test_the_cheap_refusals(self):
        cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cfg, True)
        os.makedirs(os.path.join(cfg, backupkit.BACKUP_DIR))
        inst = object.__new__(Installer)
        inst.config_dir = cfg
        rec = {"running_tag": "0.61.1", "previous_tag": "0.60.4", "pre_update_backup": "pre.zip", "versions": {"0.60.4": {}}}
        inst.state = State(domain="ramses_cc", installed={"ramses_cc": rec})
        self.assertIn("pre.zip no longer exists", inst.rollback_full_refusal("ramses_cc"))
        open(os.path.join(cfg, backupkit.BACKUP_DIR, "pre.zip"), "wb").close()
        self.assertIsNone(inst.rollback_full_refusal("ramses_cc"))
        rec["versions"] = {}
        self.assertIn("removed from the version store", inst.rollback_full_refusal("ramses_cc"))
        rec["pre_update_backup"] = None
        self.assertIn("no pre-update backup is recorded", inst.rollback_full_refusal(None))


class BuilderVenvMessageTest(_PinsBase):
    """5a: the Check of the environment builder for a Home Assistant whose venv is on the volume."""

    def test_a_target_venv_not_installed_is_built_at_the_restart(self):
        report = self.report(PIN_TARGET)
        self.assertTrue(any(f"the {PIN_TARGET} venv is built at the restart" in w for w in report["warnings"]), report["warnings"])

    def test_an_installed_target_venv_is_said_to_be_installed(self):
        _venv(self.inst.config_dir, PIN_TARGET)
        report = self.report(PIN_TARGET)
        self.assertTrue(any(f"the {PIN_TARGET} venv is installed here" in w for w in report["warnings"]), report["warnings"])
        self.assertFalse(any("built at the restart" in w for w in report["warnings"]), report["warnings"])


class DebugLoggerTest(unittest.TestCase):
    """6"""

    def setUp(self):
        self.log = _Log(self, max_bytes=4000, keep=1)
        logs_page._SET_LEVELS.clear()
        self.addCleanup(logs_page._SET_LEVELS.clear)

    def test_a_debug_flood_does_not_rotate_the_info_lines_away(self):
        self.log.write("INFO kept", logger="other.lib", level=logging.INFO)
        self.log.write("WARNING kept", logger="ramses_tx.transport", level=logging.WARNING)
        for i in range(200):  # many times what the INFO files hold
            self.log.write(f"frame {i}", logger="ramses_tx.transport", level=logging.DEBUG)
        self.log.write("INFO after", logger="ramses_tx.transport", level=logging.INFO)
        info = [r["message"] for r in self.log.api(level="INFO", limit=50)["records"]]
        self.assertEqual(info, ["INFO kept", "WARNING kept", "INFO after"])
        self.assertTrue(os.path.isfile(self.log.handler.debug_path))
        debug = self.log.api(level="DEBUG", limit=5)["records"]
        self.assertEqual([r["message"] for r in debug], ["frame 196", "frame 197", "frame 198", "frame 199", "INFO after"])
        self.assertEqual([r["id"] for r in debug], sorted(r["id"] for r in debug))

    def test_a_follower_reads_both_files_in_order_and_never_skips_a_line(self):
        for i in range(6):
            self.log.write(f"line {i}", level=logging.DEBUG if i % 2 else logging.INFO)
        first = self.log.api(level="DEBUG", limit=200)
        self.assertEqual([r["message"] for r in first["records"]], [f"line {i}" for i in range(6)])
        since = first["records"][1]["id"]
        page = self.log.api(level="DEBUG", limit=2, since_id=since)
        self.assertEqual([r["message"] for r in page["records"]], ["line 2", "line 3"])
        self.assertTrue(page["truncated"])
        rest = self.log.api(level="DEBUG", limit=200, since_id=page["cursor"])
        self.assertEqual([r["message"] for r in rest["records"]], ["line 4", "line 5"])
        quiet = self.log.api(level="WARNING", limit=200, since_id=0)
        self.assertEqual(quiet["cursor"], rest["records"][-1]["id"], "lines a level leaves out still move the cursor")

    def test_a_line_still_being_written_holds_the_cursor_before_it(self):
        self.log.write("info", level=logging.INFO)
        self.log.write("debug", level=logging.DEBUG)
        last = self.log.api(level="DEBUG", limit=200)["records"][-1]["id"]
        with open(self.log.handler.path, "a", encoding="utf-8") as fh:
            fh.write(f'{{"id": {last + 1}, "ts": "2026')  # cut while it is written
        with open(self.log.handler.debug_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": last + 2, "ts": "t", "level": "DEBUG", "levelno": 10, "logger": "x", "message": "after"}) + "\n")
        for since in (0, last):
            with self.subTest(since=since):
                r = self.log.api(level="DEBUG", limit=200, since_id=since)
                self.assertNotIn("after", [x["message"] for x in r["records"]])
                self.assertLessEqual(r["cursor"], last)

    def follow(self, since):
        shown = []
        for _ in range(3):
            r = self.log.api(level="DEBUG", limit=200, since_id=since)
            shown += [x["message"] for x in r["records"]]
            since = r["cursor"]
        return shown

    def test_a_line_written_to_the_file_read_first_is_not_skipped_on_a_follow(self):
        self.log.write("start", level=logging.INFO)
        since = self.log.api(level="DEBUG", limit=200)["cursor"]
        for i, level in enumerate((logging.DEBUG, logging.INFO, logging.DEBUG, logging.INFO)):
            self.log.write(f"line {i}", level=level)
        real = logbuffer._oldest_first
        calls = []

        def hooked(handles, since_id):
            calls.append(handles)
            yield from real(handles, since_id)
            if len(calls) == 2 and not getattr(hooked, "done", False):  # the DEBUG files are read to their end: two more lines come
                hooked.done = True
                self.log.write("late debug", level=logging.DEBUG)
                self.log.write("late info", level=logging.INFO)

        with mock.patch.object(logbuffer, "_oldest_first", hooked):
            shown = self.follow(since)
        self.assertEqual(shown, ["line 0", "line 1", "line 2", "line 3", "late debug", "late info"])

    def test_a_line_written_to_the_file_read_first_is_not_skipped_on_a_page_load(self):
        for i, level in enumerate((logging.INFO, logging.DEBUG, logging.INFO, logging.DEBUG)):
            self.log.write(f"line {i}", level=level)
        real = logbuffer._read_lines
        state = {"done": False}

        def hooked(path):
            if path == self.log.handler.debug_path and not state["done"]:  # process.log is read: two more lines come
                state["done"] = True
                self.log.write("late info", level=logging.INFO)
                self.log.write("late debug", level=logging.DEBUG)
            return real(path)

        with mock.patch.object(logbuffer, "_read_lines", hooked):
            first = self.log.api(level="DEBUG", limit=200)
        self.assertEqual([x["message"] for x in first["records"]], ["line 0", "line 1", "line 2", "line 3"])
        self.assertEqual(self.follow(first["cursor"]), ["late info", "late debug"])

    def test_a_debug_line_torn_by_a_crash_does_not_blank_the_view_after_a_restart(self):
        self.log.write("before", level=logging.INFO)
        self.log.write("debug before", level=logging.DEBUG)
        path = self.log.handler.path
        self.log.handler.close()
        with open(self.log.handler.debug_path, "a", encoding="utf-8") as fh:
            fh.write('{"id": 3, "ts": "2026')  # killed while writing; no DEBUG record comes after the restart
        self.log.handler = logbuffer.FileLogHandler(path, max_bytes=4000, keep=1)
        self.addCleanup(self.log.handler.close)
        first = self.log.api(level="DEBUG", limit=200)
        self.log.write("after 1", level=logging.INFO)
        self.log.write("after 2", level=logging.INFO)
        shown = self.follow(first["cursor"])
        self.assertEqual(shown, ["after 1", "after 2"])
        self.assertEqual([x["message"] for x in self.log.api(level="DEBUG", limit=200)["records"]],
                         ["before", "debug before", "after 1", "after 2"])

    def test_a_logger_set_to_debug_shows_its_debug_lines_under_a_higher_filter(self):
        async def payload():
            return {"logger": "ramses_tx", "level": "DEBUG"}

        resp = asyncio.run(logs_page.LogLevelView().post(SimpleNamespace(content_type="application/json", json=payload)))
        self.addCleanup(logging.getLogger("ramses_tx").setLevel, logging.NOTSET)
        self.assertEqual(json.loads(resp.body)["level"], "DEBUG")
        self.log.write("other info", logger="other.lib", level=logging.INFO)
        self.log.write("other debug", logger="other.lib", level=logging.DEBUG)
        self.log.write("ramses debug", logger="ramses_tx.transport", level=logging.DEBUG)
        self.log.write("ramses info", logger="ramses_tx.transport", level=logging.INFO)
        self.log.write("ramses warning", logger="ramses_tx.transport", level=logging.WARNING)
        r = self.log.api(level="INFO", limit=200)
        self.assertEqual([x["message"] for x in r["records"]], ["other info", "ramses debug", "ramses info", "ramses warning"])
        self.assertEqual(r["raised"], {"ramses_tx": "DEBUG"})
        r = self.log.api(level="WARNING", limit=200, prefix="ramses_tx")
        self.assertEqual([x["message"] for x in r["records"]], ["ramses debug", "ramses info", "ramses warning"])

        async def back():
            return {"logger": "ramses_tx", "level": None}

        asyncio.run(logs_page.LogLevelView().post(SimpleNamespace(content_type="application/json", json=back)))
        r = self.log.api(level="INFO", limit=200)
        self.assertEqual([x["message"] for x in r["records"]], ["other info", "ramses info", "ramses warning"])
        self.assertNotIn("raised", r)


if __name__ == "__main__":
    unittest.main()
