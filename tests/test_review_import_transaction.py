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
        # the import's record holds its source, never the clean start's plan or set-aside .storage
        self.assertTrue(ha_import.drop_rebuild(self.cfg))
        self.assertFalse(os.path.exists(plan))
        self.assertFalse(os.path.exists(aside))
        self.assertTrue(os.path.isfile(os.path.join(self.source, "hub.e1")))
        with self.assertRaisesRegex(ValueError, "incomplete import"):
            await ha_import.apply_all(self.hass, self.aligner, None, False, False, None, {"hub"})

    def phase(self):
        with open(os.path.join(self.cfg, ha_import.IMPORT_PENDING_FILE), encoding="utf-8") as fh:
            return json.load(fh)["phase"]

    async def interrupt_add(self, *, inserted):
        """HA stops (and cancels the import) inside async_add, with the entry in memory or not yet."""
        reached = asyncio.Event()

        async def add(entry):
            self.phases.append(self.phase())
            if inserted:
                self.entries[entry.entry_id] = entry
                self.pending = True
            reached.set()
            await asyncio.Event().wait()

        self.phases = []
        self.hass.config_entries.async_add = add
        task = asyncio.create_task(self.apply())
        await asyncio.wait_for(reached.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.phases, ["added"])  # durable before HA could write the entry

    async def resolve(self):
        with mock.patch.object(ha_import, "_forget_cached_stores") as forget, mock.patch.object(ha_import.events, "emit"):
            return await ha_import.async_resolve_pending(self.hass), forget

    async def test_resolve_keeps_the_imported_stores_of_an_entry_home_assistant_has(self):
        self.put(self.storage, "hub.e1", "original")
        await self.interrupt_add(inserted=True)
        message, _forget = await self.resolve()
        self.assertIn("imported stores are kept", message)
        self.assertEqual(self.read("hub.e1"), "imported")
        self.assertFalse(os.path.exists(os.path.join(self.storage, "hub.e1.pre-import")))
        self.assertFalse(os.path.exists(os.path.join(self.cfg, ha_import.IMPORT_PENDING_FILE)))
        self.assertEqual([e["entry_id"] for e in ha_import._saved_config_entries(self.cfg)], ["e1"])  # confirmed first
        ha_import.clear(self.cfg)  # no longer held
        self.assertFalse(os.path.exists(self.source))

    async def test_resolve_puts_the_originals_back_without_the_entry(self):
        self.put(self.storage, "hub.e1", "original")
        await self.interrupt_add(inserted=False)
        message, forget = await self.resolve()
        self.assertIn("original stores were put back", message)
        self.assertEqual(self.read("hub.e1"), "original")
        forget.assert_called_once_with(self.hass, ["hub.e1"])
        self.assertFalse(os.path.exists(os.path.join(self.cfg, ha_import.IMPORT_PENDING_FILE)))

    async def test_resolve_changes_nothing_while_the_entry_cannot_be_confirmed_on_disk(self):
        self.put(self.storage, "hub.e1", "original")
        await self.interrupt_add(inserted=True)
        self.writes = ["swallow"] * ha_import._PERSIST_ATTEMPTS
        with self.assertRaisesRegex(ValueError, "nothing was changed; restart"):
            await self.resolve()
        self.assertEqual(self.read("hub.e1.pre-import"), "original")
        self.assertTrue(os.path.isfile(os.path.join(self.cfg, ha_import.IMPORT_PENDING_FILE)))

    async def test_resolve_refuses_to_drop_an_unreadable_record_while_an_original_is_set_aside(self):
        # the next boot, finding no record, would otherwise put the original back under a loaded imported entry
        self.put(self.storage, "hub.e1", "imported")
        self.put(self.storage, "hub.e1.pre-import", "original")
        journal = os.path.join(self.cfg, ha_import.IMPORT_PENDING_FILE)
        os.makedirs(os.path.dirname(journal), exist_ok=True)
        for record in ("not JSON", json.dumps({"version": 1, "entry_id": "e1", "domain": "hub", "phase": "sideways",
                                               "stores": [{"name": "hub.e1", "had_original": True}]})):
            with self.subTest(record=record):
                with open(journal, "w", encoding="utf-8") as fh:
                    fh.write(record)
                with self.assertRaisesRegex(ValueError, r"unreadable.*hub\.e1\.pre-import.*restart"):
                    await self.resolve()
                self.assertTrue(os.path.isfile(journal))
                self.assertEqual(self.read("hub.e1"), "imported")
                self.assertEqual(self.read("hub.e1.pre-import"), "original")

    async def test_resolve_drops_an_unreadable_record_with_nothing_set_aside(self):
        journal = os.path.join(self.cfg, ha_import.IMPORT_PENDING_FILE)
        os.makedirs(os.path.dirname(journal), exist_ok=True)
        with open(journal, "w", encoding="utf-8") as fh:
            fh.write("not JSON")
        message, _forget = await self.resolve()
        self.assertIn("unreadable", message)
        self.assertNotIn("puts back", message)
        self.assertFalse(os.path.exists(journal))

    async def test_resolve_view_runs_under_the_import_lock_and_answers_the_outcome(self):
        from custom_components.integration_manager import import_views

        await self.interrupt_add(inserted=False)
        installer = SimpleNamespace(busy=False)
        request = SimpleNamespace(headers={}, query={}, content_type="application/json", json=mock.AsyncMock(return_value={}))
        with mock.patch.object(ha_import, "_forget_cached_stores"), mock.patch.object(ha_import.events, "emit"):
            response = await import_views.ImportResolveView(self.hass, installer).post(request)
        body = json.loads(response.body)
        self.assertTrue(body["ok"], body)
        self.assertIn("put back", body["message"])
        self.assertFalse(installer.busy)
        self.assertIsNone(ha_import.pending_import(self.cfg))

    async def test_an_entry_deleted_moments_ago_is_flushed_not_refused(self):
        self.write([{"entry_id": "e1", "domain": "hub"}])  # deleted here, HA writes that SAVE_DELAY later
        result = await self.apply()
        self.assertEqual(result["entry_id"], "e1")

    async def test_an_entry_whose_removal_cannot_be_written_is_refused_with_the_reason(self):
        self.write([{"entry_id": "e1", "domain": "hub"}])
        self.writes = ["swallow"]
        with self.assertRaisesRegex(ValueError, "that save failed"):
            await self.apply()
        self.assertFalse(os.path.exists(os.path.join(self.cfg, ha_import.IMPORT_PENDING_FILE)))

    async def test_ha_stop_inside_async_add_during_a_rebuild_is_settled_at_the_next_boot(self):
        """#122 keeps the clean start's plan when HA cancels the rebuild; the import's record must not pin it."""
        from homeassistant.core import CoreState
        from tests.fakes import entrypoint_for

        plan_path = os.path.join(self.cfg, ha_import.REBUILD_FILE)
        with open(plan_path, "w", encoding="utf-8") as fh:
            json.dump({"stage": "import", "domain": "hub", "backup": "pre.zip", "to": "2026.1.0"}, fh)
        with open(os.path.join(self.cfg, ha_import.SUMMARY_FILE), encoding="utf-8") as fh:
            summary = json.load(fh)
        with open(os.path.join(self.cfg, ha_import.SUMMARY_FILE), "w", encoding="utf-8") as fh:
            json.dump({**summary, "type": ha_import.REBUILD_TYPE}, fh)
        aside = os.path.join(self.cfg, ".storage.pre-rebuild-test")
        os.makedirs(aside)
        self.hass.state = CoreState.running
        installer = SimpleNamespace(busy=False, running="hub", state=SimpleNamespace(installed={"hub"}))
        reached = asyncio.Event()

        async def add(entry):
            self.entries[entry.entry_id] = entry
            self.pending = True
            reached.set()
            await asyncio.Event().wait()

        self.hass.config_entries.async_add = add
        loader = mock.Mock(async_get_integration=mock.AsyncMock(return_value=SimpleNamespace(is_built_in=False)))
        with mock.patch.object(ha_import, "loader", loader), mock.patch.object(ha_import, "_forget_cached_stores"), \
                mock.patch.object(ha_import, "pn") as pn, mock.patch.object(ha_import.events, "emit"):
            task = asyncio.create_task(ha_import.async_finish_rebuild(self.hass, self.aligner, installer))
            await asyncio.wait_for(reached.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(self.phase(), "added")
            self.assertTrue(os.path.isfile(plan_path) and os.path.isdir(aside))
            await self.hass.config_entries._store._async_handle_write_data()  # HA's final write on the way out

            ep = entrypoint_for(self, self.cfg)
            with mock.patch.object(ep, "log"):
                ep.clean_import_leftovers()  # the next boot
            self.assertIsNone(ha_import.pending_import(self.cfg))
            self.assertEqual(self.read("hub.e1"), "imported")
            self.assertTrue(os.path.isfile(plan_path))  # still the clean start's, with its source
            self.assertTrue(os.path.isfile(os.path.join(self.source, "hub.e1")))

            await ha_import.async_finish_rebuild(self.hass, self.aligner, installer)  # after the next start
        self.assertFalse(os.path.exists(plan_path))
        self.assertFalse(os.path.exists(aside))
        self.assertFalse(os.path.exists(os.path.join(self.cfg, ha_import.EXTRACT_DIR)))
        self.assertNotIn("failed", pn.async_create.call_args.args[1])


class ResolveButtonTest(unittest.TestCase):
    def test_the_import_page_shows_the_blocked_state_with_a_resolve_button(self):
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "custom_components", "integration_manager")
        with open(os.path.join(root, "templates", "system.html"), encoding="utf-8") as fh:
            self.assertIn('id="imresolve"', fh.read())
        with open(os.path.join(root, "static", "system.js"), encoding="utf-8") as fh:
            js = fh.read()
        self.assertIn("r.pending_import", js)
        self.assertIn("post('api/import/resolve')", js)
