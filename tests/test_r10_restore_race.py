"""Review round 10: a restore scheduled by hand and a Home Assistant version change prepared at the same time.

The restore by hand (BackupActionView) checked installer.busy and ha.json's change, awaited several executor jobs,
then scheduled its archive without holding _HA_CHANGE_LOCK or reserving busy; a version change prepares under
both.  The archive's own lock protects each write, not the check-and-schedule sequence, so the two could both
answer ok while one plan replaced the other.  Each test stops one request at a chosen executor job, runs the
other to its end, lets the first finish, and then checks the pending archive, its for_version, ha.json's change
and what the entrypoint would do at the next boot: every request that answered ok is what the boot does."""

import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

import backupkit
from tests.fakes import entrypoint_for
from tests.test_review_backup import _volume, _zip

RUNNING = "2026.8.3"
OLDER = "2026.1.0"
READABLE = "2025.12.0"  # a backup both versions can read: the archive's own version check does not stop it


class _Pauses:
    """Executor jobs that wait: before running (a check about to be made, a write about to be committed) or after
    (a value read, the coroutine not resumed yet)."""

    def __init__(self):
        self.points = []

    def add(self, match, after=False):
        point = SimpleNamespace(match=match, after=after, reached=asyncio.Event(), go=asyncio.Event(), used=False)
        self.points.append(point)
        return point

    async def wait(self, fn, args, after):
        for p in self.points:
            if not p.used and p.after == after and p.match(fn, args):
                p.used = True
                p.reached.set()
                await p.go.wait()


class RestoreByHandVersusVersionChangeTest(unittest.TestCase):

    def setUp(self):
        from custom_components.integration_manager import backup_views, ha_updater, views

        self.views, self.backup_views = views, backup_views
        self.cfg = _volume()
        _zip(self.cfg, "manual.zip", {"ha_version": READABLE})
        _zip(self.cfg, "running.zip", {"ha_version": RUNNING})
        _zip(self.cfg, "old.zip", {"ha_version": OLDER})
        for module in (views, backup_views):
            p = mock.patch.object(module, "HA_VERSION", RUNNING)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(views.events, "emit")
        p.start()
        self.addCleanup(p.stop)
        self.pauses = _Pauses()
        pauses = self.pauses

        async def executor(fn, *args):
            await pauses.wait(fn, args, after=False)
            result = fn(*args)
            await pauses.wait(fn, args, after=True)
            return result

        cfg = self.cfg
        self.hass = SimpleNamespace(config=SimpleNamespace(config_dir=cfg, path=lambda *p: os.path.join(cfg, *p)), async_add_executor_job=executor)
        self.updater = ha_updater.HaUpdater(self.hass)
        self.backup_gate = None

        async def async_backup(label=""):
            rec = await executor(backupkit.create, cfg, label)
            if self.backup_gate is not None:  # the pre-change backup is the long part of a version change
                self.backup_gate.reached.set()
                await self.backup_gate.go.wait()
            return rec

        self.installer = SimpleNamespace(hass=self.hass, busy=False, running=None, running_tag=None, protected_backups=set,
                                         settings=SimpleNamespace(backup_keep=50), async_backup=async_backup)

    def test_a_restore_by_hand_does_not_replace_a_full_rollbacks_own_restore(self):
        backupkit.schedule_restore(self.cfg, "running.zip", force=True)  # what _rollback_full schedules
        self.installer.state = SimpleNamespace(rollback_backup="running.zip")
        result = asyncio.run(self.restore_by_hand())
        self.assertFalse(result["ok"])
        self.assertIn("full rollback", result["error"])
        self.assertEqual(backupkit._pending_meta(self.cfg)["name"], "running.zip")  # noqa: SLF001
        self.assertFalse(self.installer.busy)

    # ----- the two requests -----

    async def restore_by_hand(self, body=None, name="manual.zip"):
        view = self.backup_views.BackupActionView(self.hass, self.installer, self.updater)
        view.json = lambda d: d
        return await self.backup_views.BackupActionView.post.__wrapped__(view, None, body or {}, name, "restore")

    async def version_change(self, mode):
        try:
            if mode == "restore":  # what a restore with its backup's version does (BackupActionView, ha=backup)
                result = await self.views.async_change_ha_version(self.installer, self.updater, OLDER, "restore", "restore", restore_backup="old.zip")
            else:
                result = await self.views.async_change_ha_version(self.installer, self.updater, OLDER, mode, "update")
            return {"ok": True, **result}
        except ValueError as err:
            return {"ok": False, "error": str(err)}

    # ----- what is scheduled, and what the next boot does -----

    def boot(self):
        with open(os.path.join(self.cfg, backupkit.STATE_DIR, "ha.json"), encoding="utf-8") as fh:
            state = json.load(fh)
        scheduled = {"pending": backupkit._pending_meta(self.cfg) if backupkit.pending(self.cfg) else None, "change": state.get("change")}
        ep = entrypoint_for(self, self.cfg)
        wanted = state.get("desired") or state["current"]
        with mock.patch.object(ep, "venv_ok", lambda _v: True), mock.patch.object(ep, "log", lambda *_: None):
            booted = ep.apply_config_changes(state, wanted, state["current"])
        return scheduled, booted, state

    def assert_each_ok_is_what_boots(self, manual, change, mode):
        self.assertFalse(self.installer.busy)
        self.assertFalse(self.views._HA_CHANGE_LOCK.locked())
        self.assertFalse(manual["ok"] and change["ok"], f"both answered ok, one plan replaced the other: {manual} / {change}")
        self.assertTrue(manual["ok"] or change["ok"], f"neither went through: {manual} / {change}")
        scheduled, booted, state = self.boot()
        if manual["ok"]:
            self.assertEqual((scheduled["pending"] or {}).get("name"), "manual.zip")
            self.assertIsNone(scheduled["pending"].get("for_version"))
            self.assertIsNone(scheduled["change"])
            self.assertEqual(booted, RUNNING)
            self.assertEqual((state.get("last_restore") or {}).get("backup"), "manual.zip")
            self.assertTrue(state["last_restore"]["ok"], state["last_restore"])
        else:
            self.assertEqual((scheduled["change"] or {}).get("to"), OLDER)
            self.assertEqual(scheduled["change"].get("mode"), mode)
            self.assertEqual(booted, OLDER, state.get("last_error"))
            self.assertEqual(state.get("desired"), OLDER)
            self.assertTrue(state["change"].get("applied"), state)
            if mode == "restore":
                self.assertEqual(scheduled["pending"].get("name"), "old.zip")
                self.assertEqual(scheduled["pending"].get("for_version"), OLDER)
                self.assertEqual((state.get("last_restore") or {}).get("backup"), "old.zip")
            else:
                self.assertIsNone(scheduled["pending"])
                self.assertFalse(state.get("last_error"), state)

    # ----- interleavings -----

    def _change_while_the_restore_is_about_to_commit(self, mode):
        async def run():
            commit = self.pauses.add(lambda fn, args: fn is backupkit.schedule_restore and args[1] == "manual.zip")
            manual = asyncio.ensure_future(self.restore_by_hand())
            await asyncio.wait_for(commit.reached.wait(), 5)  # every check of the restore made, its schedule not written
            change = await asyncio.wait_for(self.version_change(mode), 5)
            commit.go.set()
            return await asyncio.wait_for(manual, 5), change

        manual, change = asyncio.run(run())
        self.assert_each_ok_is_what_boots(manual, change, mode)

    def test_a_restore_with_its_version_prepared_before_the_restore_by_hand_commits(self):
        self._change_while_the_restore_is_about_to_commit("restore")

    def test_a_clean_start_prepared_before_the_restore_by_hand_commits(self):
        self._change_while_the_restore_is_about_to_commit("rebuild")

    def test_a_restore_by_hand_validated_before_a_version_change_began(self):
        # the change sets busy and checks for a restore by hand before its backup; this restore passed its busy
        # check just before, and schedules while the backup is written
        async def run():
            checked = self.pauses.add(lambda fn, args: fn is backupkit.describe and args[1] == "manual.zip")
            self.backup_gate = SimpleNamespace(reached=asyncio.Event(), go=asyncio.Event())
            manual = asyncio.ensure_future(self.restore_by_hand())
            await asyncio.wait_for(checked.reached.wait(), 5)
            change = asyncio.ensure_future(self.version_change("restore"))
            await asyncio.wait_for(self.backup_gate.reached.wait(), 5)
            checked.go.set()
            manual_result = await asyncio.wait_for(manual, 5)  # answered while the backup is still being written: no wait on it
            self.backup_gate.go.set()
            return manual_result, await asyncio.wait_for(change, 5)

        manual, change = asyncio.run(run())
        self.assert_each_ok_is_what_boots(manual, change, "restore")

    def test_a_restore_by_hand_that_cancels_a_switch_does_not_cancel_a_newer_one(self):
        # made on the running version while a switch is scheduled: that switch goes.  A different switch scheduled
        # after the restore read which version boots next must not be cancelled in its name.
        self.updater.set_desired("2026.2.0", {"to": "2026.2.0", "mode": "keep", "backup": "manual.zip", "at": "2026-01-01T00:00:00"})

        async def run():
            read = self.pauses.add(lambda fn, args: fn is backupkit.boot_version, after=True)
            manual = asyncio.ensure_future(self.restore_by_hand({"ha": "backup"}, "running.zip"))
            await asyncio.wait_for(read.reached.wait(), 5)
            change = await asyncio.wait_for(self.version_change("keep"), 5)
            read.go.set()
            return await asyncio.wait_for(manual, 5), change

        manual, change = asyncio.run(run())
        self.assertTrue(change["ok"], change)
        state = self.updater._read()
        self.assertFalse(manual["ok"] and state.get("desired") != OLDER, f"the switch to {OLDER} answered ok and was cancelled as {manual.get('cancelled_switch')}")
        self.assertEqual(state.get("desired"), OLDER)
        self.assertFalse(self.installer.busy)

    def test_an_ordinary_restore_still_goes_through_and_releases_what_it_reserved(self):
        r = asyncio.run(self.restore_by_hand())
        self.assertTrue(r["ok"], r)
        self.assertEqual(backupkit._pending_meta(self.cfg)["name"], "manual.zip")
        self.assertFalse(self.installer.busy)
        self.assertFalse(self.views._HA_CHANGE_LOCK.locked())

    def test_a_restore_by_hand_is_refused_at_once_while_a_version_change_is_prepared(self):
        async def run():
            self.backup_gate = SimpleNamespace(reached=asyncio.Event(), go=asyncio.Event())
            change = asyncio.ensure_future(self.version_change("keep"))
            await asyncio.wait_for(self.backup_gate.reached.wait(), 5)
            manual = await asyncio.wait_for(self.restore_by_hand(), 5)
            self.backup_gate.go.set()
            return manual, await asyncio.wait_for(change, 5)

        manual, change = asyncio.run(run())
        self.assertFalse(manual["ok"], manual)
        self.assertIn("try again in a moment", manual["error"])
        self.assertTrue(change["ok"], change)
        self.assertFalse(backupkit.pending(self.cfg))


if __name__ == "__main__":
    unittest.main()
