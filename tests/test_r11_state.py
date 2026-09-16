"""Review round 11 (state): the timeline's writing thread, Cancel restore against a version change or a full
rollback being prepared, the end of a full rollback's backup protection, the flow hook's restart flag, and the
version-change lock taken without waiting."""

import asyncio
import json
import os
import shutil
import tarfile
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import backupkit
from tests.fakes import entrypoint_for
from tests.test_review_backup import _volume, _zip

RUNNING = "2026.8.3"
OLDER = "2026.1.0"


class TimelineWriterTest(unittest.TestCase):
    """N3: a message utf-8 cannot encode (a lone surrogate) ended the writing thread; every later event added on
    the loop was queued for a thread that was gone, and every read waited its full READ_DRAIN_S."""

    def setUp(self):
        from custom_components.integration_manager import events

        self.events = events
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = events.Events(os.path.join(self.dir, "events.jsonl"))
        self.assertTrue(events.drain(5), "an earlier test left events that are never written")

    def add_on_the_loop(self, *messages):
        async def run():
            for m in messages:
                self.store.add("error", m)

        asyncio.run(run())

    def test_a_message_with_a_lone_surrogate_does_not_stop_later_events(self):
        bad = os.fsdecode(b"/config/\xff.yaml")  # how Python decodes a file name that is not utf-8
        self.add_on_the_loop(f"[Errno 13] Permission denied: '{bad}'", "the next one")
        t0 = time.monotonic()
        self.assertTrue(self.events.drain(3))
        self.assertLess(time.monotonic() - t0, 1)
        self.assertEqual([r["message"] for r in self.store.recent()], ["[Errno 13] Permission denied: '/config/?.yaml'", "the next one"])
        self.assertTrue(self.events._THREAD.is_alive())

    def test_off_the_loop_it_is_recorded_too(self):
        self.store.add("error", "bad \udcff name")  # no running loop: written by the caller
        self.assertEqual([r["message"] for r in self.store.recent()], ["bad ? name"])

    def test_an_unexpected_error_writing_one_event_does_not_end_the_thread(self):
        real = self.store._append
        calls = []

        def append(line):
            calls.append(line)
            if len(calls) == 1:
                raise RuntimeError("boom")
            real(line)

        with mock.patch.object(self.store, "_append", append), self.assertLogs(self.events._LOGGER, "ERROR"):
            self.add_on_the_loop("lost", "kept")
            self.assertTrue(self.events.drain(3))
        self.assertEqual([r["message"] for r in self.store.recent()], ["kept"])

    def test_a_thread_that_ended_anyway_is_replaced(self):
        real = self.store._append

        def append(line):
            if "ends the thread" in line:
                raise SystemExit  # not an Exception: what the guard does not catch ends the thread
            real(line)

        with mock.patch.object(self.store, "_append", append):
            self.add_on_the_loop("ends the thread")
            self.assertTrue(self.events.drain(3))
            ended = self.events._THREAD
            ended.join(3)
            self.assertFalse(ended.is_alive())
            self.add_on_the_loop("after the thread ended")
            t0 = time.monotonic()
            self.assertTrue(self.events.drain(3))
            self.assertLess(time.monotonic() - t0, 1)
        self.assertIsNot(self.events._THREAD, ended)
        self.assertEqual([r["message"] for r in self.store.recent()], ["after the thread ended"])


from tests import test_r10_restore_race as restore_race  # noqa: E402
from tests import test_r10_rollback_race as rollback_race  # noqa: E402


class CancelRestoreVersusVersionChangeTest(unittest.TestCase):
    """N4: a version change schedules its restore first and writes ha.json's change last; Cancel restore checked
    neither the version-change lock nor busy, so a cancel in between took that restore for one made by hand and the
    switch was dropped at the boot as a restore that did not happen."""

    setUp = restore_race.RestoreByHandVersusVersionChangeTest.setUp
    version_change = restore_race.RestoreByHandVersusVersionChangeTest.version_change
    boot = restore_race.RestoreByHandVersusVersionChangeTest.boot
    assert_each_ok_is_what_boots = restore_race.RestoreByHandVersusVersionChangeTest.assert_each_ok_is_what_boots

    def installer_state(self, rollback_backup=None):
        self.installer.state = SimpleNamespace(rollback_backup=rollback_backup, rollback_at="2026-09-16T10:00:00" if rollback_backup else None)
        self.installer.saves = 0

        def save():
            self.installer.saves += 1

        self.installer._save_state = save

    async def cancel(self):
        view = object.__new__(self.backup_views.RestoreCancelView)  # built as on origin/main too, which took no installer
        view.hass, view.installer, view.json = self.hass, self.installer, lambda d: d
        return await self.backup_views.RestoreCancelView.post.__wrapped__(view, None, {})

    def test_a_cancel_between_the_changes_restore_and_its_record_is_refused(self):
        from custom_components.integration_manager.ha_updater import HaUpdater

        self.installer_state()

        async def run():
            record = self.pauses.add(lambda fn, args: getattr(fn, "__func__", None) is HaUpdater.set_desired)
            change = asyncio.ensure_future(self.version_change("restore"))
            await asyncio.wait_for(record.reached.wait(), 5)  # old.zip scheduled for OLDER, ha.json's change not written yet
            self.assertEqual(backupkit._pending_meta(self.cfg)["for_version"], OLDER)
            cancel = await asyncio.wait_for(self.cancel(), 5)
            record.go.set()
            return cancel, await asyncio.wait_for(change, 5)

        cancel, change = asyncio.run(run())
        self.assertFalse(cancel["ok"], cancel)
        self.assertIn("try again in a moment", cancel["error"])
        self.assertTrue(change["ok"], change)
        self.assert_each_ok_is_what_boots({"ok": False}, change, "restore")  # the switch and its restore boot

    def test_a_cancel_is_refused_while_busy(self):
        self.installer_state()
        backupkit.schedule_restore(self.cfg, "manual.zip")
        self.installer.busy = True  # a full rollback about to schedule, an install
        r = asyncio.run(self.cancel())
        self.assertFalse(r["ok"], r)
        self.assertTrue(backupkit.pending(self.cfg))
        self.assertTrue(self.installer.busy)

    def test_a_restore_by_hand_is_still_cancelled_and_nothing_stays_reserved(self):
        self.installer_state()
        backupkit.schedule_restore(self.cfg, "manual.zip")
        self.assertEqual(asyncio.run(self.cancel()), {"ok": True, "cancelled": True})
        self.assertFalse(backupkit.pending(self.cfg))
        self.assertFalse(self.installer.busy)
        self.assertFalse(self.views._HA_CHANGE_LOCK.locked())
        self.assertEqual(self.installer.saves, 0)

    def test_cancelling_a_full_rollbacks_restore_ends_its_backups_protection(self):
        # N5: nothing else would ever release it, the restore that did is gone
        self.installer_state("running.zip")
        backupkit.schedule_restore(self.cfg, "running.zip", ["storage", "custom_components"], None, True)  # what _rollback_full schedules
        self.assertEqual(asyncio.run(self.cancel()), {"ok": True, "cancelled": True})
        self.assertEqual((self.installer.state.rollback_backup, self.installer.state.rollback_at), (None, None))
        self.assertEqual(self.installer.saves, 1)

    def test_cancelling_another_restore_keeps_a_rollback_backups_protection(self):
        self.installer_state("running.zip")
        backupkit.schedule_restore(self.cfg, "manual.zip")
        self.assertEqual(asyncio.run(self.cancel()), {"ok": True, "cancelled": True})
        self.assertEqual(self.installer.state.rollback_backup, "running.zip")


class RollbackBackupReleasedWhenItsRestoreDidNotHappenTest(unittest.TestCase):
    """N5: the protection ended only with a successful restore of that backup after the rollback; a restore dropped
    by the entrypoint, cancelled or failed never came, and the backup stayed in protected_backups() for good."""

    def installer(self, **state):
        from custom_components.integration_manager.installer import Installer, State

        inst = object.__new__(Installer)
        inst.config_dir = _volume()
        self.addCleanup(shutil.rmtree, inst.config_dir, True)
        inst.state_dir = os.path.join(inst.config_dir, backupkit.STATE_DIR)
        inst.state = State(rollback_backup="pre.zip", rollback_at="2026-09-16T10:00:00", **state)
        inst._save_state = lambda: None
        _zip(inst.config_dir, "pre.zip", {"ha_version": RUNNING})
        return inst

    def protected(self, inst):
        from custom_components.integration_manager.installer import Installer

        return "pre.zip" in Installer.protected_backups(inst)

    def test_a_restore_the_entrypoint_dropped_releases_it(self):
        inst = self.installer()  # the schedule is gone and ha.json records no outcome of it
        for last in (None, {"ok": True, "backup": "other.zip", "at": "2026-09-01T08:00:00"},
                     {"ok": True, "backup": "pre.zip", "at": "2026-09-10T08:00:00"}):  # this backup, restored before the rollback
            with self.subTest(last=last):
                inst.state.rollback_backup, inst.state.rollback_at = "pre.zip", "2026-09-16T10:00:00"
                self.assertTrue(inst.release_rollback_backup(last))
                self.assertEqual((inst.state.rollback_backup, inst.state.rollback_at), (None, None))
                self.assertFalse(self.protected(inst))

    def test_a_failed_restore_releases_it(self):
        inst = self.installer()
        self.assertTrue(inst.release_rollback_backup({"ok": False, "backup": "pre.zip", "at": "2026-09-16T10:00:05"}))
        self.assertIsNone(inst.state.rollback_backup)

    def test_an_older_outcome_releases_nothing_while_its_restore_is_still_scheduled(self):
        inst = self.installer()
        backupkit.schedule_restore(inst.config_dir, "pre.zip", ["storage", "custom_components"], None, True)
        for last in ({"ok": True, "backup": "pre.zip", "at": "2026-09-10T08:00:00"},  # the case the time was recorded for
                     {"ok": False, "backup": "pre.zip", "at": "2026-09-16T11:00:00"}):  # a failure kept for a retry
            with self.subTest(last=last):
                self.assertFalse(inst.release_rollback_backup(last))
                self.assertTrue(self.protected(inst))

    def test_an_interrupted_rollback_whose_restore_did_not_happen_releases_it(self):
        inst = self.installer(domain="demo", installed={"demo": {"versions": {"v1": {}, "v2": {}}, "running_tag": "v2"}},
                              pending_rollback={"domain": "demo", "tag": "v1", "backup": "pre.zip", "at": "2026-09-16T10:00:00"})
        with mock.patch("custom_components.integration_manager.installer.events.emit"):
            inst._apply_pending_rollback()
        self.assertEqual((inst.state.rollback_backup, inst.state.rollback_at), (None, None))
        self.assertFalse(self.protected(inst))
        self.assertEqual(inst.state.installed["demo"]["running_tag"], "v2")


class ForeignEntryNotUnloadedTest(unittest.IsolatedAsyncioTestCase):
    """N11: the flow hook said "restart the process" without setting restart_required, and took an unload that
    returned False (disabled, still running) for a stopped entry, unlike Installer._disable_entries."""

    def installer(self):
        from custom_components.integration_manager.installer import Installer, State

        inst = object.__new__(Installer)
        inst.state = State(domain="demo", installed={"demo": {"versions": {"1.0": {}}, "running_tag": "1.0"}})
        inst.saves = []
        inst._save_state = lambda: inst.saves.append(inst.state.restart_required)
        return inst

    async def hook(self, inst, entry, suspend):
        import custom_components.integration_manager as im

        with mock.patch.object(inst, "async_suspend_entry", suspend, create=True):
            return await im.async_disable_foreign_entry(inst, {"handler": "other", "result": entry})

    async def test_an_unload_that_returned_false_requires_a_restart(self):
        inst = self.installer()
        entry = SimpleNamespace(entry_id="e1", disabled_by=None, state=SimpleNamespace(value="failed_unload"))

        async def suspend(e):
            e.disabled_by = "user"
            return False

        note = await self.hook(inst, entry, suspend)
        self.assertIn("did not unload", note)
        self.assertIn("restart the process", note)
        self.assertTrue(inst.state.restart_required)
        self.assertEqual(inst.saves, [True])

    async def test_an_unload_home_assistant_refused_requires_a_restart(self):
        from homeassistant.config_entries import OperationNotAllowed

        inst = self.installer()
        entry = SimpleNamespace(entry_id="e1", disabled_by=None, state=SimpleNamespace(value="migration_error"))

        async def suspend(e):
            e.disabled_by = "user"
            raise OperationNotAllowed("migration_error")

        note = await self.hook(inst, entry, suspend)
        self.assertIn("restart the process", note)
        self.assertTrue(inst.state.restart_required)
        self.assertEqual(inst.saves, [True])

    async def test_an_entry_that_unloaded_needs_no_restart(self):
        inst = self.installer()
        entry = SimpleNamespace(entry_id="e1", disabled_by=None, state=SimpleNamespace(value="not_loaded"))

        async def suspend(e):
            e.disabled_by = "user"
            return True

        note = await self.hook(inst, entry, suspend)
        self.assertIn("entry created DISABLED: this container runs demo", note)
        self.assertFalse(inst.state.restart_required)
        self.assertEqual(inst.saves, [])


class VersionChangeLockNeverWaitedForTest(unittest.TestCase):
    """Every holder of _HA_CHANGE_LOCK checked locked() and then entered ``async with``: a lock just released to a
    caller waiting for it says unlocked, and asyncio's fair lock queues the next one behind that caller, so a
    request waited instead of getting its "try again".  Each request here meets the lock in that state."""

    setUp = rollback_race.FullRollbackVersusScheduledChangesTest.setUp
    rollback = rollback_race.FullRollbackVersusScheduledChangesTest.rollback
    restore_by_hand = rollback_race.FullRollbackVersusScheduledChangesTest.restore_by_hand
    version_change = rollback_race.FullRollbackVersusScheduledChangesTest.version_change

    def _fresh_lock(self):
        p = mock.patch.object(self.views, "_HA_CHANGE_LOCK", asyncio.Lock())  # bound to this test's loop only
        p.start()
        self.addCleanup(p.stop)

    async def cancel_switch(self):
        self.updater.set_desired(OLDER)

        async def body():
            return {"version": RUNNING}

        view = self.views.HaActionView(self.updater, self.installer)
        view.json = lambda d: d
        return await view.post(SimpleNamespace(content_type="application/json", json=body), "update")

    async def builder_keeps_running_version(self):
        from custom_components.integration_manager import build_views

        view = object.__new__(build_views.BuildPrepareView)
        view.hass, view.publisher = self.hass, None
        view.installer = SimpleNamespace(hass=self.hass, busy=False, install=mock.AsyncMock(return_value={"ok": True}))
        running = build_views.HA_VERSION
        view.updater = SimpleNamespace(status=mock.AsyncMock(return_value={"current": running, "pending": True, "desired": OLDER}),
                                       cancel_config_change=lambda: [], set_desired=lambda v: {})
        view._check = SimpleNamespace(_resolve=mock.AsyncMock(return_value=("demo", "v1", running)), checked=lambda *a: True)
        view.json = lambda d: d
        with mock.patch.object(build_views, "_commit_of", mock.AsyncMock(return_value="0" * 40)), mock.patch.object(build_views.events, "emit"):
            return await build_views.BuildPrepareView.post.__wrapped__(view, None, {"domain": "demo", "ref": "v1", "ha": running})

    def assert_refused_without_waiting(self, request):
        self._fresh_lock()
        lock = self.views._HA_CHANGE_LOCK

        async def run():
            await lock.acquire()
            go = asyncio.Event()

            async def waiting():
                async with lock:
                    await go.wait()

            other = asyncio.ensure_future(waiting())
            await asyncio.sleep(0)  # queued on the lock
            lock.release()  # to that caller, which has not resumed yet
            self.assertFalse(lock.locked())
            try:
                async with asyncio.timeout(2):
                    return await request()
            finally:
                go.set()
                await other

        try:
            result = asyncio.run(run())
        except TimeoutError:
            self.fail("waited for the lock instead of refusing")
        if isinstance(result, dict) and "ok" in result:
            self.assertFalse(result["ok"], result)
            self.assertIn("try again", result["error"])
        self.assertFalse(self.installer.busy)

    def test_a_version_change(self):
        self.assert_refused_without_waiting(lambda: self.version_change("keep"))

    def test_cancelling_a_switch(self):
        self.assert_refused_without_waiting(self.cancel_switch)

    def test_the_environment_builder_keeping_the_running_version(self):
        self.assert_refused_without_waiting(self.builder_keeps_running_version)

    def test_a_restore_by_hand(self):
        self.assert_refused_without_waiting(self.restore_by_hand)

    def test_a_full_rollback(self):
        self.assert_refused_without_waiting(self.rollback)

    def test_cancel_restore(self):
        async def cancel():
            view = object.__new__(self.backup_views.RestoreCancelView)
            view.hass, view.installer, view.json = self.hass, self.installer, lambda d: d
            return await self.backup_views.RestoreCancelView.post.__wrapped__(view, None, {})

        self.assert_refused_without_waiting(cancel)


class FullRollbackChecksItsBackupOffTheLoopTest(unittest.TestCase):
    """_rollback_full looked for the backup file with os.path.isfile on the event loop."""

    setUp = rollback_race.FullRollbackVersusScheduledChangesTest.setUp
    rollback = rollback_race.FullRollbackVersusScheduledChangesTest.rollback

    def run_rollback(self):
        real = self.hass.async_add_executor_job
        job = []  # the executor job running now
        looked_up = []  # (in the executor, as its own job) for each look for the backup file

        async def executor(fn, *args):
            job.append((fn, args))
            try:
                return await real(fn, *args)
            finally:
                job.pop()

        real_isfile = os.path.isfile

        def isfile(path):
            if os.path.basename(str(path)) == "pre.zip":
                looked_up.append((bool(job), bool(job) and job[-1] == (isfile, (path,))))
            return real_isfile(path)

        self.hass.async_add_executor_job = executor
        with mock.patch("custom_components.integration_manager.installer.os.path.isfile", isfile):
            result = asyncio.run(self.rollback())
        return result, looked_up

    def test_the_backup_is_looked_for_in_the_executor(self):
        result, looked_up = self.run_rollback()
        self.assertTrue(result["ok"], result)
        self.assertIn((True, True), looked_up)
        self.assertEqual([x for x in looked_up if not x[0]], [], "looked for on the event loop")

    def test_a_missing_backup_is_still_refused_and_releases_what_it_took(self):
        os.remove(os.path.join(self.cfg, backupkit.BACKUP_DIR, "pre.zip"))
        result, _ = self.run_rollback()
        self.assertFalse(result["ok"])
        self.assertIn("no longer exists", result["error"])
        self.assertFalse(self.installer.busy)
        self.assertFalse(self.views._HA_CHANGE_LOCK.locked())
        self.assertFalse(backupkit.pending(self.cfg))


class DroppedCleanStartSetAsideCopyTest(unittest.TestCase):
    """The clean-start orphan fix (_drop_set_aside, storage_restored): what test_r9_boot's F15 tests leave out.  A
    restore of .storage that failed and was put back did not replace the copy's configuration; a switch killed after
    its rename, followed by a restore that left .storage alone, gets its .storage back from the copy."""

    ASIDE = ".storage.pre-rebuild-20260101-000000"

    def setUp(self):
        self.cfg = _volume()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        self.ep = entrypoint_for(self, self.cfg)
        with open(os.path.join(self.cfg, "custom_components", "x", "manifest.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
        self.pre = backupkit.create(self.cfg, "pre-change", storage_version="2026.9.2")["name"]
        self.aside = os.path.join(self.cfg, self.ASIDE)
        os.makedirs(self.aside)
        with open(os.path.join(self.aside, "auth"), "w", encoding="utf-8") as fh:
            fh.write("refresh tokens")
        self.logged = []

    def boot(self, stage, parts):
        with open(self.ep.REBUILD_FILE, "w", encoding="utf-8") as fh:
            json.dump({"stage": stage, "to": "2026.8.3", "backup": self.pre, "boot_backup": "boot.zip", "aside": self.ASIDE}, fh)
        backupkit.schedule_restore(self.cfg, self.pre, parts, for_version="2026.9.2", force=True)
        state = {}
        with mock.patch.object(self.ep, "log", self.logged.append):
            self.ep.apply_config_changes(state, "2026.9.2", "2026.8.3")
        self.assertFalse(os.path.isfile(self.ep.REBUILD_FILE))
        return state["last_restore"]

    def test_a_failed_restore_of_storage_keeps_the_copy(self):
        with mock.patch.object(backupkit, "_extract_to", side_effect=OSError(28, "No space left on device")):
            last = self.boot("import", ["storage"])
        self.assertFalse(last["ok"], last)
        self.assertTrue(os.path.isfile(os.path.join(self.aside, "auth")))
        self.assertTrue(any(self.ASIDE in line and "is kept" in line for line in self.logged), self.logged)

    def test_a_killed_switch_gets_its_storage_back_when_the_restore_left_storage_alone(self):
        shutil.rmtree(os.path.join(self.cfg, ".storage"))
        os.makedirs(os.path.join(self.cfg, ".storage"))  # what the boot killed after the rename left
        last = self.boot("renaming", ["custom_components"])
        self.assertTrue(last["ok"], last)
        self.assertFalse(os.path.isdir(self.aside))
        with open(os.path.join(self.cfg, ".storage", "auth"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "refresh tokens")
        self.assertFalse([line for line in self.logged if "putting .storage back failed" in line], self.logged)


class EncryptedImportFirstHeaderBoundedTest(unittest.TestCase):
    """N6: SecureTarFile parses the first tar header while it opens the archive, before _open_inner could install
    the bounded header class, so the 1 MB extended-header bound did not apply to an encrypted backup's first member."""

    def setUp(self):
        from custom_components.integration_manager import ha_import

        self.ha_import = ha_import
        self.cfg = _volume()
        self.addCleanup(shutil.rmtree, self.cfg, True)

    def test_an_oversized_first_header_is_refused_before_its_data_is_read(self):
        from tests.test_r9_boot import _ha_backup

        _ha_backup(self.cfg, [], protected=True, first=[("././@LongLink", 8 * 1024**2, tarfile.GNUTYPE_LONGNAME, None)])
        read = []
        real = tarfile.TarInfo._proc_gnulong

        def proc_gnulong(info, tar):
            read.append(info.size)  # tarfile reads the whole header data into memory here
            return real(info, tar)

        with mock.patch.object(tarfile.TarInfo, "_proc_gnulong", proc_gnulong), self.assertRaises(ValueError) as ctx:
            self.ha_import.inspect_backup(self.cfg, "key", {"demo"})
        self.assertIn("extended tar header", str(ctx.exception))
        self.assertEqual(read, [])

    def test_an_encrypted_backup_is_still_imported_and_a_wrong_key_still_said(self):
        from tests.test_r9_boot import _ha_backup

        _ha_backup(self.cfg, [("data/configuration.yaml", 100, tarfile.REGTYPE, None)], protected=True)
        summary = self.ha_import.inspect_backup(self.cfg, "key", {"demo"})
        self.assertEqual(summary["domains"]["demo"]["entries"][0]["entry_id"], "abc")
        _ha_backup(self.cfg, [("data/configuration.yaml", 100, tarfile.REGTYPE, None)], protected=True)
        with self.assertRaises(ValueError) as ctx:
            self.ha_import.inspect_backup(self.cfg, "not the key", {"demo"})
        self.assertIn("wrong encryption key", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
