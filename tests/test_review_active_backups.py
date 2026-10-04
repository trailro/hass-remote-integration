"""Recovery copies stay protected across awaits and reservations have one owner."""

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import backupkit
from custom_components.integration_manager import backup_views, installer as inst_mod
from custom_components.integration_manager.installer import Installer, State


class ActiveBackupTest(unittest.TestCase):
    def fixture(self):
        tmp = tempfile.TemporaryDirectory(prefix="hri-active-backup-")
        self.addCleanup(tmp.cleanup)
        async def executor(fn, *args):
            return fn(*args)
        hass = SimpleNamespace(config=SimpleNamespace(config_dir=tmp.name, components=set()),
                               async_add_executor_job=executor)
        inst = Installer(hass)
        inst.state = State(domain="demo", installed={"demo": {"running_tag": "v1", "versions": {"v1": {}, "v2": {}}}})
        for tag in ("v1", "v2"):
            os.makedirs(inst._version_dir("demo", tag))
            with open(os.path.join(inst._version_dir("demo", tag), "manifest.json"), "w", encoding="utf-8") as fh:
                json.dump({"domain": "demo", "version": tag}, fh)
        os.makedirs(os.path.join(tmp.name, backupkit.BACKUP_DIR))
        path = os.path.join(tmp.name, backupkit.BACKUP_DIR, "pre.zip")
        with open(path, "wb") as fh:
            fh.write(b"recovery")
        inst.async_backup = mock.AsyncMock(return_value={"name": "pre.zip"})
        inst._requirements_for = mock.AsyncMock(return_value=[])
        inst._install_requirements = mock.Mock(return_value=[])
        inst._apply_patches = mock.Mock(return_value="")
        inst._loadable = mock.AsyncMock(return_value=True)
        inst._enable_entries = mock.AsyncMock(return_value=[])
        inst._schedule_smoke = mock.Mock()
        inst._save_state = mock.Mock()
        view = backup_views.BackupActionView(hass, inst)
        view.json = lambda value: value
        return inst, view, path

    async def delete(self, view):
        return await backup_views.BackupActionView.post.__wrapped__(view, None, {}, "pre.zip", "delete")

    def test_pip_window_protects_copy_and_success_transfers_to_record(self):
        inst, view, path = self.fixture()
        async def check():
            entered, release = asyncio.Event(), asyncio.Event()
            async def executor(fn, *args):
                if fn is inst._install_requirements:
                    entered.set()
                    await release.wait()
                return fn(*args)
            inst.hass.async_add_executor_job = executor
            task = asyncio.create_task(inst.start("demo", "v2"))
            await asyncio.wait_for(entered.wait(), 5)
            self.assertIn("pre.zip", inst.protected_backups())
            self.assertFalse((await self.delete(view))["ok"])
            self.assertTrue(inst.busy)  # refusal did not release start's reservation
            self.assertTrue(os.path.isfile(path))
            release.set()
            self.assertTrue((await task)["ok"])
            self.assertEqual(inst._active_backups, set())
            self.assertIn("pre.zip", inst.protected_backups())  # now owned by rollback record
        with mock.patch.object(backupkit, "prune"), mock.patch.object(inst_mod.events, "emit"):
            asyncio.run(check())

    def test_cancellation_releases_only_this_starts_reservation(self):
        inst, _, _ = self.fixture()
        inst._active_backups.add("other.zip")
        async def check():
            entered = asyncio.Event()
            async def executor(fn, *args):
                if fn is inst._install_requirements:
                    entered.set()
                    await asyncio.Future()
                return fn(*args)
            inst.hass.async_add_executor_job = executor
            task = asyncio.create_task(inst.start("demo", "v2"))
            await asyncio.wait_for(entered.wait(), 5)
            self.assertIn("pre.zip", inst.protected_backups())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(inst._active_backups, {"other.zip"})
            self.assertFalse(inst.busy)
        with mock.patch.object(backupkit, "prune"):
            asyncio.run(check())

    def test_failed_pip_releases_temporary_protection(self):
        inst, _, path = self.fixture()
        inst._install_requirements.side_effect = [["nestedpkg"], []]
        with mock.patch.object(backupkit, "prune"):
            result = asyncio.run(inst.start("demo", "v2"))
        self.assertFalse(result["ok"])
        self.assertEqual(inst._active_backups, set())
        self.assertFalse(inst.busy)
        self.assertTrue(os.path.isfile(path))

    def test_copy_visible_before_backup_returns_cannot_be_deleted(self):
        inst, view, path = self.fixture()
        async def backup(label):
            # create() has published the ZIP, but its executor has not returned the name.
            self.assertFalse((await self.delete(view))["ok"])
            self.assertTrue(inst.busy)
            return {"name": "pre.zip"}
        inst.async_backup = backup
        inst._install_requirements.side_effect = [["nestedpkg"], []]
        with mock.patch.object(backupkit, "prune"):
            asyncio.run(inst.start("demo", "v2"))
        self.assertTrue(os.path.isfile(path))

    def test_delete_claims_busy_through_checks_and_releases_on_error_or_cancel(self):
        for failure in (OSError("disk error"), asyncio.CancelledError()):
            with self.subTest(failure=type(failure).__name__):
                inst, view, _ = self.fixture()
                async def executor(fn, *args):
                    if fn is os.remove:
                        self.assertTrue(inst.busy)
                        raise failure
                    if getattr(fn, "__name__", "") == "<lambda>":
                        self.assertTrue(inst.busy)
                        # A start cannot enter while deletion's protection check yields.
                        result = await inst.start("demo", "v2")
                        self.assertFalse(result["ok"])
                    return fn(*args)
                inst.hass.async_add_executor_job = executor
                with mock.patch.object(backupkit, "restore_needs", return_value=set()), \
                        mock.patch.object(backupkit, "app_backup_running", return_value=False):
                    if isinstance(failure, asyncio.CancelledError):
                        with self.assertRaises(asyncio.CancelledError):
                            asyncio.run(self.delete(view))
                    else:
                        self.assertFalse(asyncio.run(self.delete(view))["ok"])
                self.assertFalse(inst.busy)

    def test_cancelled_delete_keeps_ownership_until_the_executor_finishes(self):
        inst, view, path = self.fixture()
        async def check():
            entered, release = asyncio.Event(), asyncio.Event()
            async def executor(fn, *args):
                if fn is os.remove:
                    entered.set()
                    await release.wait()  # like executor I/O, cancellation must not stop this work
                return fn(*args)
            inst.hass.async_add_executor_job = executor
            task = asyncio.create_task(self.delete(view))
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            await asyncio.sleep(0)
            self.assertTrue(inst.busy)
            task.cancel()  # repeated cancellation must not abandon the running unlink either
            await asyncio.sleep(0)
            self.assertTrue(inst.busy)
            self.assertFalse((await inst.start("demo", "v2"))["ok"])
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(inst.busy)
            self.assertFalse(os.path.exists(path))
        with mock.patch.object(backupkit, "restore_needs", return_value=set()), \
                mock.patch.object(backupkit, "app_backup_running", return_value=False):
            asyncio.run(check())
