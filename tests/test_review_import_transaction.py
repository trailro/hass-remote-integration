"""A swallowed HA Store write error cannot commit an import transaction."""
import asyncio
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import ha_import


class ImportTransactionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cfg = tempfile.mkdtemp(prefix="hri-import-transaction-")
        self.addCleanup(shutil.rmtree, self.cfg, True)
        self.source = os.path.join(self.cfg, ha_import.EXTRACT_DIR, ".storage")
        self.storage = os.path.join(self.cfg, ".storage")
        os.makedirs(self.source)
        os.makedirs(self.storage)
        self.put(self.source, "hub.e1", "imported")
        summary = {"domains": {"hub": {"entries": [{"entry_id": "e1", "data": {"host": "source"}}],
                                        "storage_files": ["hub.e1"]}}}
        with open(os.path.join(self.cfg, ha_import.SUMMARY_FILE), "w", encoding="utf-8") as fh:
            json.dump(summary, fh)
        self.entries = {}
        self.writes = []
        self.pending = False
        self.schedule_count = 0
        self.write([])

        def schedule():
            self.schedule_count += 1
            self.pending = True

        async def flush():
            if not self.pending:
                return
            self.pending = False  # HA consumes the callback/listeners before the write
            action = self.writes.pop(0) if self.writes else "write"
            if action == "raise":
                raise OSError("test write failure")
            if action == "swallow":
                return  # HA logs WriteError and returns normally without data on disk
            records = [e.as_dict() for e in self.entries.values()]
            if action == "stale":
                records[0] = {**records[0], "data": {"host": "stale"}}
            self.write(records)

        async def add(entry):
            self.entries[entry.entry_id] = entry
            schedule()

        async def remove(entry_id):
            self.entries.pop(entry_id)
            schedule()

        async def executor(fn, *args):
            return fn(*args)

        self.hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg), data={}, async_add_executor_job=executor,
            config_entries=SimpleNamespace(async_entries=lambda domain=None: list(self.entries.values()),
                async_get_entry=self.entries.get, async_add=add, async_remove=remove, _async_schedule_save=schedule,
                _store=SimpleNamespace(_async_handle_write_data=flush)))
        self.aligner = mock.Mock()

    def put(self, directory, name, value):
        with open(os.path.join(directory, name), "w", encoding="utf-8") as fh:
            fh.write(value)

    def write(self, entries):
        with open(os.path.join(self.storage, "core.config_entries"), "w", encoding="utf-8") as fh:
            json.dump({"data": {"entries": entries}}, fh)

    def read(self, name):
        with open(os.path.join(self.storage, name), encoding="utf-8") as fh:
            return fh.read()

    async def apply(self):
        with mock.patch.object(ha_import, "_forget_cached_stores"):
            return await ha_import.apply(self.hass, self.aligner, "hub", "e1", None, None, False, True, running=False)

    async def test_swallowed_write_failure_restores_original_and_keeps_retry_source(self):
        self.put(self.storage, "hub.e1", "original")
        self.writes = ["swallow"] * ha_import._PERSIST_ATTEMPTS + ["write"]
        with self.assertRaisesRegex(ValueError, "stores restored"):
            await self.apply()
        self.assertEqual(self.entries, {})
        self.assertEqual(self.read("hub.e1"), "original")
        self.assertFalse(os.path.exists(os.path.join(self.storage, "hub.e1.pre-import")))
        self.assertTrue(os.path.isfile(os.path.join(self.source, "hub.e1")))
        self.assertFalse(self.pending)
        self.assertGreaterEqual(self.schedule_count, 4)  # rollback was explicitly rescheduled, not left to a consumed listener

    async def test_unconfirmed_rollback_retains_original_recovery_and_source(self):
        self.put(self.storage, "hub.e1", "original")
        self.writes = ["stale"] * ha_import._PERSIST_ATTEMPTS + ["swallow"] * ha_import._PERSIST_ATTEMPTS
        with self.assertRaisesRegex(ValueError, "rollback persistence is unconfirmed"):
            await self.apply()
        self.assertEqual(self.entries, {})
        self.assertEqual(self.read("hub.e1.pre-import"), "original")
        self.assertEqual(self.read("hub.e1"), "imported")
        self.assertTrue(os.path.isfile(os.path.join(self.source, "hub.e1")))
        with self.assertRaisesRegex(ValueError, "earlier import"):
            await self.apply()
        self.assertEqual(self.read("hub.e1.pre-import"), "original")

    async def test_raised_write_failure_also_undoes_a_previously_absent_store(self):
        self.writes = ["raise", "write"]
        with self.assertRaises(ValueError):
            await self.apply()
        self.assertFalse(os.path.exists(os.path.join(self.storage, "hub.e1")))
        self.assertTrue(os.path.isfile(os.path.join(self.source, "hub.e1")))

    async def test_failed_first_copy_removes_its_partial_destination(self):
        def fail_copy(source, destination):
            with open(destination, "w", encoding="utf-8") as fh:
                fh.write("partial")
            raise OSError("test short copy")

        with mock.patch.object(ha_import.shutil, "copyfile", fail_copy), self.assertRaises(ValueError):
            await self.apply()
        self.assertFalse(os.path.exists(os.path.join(self.storage, "hub.e1")))
        self.assertEqual(self.entries, {})
        self.assertTrue(os.path.isfile(os.path.join(self.source, "hub.e1")))

    async def test_confirmed_write_commits_and_removes_source(self):
        self.put(self.storage, "hub.e1", "original")
        result = await self.apply()
        self.assertEqual(result["entry_id"], "e1")
        self.assertTrue(result["cleaned_up"])
        self.assertEqual(self.read("hub.e1"), "imported")
        self.assertFalse(os.path.exists(os.path.join(self.storage, "hub.e1.pre-import")))
        self.assertFalse(os.path.exists(os.path.join(self.cfg, ha_import.EXTRACT_DIR)))

    async def test_failed_unload_does_not_restore_stores_used_by_live_integration(self):
        self.put(self.storage, "hub.e1", "original")
        self.writes = ["swallow"] * ha_import._PERSIST_ATTEMPTS
        self.hass.config_entries.async_remove = mock.AsyncMock(return_value={"require_restart": True})
        with self.assertRaisesRegex(ValueError, "rollback persistence is unconfirmed"):
            await self.apply()
        self.assertEqual(self.read("hub.e1.pre-import"), "original")
        self.assertEqual(self.read("hub.e1"), "imported")
        self.assertTrue(os.path.isfile(os.path.join(self.source, "hub.e1")))

    async def test_failed_store_restore_reports_incomplete_recovery(self):
        self.put(self.storage, "hub.e1", "original")
        self.writes = ["swallow"] * ha_import._PERSIST_ATTEMPTS + ["write"]
        replace = os.replace

        def fail_restore(source, destination):
            if source.endswith(".pre-import"):
                raise OSError("test restore failure")
            return replace(source, destination)

        with mock.patch.object(ha_import.os, "replace", fail_restore), self.assertRaisesRegex(ValueError, "store rollback is incomplete"):
            await self.apply()
        self.assertEqual(self.entries, {})
        self.assertEqual(self.read("hub.e1.pre-import"), "original")
        self.assertTrue(os.path.isfile(os.path.join(self.source, "hub.e1")))

    async def test_cancellation_after_write_before_verification_retains_boot_recovery(self):
        self.put(self.storage, "hub.e1", "original")
        reached = asyncio.Event()
        executor = self.hass.async_add_executor_job

        async def paused(fn, *args):
            if fn is ha_import._saved_config_entries:
                reached.set()
                await asyncio.Event().wait()
            return await executor(fn, *args)

        self.hass.async_add_executor_job = paused
        task = asyncio.create_task(self.apply())
        await asyncio.wait_for(reached.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(os.path.isfile(os.path.join(self.cfg, ha_import.IMPORT_PENDING_FILE)))
        ha_import.clear(self.cfg)  # apply_all's cancellation cleanup cannot erase unresolved recovery
        self.assertEqual(self.read("hub.e1.pre-import"), "original")
        self.assertTrue(os.path.isfile(os.path.join(self.source, "hub.e1")))

    async def test_unresolved_import_also_protects_rebuild_leaf_cleanup(self):
        self.put(self.storage, "hub.e1", "original")
        self.writes = ["stale"] * ha_import._PERSIST_ATTEMPTS + ["swallow"] * ha_import._PERSIST_ATTEMPTS
        with self.assertRaises(ValueError):
            await self.apply()
        plan = os.path.join(self.cfg, ha_import.REBUILD_FILE)
        with open(plan, "w", encoding="utf-8") as fh:
            json.dump({"stage": "import", "domain": "hub"}, fh)
        aside = os.path.join(self.cfg, ".storage.pre-rebuild-test")
        os.makedirs(aside)
        ha_import.clear_extracted(self.cfg)
        self.assertFalse(ha_import.drop_rebuild(self.cfg))
        self.assertTrue(os.path.isfile(plan))
        self.assertTrue(os.path.isdir(aside))
        self.assertTrue(os.path.isfile(os.path.join(self.source, "hub.e1")))
        with self.assertRaisesRegex(ValueError, "incomplete import"):
            await ha_import.apply_all(self.hass, self.aligner, None, False, False, None, {"hub"})
