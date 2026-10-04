"""A shutdown cancellation must leave the clean-start recovery available."""
import asyncio
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from homeassistant.core import CoreState
from custom_components.integration_manager import ha_import, import_views


class RebuildCancellationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cfg = tempfile.mkdtemp(prefix="hri-cancel-rebuild-")
        self.addCleanup(shutil.rmtree, self.cfg, True)
        os.makedirs(os.path.join(self.cfg, ha_import.EXTRACT_DIR))
        self.plan = {"stage": "import", "domain": "hub", "backup": "pre.zip", "to": "2026.1.0"}
        self.summary = {"type": ha_import.REBUILD_TYPE, "domains": {"hub": {"entries": [
            {"entry_id": "e1", "data": {}}, {"entry_id": "e2", "data": {}}]}}}
        self.write(ha_import.REBUILD_FILE, self.plan)
        self.write(ha_import.SUMMARY_FILE, self.summary)
        self.aside = os.path.join(self.cfg, ".storage.pre-rebuild-test")
        os.makedirs(self.aside)
        with open(os.path.join(self.aside, "original"), "w", encoding="utf-8") as fh:
            fh.write("recovery")

        async def executor(fn, *args):
            return fn(*args)

        self.entries = []
        self.hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg), state=CoreState.running,
            async_add_executor_job=executor, config_entries=SimpleNamespace(
                async_entries=lambda domain=None: self.entries,
                async_get_entry=lambda eid: next((e for e in self.entries if e.entry_id == eid), None)))
        self.installer = SimpleNamespace(busy=False, running="hub", state=SimpleNamespace(installed={"hub"}))
        self.aligner = mock.Mock()

    def write(self, relative, value):
        with open(os.path.join(self.cfg, relative), "w", encoding="utf-8") as fh:
            json.dump(value, fh)

    def assert_recovery(self):
        for rel in (ha_import.REBUILD_FILE, ha_import.SUMMARY_FILE, ".storage.pre-rebuild-test/original"):
            self.assertTrue(os.path.isfile(os.path.join(self.cfg, rel)), rel)

    async def cancel_at(self, reached, task):
        await asyncio.wait_for(reached.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_recovery()

    async def test_shutdown_while_waiting_for_busy_manager_keeps_untouched_plan(self):
        reached = asyncio.Event()

        async def busy(*args):
            reached.set()
            raise import_views.ImportBusy("busy")

        with mock.patch.object(import_views, "_locked", busy):
            task = asyncio.create_task(ha_import.async_finish_rebuild(self.hass, self.aligner, self.installer))
            await self.cancel_at(reached, task)
        with open(os.path.join(self.cfg, ha_import.REBUILD_FILE), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), self.plan)

    async def test_partial_bulk_import_cancellation_keeps_source_for_next_boot(self):
        reached = asyncio.Event()

        async def apply(hass, aligner, domain, entry_id, *args, **kwargs):
            if entry_id == "e2":
                reached.set()
                await asyncio.Event().wait()
            self.entries.append(SimpleNamespace(entry_id=entry_id, domain=domain, unique_id=None))
            return {"state": "loaded", "entry_id": entry_id}

        with mock.patch.object(ha_import, "apply", apply):
            task = asyncio.create_task(ha_import.async_finish_rebuild(self.hass, self.aligner, self.installer))
            await self.cancel_at(reached, task)
        self.assertEqual([e.entry_id for e in self.entries], ["e1"])
        self.assertFalse(self.installer.busy)
        imported = []

        async def retry(hass, aligner, domain, entry_id, *args, **kwargs):
            imported.append(entry_id)
            return {"state": "loaded", "entry_id": entry_id}

        with mock.patch.object(ha_import, "apply", retry), mock.patch.object(ha_import, "pn"), mock.patch.object(ha_import.events, "emit"):
            await ha_import.async_finish_rebuild(self.hass, self.aligner, self.installer)
        self.assertEqual(imported, ["e2"])
        self.assertFalse(os.path.exists(os.path.join(self.cfg, ha_import.REBUILD_FILE)))
        self.assertFalse(os.path.exists(os.path.join(self.cfg, ha_import.EXTRACT_DIR)))
        self.assertFalse(os.path.exists(self.aside))
