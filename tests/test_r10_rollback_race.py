"""Review round 10: a full rollback and a Home Assistant version change or a restore by hand at the same time.

A full rollback schedules a restore too: the backup taken before the integration switch, then starts the previous
version.  It checked that no restore was scheduled, awaited the schedule and then started, without holding
_HA_CHANGE_LOCK or busy until start() took it; a version change prepares under both, and a restore by hand checks
and schedules under both.  Each test stops one request at a chosen executor job, runs the other, lets the first
finish, and then checks the pending archive, its for_version, ha.json's change, the rollback intent and what the
entrypoint would do at the next boot: every request that answered ok is what the boot does."""

import asyncio
import json
import os
import shutil
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


def _deploy(domain, tag):
    """What start() does between taking busy and releasing it (the pre-start backup, the deploy, pip)."""


async def _first(event, task):
    """Wait until ``event`` is set or ``task`` has finished (a request refused before it reached its pause)."""
    waiter = asyncio.ensure_future(event.wait())
    await asyncio.wait({waiter, task}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
    waiter.cancel()


class FullRollbackVersusScheduledChangesTest(unittest.TestCase):

    def setUp(self):
        from custom_components.integration_manager import backup_views, ha_updater, views
        from custom_components.integration_manager.installer import Installer, State

        self.views, self.backup_views = views, backup_views
        self.cfg = cfg = _volume()
        self.addCleanup(shutil.rmtree, cfg, ignore_errors=True)
        _zip(cfg, "pre.zip", {"ha_version": READABLE})  # taken before the switch from v1 to v2
        _zip(cfg, "manual.zip", {"ha_version": READABLE})
        _zip(cfg, "old.zip", {"ha_version": OLDER})
        for module in (views, backup_views):
            p = mock.patch.object(module, "HA_VERSION", RUNNING)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(views.events, "emit")
        p.start()
        self.addCleanup(p.stop)
        self.pauses = pauses = _Pauses()

        async def executor(fn, *args):
            await pauses.wait(fn, args, after=False)
            result = fn(*args)
            await pauses.wait(fn, args, after=True)
            return result

        self.hass = SimpleNamespace(config=SimpleNamespace(config_dir=cfg, path=lambda *p: os.path.join(cfg, *p)), async_add_executor_job=executor)
        self.updater = ha_updater.HaUpdater(self.hass)
        self.backup_gate = None
        inst = self.installer = Installer.__new__(Installer)
        inst.hass, inst.config_dir, inst.state_dir = self.hass, cfg, os.path.join(cfg, backupkit.STATE_DIR)
        inst.busy = False
        inst.settings = SimpleNamespace(backup_keep=50)
        inst.state = State(domain="demo", installed={"demo": {"versions": {"v1": {}, "v2": {}}, "running_tag": "v2",
                                                              "previous_tag": "v1", "pre_update_backup": "pre.zip"}})
        inst._save_state = lambda: None
        inst._cancel_smoke = lambda: None

        async def async_backup(label=""):
            rec = await executor(backupkit.create, cfg, label)
            if self.backup_gate is not None:  # the pre-change backup is the long part of a version change
                self.backup_gate.reached.set()
                await self.backup_gate.go.wait()
            return rec

        async def start(domain, tag, own_restore=None):
            # installer.start's gates: refused while busy or while a restore it does not own is scheduled; busy set
            # before its first await and released at its end
            if inst.busy:
                return {"ok": False, "error": "another action is running"}
            archive = backupkit.pending_archive(cfg)
            if archive is not None and os.path.basename(archive) != own_restore:
                return {"ok": False, "error": "a restore is scheduled for the next restart"}
            inst.busy = True
            try:
                await executor(_deploy, domain, tag)
                rec = inst.state.installed[domain]
                rec["previous_tag"], rec["running_tag"] = rec["running_tag"], tag
                return {"ok": True}
            finally:
                inst.busy = False

        inst.async_backup = async_backup
        inst.start = start

    # ----- the requests -----

    async def rollback(self):
        return await self.installer.rollback_full("demo")

    async def restore_by_hand(self):
        view = self.backup_views.BackupActionView(self.hass, self.installer, self.updater)
        view.json = lambda d: d
        return await self.backup_views.BackupActionView.post.__wrapped__(view, None, {}, "manual.zip", "restore")

    async def version_change(self, mode):
        try:
            if mode == "restore":  # a restore with its backup's version (BackupActionView, ha=backup)
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

    def assert_each_ok_is_what_boots(self, rollback, other, mode):
        """``mode``: the other request's (restore, rebuild: a version change; manual: a restore by hand)."""
        self.assertFalse(self.installer.busy)
        self.assertFalse(self.views._HA_CHANGE_LOCK.locked())
        self.assertIsNone(self.installer.state.pending_rollback, "the rollback finished: nothing is left for a boot to finish")
        self.assertFalse(rollback["ok"] and other["ok"], f"both answered ok, one plan replaced the other: {rollback} / {other}")
        self.assertTrue(rollback["ok"] or other["ok"], f"neither went through: {rollback} / {other}")
        rec = self.installer.state.installed["demo"]
        self.assertEqual(rec["running_tag"], "v1" if rollback["ok"] else "v2")
        scheduled, booted, state = self.boot()
        last = state.get("last_restore") or {}
        if rollback["ok"]:
            self.assertEqual((scheduled["pending"] or {}).get("name"), "pre.zip")
            self.assertIsNone(scheduled["pending"].get("for_version"))
            self.assertIsNone(scheduled["change"])
            self.assertEqual(booted, RUNNING)
            self.assertEqual((last.get("backup"), last.get("ok"), last.get("parts")), ("pre.zip", True, ["storage", "custom_components"]), state)
        elif mode == "manual":
            self.assertEqual((scheduled["pending"] or {}).get("name"), "manual.zip")
            self.assertIsNone(scheduled["change"])
            self.assertEqual(booted, RUNNING)
            self.assertEqual((last.get("backup"), last.get("ok")), ("manual.zip", True), state)
        else:
            self.assertEqual((scheduled["change"] or {}).get("to"), OLDER)
            self.assertEqual(scheduled["change"].get("mode"), mode)
            self.assertEqual(booted, OLDER, state.get("last_error"))
            self.assertEqual(state.get("desired"), OLDER)
            self.assertTrue(state["change"].get("applied"), state)
            if mode == "restore":
                self.assertEqual(scheduled["pending"].get("name"), "old.zip")
                self.assertEqual(scheduled["pending"].get("for_version"), OLDER)
                self.assertEqual(last.get("backup"), "old.zip")
            else:
                self.assertIsNone(scheduled["pending"])
                self.assertFalse(state.get("last_error"), state)

    def _rollback_commit(self):
        return self.pauses.add(lambda fn, args: fn is backupkit.schedule_restore and args[1] == "pre.zip")

    # ----- (a), (b): a version change with a restore or a clean start -----

    def _change_while_the_rollback_is_about_to_commit(self, mode):
        async def run():
            commit = self._rollback_commit()
            rollback = asyncio.ensure_future(self.rollback())
            await asyncio.wait_for(commit.reached.wait(), 5)  # its checks made and its intent recorded, its schedule not written
            change = await asyncio.wait_for(self.version_change(mode), 5)
            commit.go.set()
            return await asyncio.wait_for(rollback, 5), change

        rollback, change = asyncio.run(run())
        self.assert_each_ok_is_what_boots(rollback, change, mode)

    def test_a_restore_with_its_version_prepared_before_the_rollback_commits(self):
        self._change_while_the_rollback_is_about_to_commit("restore")

    def test_a_clean_start_prepared_before_the_rollback_commits(self):
        self._change_while_the_rollback_is_about_to_commit("rebuild")

    def _rollback_commits_while_the_change_is_recorded(self, mode):
        # the change has written its restore or clean start and is about to record itself in ha.json (busy and the lock held)
        async def run():
            commit = self._rollback_commit()
            recorded = self.pauses.add(lambda fn, args: fn == self.updater.set_desired)
            rollback = asyncio.ensure_future(self.rollback())
            await asyncio.wait_for(commit.reached.wait(), 5)
            change = asyncio.ensure_future(self.version_change(mode))
            await _first(recorded.reached, change)
            commit.go.set()
            rollback_result = await asyncio.wait_for(rollback, 5)
            recorded.go.set()
            return rollback_result, await asyncio.wait_for(change, 5)

        rollback, change = asyncio.run(run())
        self.assert_each_ok_is_what_boots(rollback, change, mode)

    def test_a_rollback_committed_while_a_restore_with_its_version_is_recorded(self):
        self._rollback_commits_while_the_change_is_recorded("restore")

    def test_a_rollback_committed_while_a_clean_start_is_recorded(self):
        self._rollback_commits_while_the_change_is_recorded("rebuild")

    def test_a_rollback_after_a_clean_start_was_scheduled(self):
        # no race: a clean start schedules no archive, and the rollback's restore took its place at the boot
        async def run():
            change = await self.version_change("rebuild")
            return await self.rollback(), change

        rollback, change = asyncio.run(run())
        self.assertTrue(change["ok"], change)
        self.assert_each_ok_is_what_boots(rollback, change, "rebuild")

    def test_a_rollback_is_refused_while_a_version_change_is_prepared(self):
        async def run():
            self.backup_gate = SimpleNamespace(reached=asyncio.Event(), go=asyncio.Event())
            change = asyncio.ensure_future(self.version_change("restore"))
            await asyncio.wait_for(self.backup_gate.reached.wait(), 5)
            rollback = await asyncio.wait_for(self.rollback(), 5)  # answered while the backup is still written: no wait on it
            self.backup_gate.go.set()
            return rollback, await asyncio.wait_for(change, 5)

        rollback, change = asyncio.run(run())
        self.assertFalse(rollback["ok"], rollback)
        self.assertTrue(change["ok"], change)
        self.assert_each_ok_is_what_boots(rollback, change, "restore")

    # ----- (c): a restore by hand -----

    def test_a_restore_by_hand_before_the_rollback_commits(self):
        async def run():
            commit = self._rollback_commit()
            rollback = asyncio.ensure_future(self.rollback())
            await asyncio.wait_for(commit.reached.wait(), 5)
            manual = await asyncio.wait_for(self.restore_by_hand(), 5)
            commit.go.set()
            return await asyncio.wait_for(rollback, 5), manual

        rollback, manual = asyncio.run(run())
        self.assert_each_ok_is_what_boots(rollback, manual, "manual")

    def _restore_by_hand_reserves_busy(self):
        """The restore by hand checks and schedules with busy reserved (ac6febf); before that it held nothing, and
        a rollback run while it was about to commit was replaced by it, whatever the rollback held.  Probed, not
        read off the source: what the next test asserts is that the commit really is covered."""
        async def probe():
            commit = self.pauses.add(lambda fn, args: fn is backupkit.schedule_restore and args[1] == "manual.zip")
            manual = asyncio.ensure_future(self.restore_by_hand())
            await asyncio.wait_for(commit.reached.wait(), 5)
            held = self.installer.busy
            commit.go.set()
            await asyncio.wait_for(manual, 5)
            return held

        held = asyncio.run(probe())
        backupkit.cancel_restore(self.cfg)
        self.pauses.points.clear()
        return held

    def test_a_rollback_while_a_restore_by_hand_is_about_to_commit(self):
        # asserted, not skipped over: the reservation is what makes the rest of this test meaningful, so losing it
        # must fail here rather than quietly take the test out of the run
        self.assertTrue(self._restore_by_hand_reserves_busy(),
                        "the restore by hand commits without reserving busy: a full rollback started while it is "
                        "about to write its schedule replaces the restore that already answered ok")

        async def run():
            commit = self.pauses.add(lambda fn, args: fn is backupkit.schedule_restore and args[1] == "manual.zip")
            manual = asyncio.ensure_future(self.restore_by_hand())
            await asyncio.wait_for(commit.reached.wait(), 5)
            rollback = await asyncio.wait_for(self.rollback(), 5)
            commit.go.set()
            return rollback, await asyncio.wait_for(manual, 5)

        rollback, manual = asyncio.run(run())
        self.assert_each_ok_is_what_boots(rollback, manual, "manual")

    # ----- the rollback's own start -----

    def test_nothing_is_scheduled_while_the_rollbacks_start_runs(self):
        async def run():
            deploy = self.pauses.add(lambda fn, args: fn is _deploy)
            rollback = asyncio.ensure_future(self.rollback())
            await asyncio.wait_for(deploy.reached.wait(), 5)
            refused = [await self.version_change(mode) for mode in ("keep", "restore", "rebuild")] + [await self.restore_by_hand()]
            deploy.go.set()
            return await asyncio.wait_for(rollback, 5), refused

        rollback, refused = asyncio.run(run())
        self.assertTrue(rollback["ok"], rollback)
        self.assertEqual([r["ok"] for r in refused], [False] * 4, refused)
        self.assert_each_ok_is_what_boots(rollback, {"ok": False}, "manual")

    def test_a_rollback_alone_goes_through_and_releases_what_it_reserved(self):
        r = asyncio.run(self.rollback())
        self.assertTrue(r["ok"], r)
        self.assert_each_ok_is_what_boots(r, {"ok": False}, "manual")

    def test_a_rollback_whose_start_fails_releases_what_it_reserved(self):
        async def start(domain, tag, own_restore=None):
            self.assertFalse(self.installer.busy, "start() refuses while busy: the rollback hands it over")
            return {"ok": False, "error": "pip failed"}

        self.installer.start = start
        r = asyncio.run(self.rollback())
        self.assertFalse(r["ok"])
        self.assertFalse(self.installer.busy)
        self.assertFalse(self.views._HA_CHANGE_LOCK.locked())
        self.assertIsNone(self.installer.state.pending_rollback)
        self.assertFalse(backupkit.pending(self.cfg))


class AutomaticRollbackTest(unittest.TestCase):
    """The smoke test's rollback runs on its own: never waiting on the lock, never refused for good by it."""

    def installer(self):
        from custom_components.integration_manager.installer import Installer, State

        inst = object.__new__(Installer)
        inst.state = State(domain="demo", installed={"demo": {"versions": {"1.0": {}, "2.0": {}}, "running_tag": "2.0"}},
                           pending_smoke={"domain": "demo", "tag": "2.0", "can_rollback": True})
        inst.hass = SimpleNamespace(loop=mock.Mock(), async_create_task=mock.Mock(), is_running=True)
        inst.settings = SimpleNamespace(int_=lambda key, lo, hi: 300, bool_=lambda key: True)
        inst.busy = False
        inst._smoke_handle, inst._smoke_pending, inst._smoke_waiting, inst._smoke_rechecked = None, {"domain": "demo"}, {}, set()
        inst._entries_of = lambda dom: [SimpleNamespace(state=SimpleNamespace(value="setup_error"), disabled_by=None, title="Hub")]
        inst.health_source = lambda grace: {"state": "error", "reason": "not loaded"}
        inst._save_state = lambda: None
        inst.rollback_full = mock.AsyncMock(return_value={"ok": True, "tag": "1.0", "restore": "b.zip"})
        inst.restart = mock.AsyncMock()
        inst.announce_smoke = mock.Mock()
        return inst

    def test_the_verdict_waits_while_the_version_change_lock_is_held(self):
        from custom_components.integration_manager import views

        inst = self.installer()

        async def held():
            async with views._HA_CHANGE_LOCK:  # a scheduled switch being cancelled holds it without busy
                await inst._smoke_check("demo", "2.0", True)

        with mock.patch.object(views.events, "emit"):
            asyncio.run(held())
            inst.rollback_full.assert_not_awaited()
            self.assertEqual(inst.hass.loop.call_later.call_args.args[0], 60)
            self.assertIsNotNone(inst.state.pending_smoke)
            asyncio.run(inst._smoke_check("demo", "2.0", True))
        inst.rollback_full.assert_awaited_once()
        inst.restart.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
