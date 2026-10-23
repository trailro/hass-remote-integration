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
import os
import shutil
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import backupkit
from custom_components.integration_manager import ha_updater, views
from custom_components.integration_manager.ha_updater import HaUpdater
from custom_components.integration_manager.installer import Installer, State
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


if __name__ == "__main__":
    unittest.main()
