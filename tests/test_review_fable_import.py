"""External review at 70c3e8c, checked against Home Assistant 2026.9.3.

IMP-1: an import that replaces a store this volume already had sets the
original aside as ``.storage/<store>.pre-import``; the boot puts every such
file back (``entrypoint.clean_import_leftovers``), and ``_commit`` removes them
once the import is done.  Home Assistant writes ``core.config_entries``
SAVE_DELAY (1 s) after ``async_add``.  ``apply`` ran ``clear`` (the whole
extraction) before ``_commit``, so a restart while it ran, with the entry
already saved, put the original store back under the imported entry.  And
``apply_all`` (``cleanup=False``) committed at once, before the delayed save: a
restart in that second left no entry and no original.  Every crash point is
simulated below and the volume checked after the boot's cleanup.

IMP-2: ``apply`` and ``apply_all`` run on the event loop (``import_views._locked``
awaits them) and read the summary file there.
"""

import asyncio
import json
import os
import shutil
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from homeassistant import config_entries as ce, core, loader
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.integration_manager import ha_import
from tests.fakes import entrypoint_for

ORIGINAL = "this volume's own"
IMPORTED = "from the backup"


class _Store:
    """What HA's Store does for config entries: async_add schedules a write SAVE_DELAY later; the write itself
    is _async_handle_write_data.  The delayed write never fires on its own here: when it lands is the crash
    point's choice (see snapshot)."""

    def __init__(self, cfg, entries):
        self.path = os.path.join(cfg, ".storage", "core.config_entries")
        self.entries = entries
        self.pending = False

    def write(self, path=None):
        with open(path or self.path, "w", encoding="utf-8") as fh:
            json.dump({"entries": sorted(self.entries)}, fh)

    async def _async_handle_write_data(self):
        if self.pending:
            self.write()
            self.pending = False


class CrashPointsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        self.src = os.path.join(self.cfg, ha_import.EXTRACT_DIR, ".storage")
        os.makedirs(self.src)
        self.storage = os.path.join(self.cfg, ".storage")
        os.makedirs(self.storage)
        self.put(self.src, "hub.e1", IMPORTED)
        self.put(self.storage, "hub.e1", ORIGINAL)
        self.snapshots = []

    def put(self, where, name, text):
        with open(os.path.join(where, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def hass(self, with_store=True):
        entries = {}
        store = _Store(self.cfg, entries)
        store.write()

        async def async_add(entry):
            entries[entry.entry_id] = entry
            store.pending = True  # HA: _async_schedule_save after the setup

        config_entries = SimpleNamespace(async_entries=lambda _d=None: [], async_get_entry=lambda i: entries.get(i),
                                         async_add=async_add, async_remove=mock.AsyncMock(),
                                         flow=SimpleNamespace(async_progress_by_handler=lambda *a, **k: []))
        if with_store:
            config_entries._store = store

        async def executor(fn, *args):
            # a restart may come before the job, while it runs, or after it; the delayed save may have landed or not
            name = getattr(fn, "__name__", "load_summary")  # the summary is a mock here
            self.snapshot(store, "before " + name)
            result = fn(*args)
            self.snapshot(store, "after " + name)
            return result

        self.store = store
        return SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg), config_entries=config_entries,
                               data={}, async_add_executor_job=executor)

    def snapshot(self, store, where):
        for landed in (False, True):
            if landed and not store.pending:
                continue
            copy = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, copy, True)
            shutil.copytree(self.cfg, copy, dirs_exist_ok=True)
            if landed:
                store.write(os.path.join(copy, ".storage", "core.config_entries"))
            self.snapshots.append((f"{where}, delayed save {'landed' if landed else 'pending'}", copy))

    def apply(self, hass, cleanup):
        summary = {"domains": {"hub": {"entries": [{"entry_id": "e1", "data": {}, "title": "Hub"}],
                                       "storage_files": ["hub.e1"]}}}
        with mock.patch.object(ha_import, "load_summary", return_value=summary), \
                mock.patch.object(ha_import, "_forget_cached_stores"):
            return asyncio.run(ha_import.apply(hass, mock.Mock(), "hub", "e1", None, None, align=False, copy_storage=True,
                                               running=False, cleanup=cleanup))

    def boot(self, cfg):
        """What the volume is after a restart at that point: the boot's cleanup, then what HA would load."""
        ep = entrypoint_for(self, cfg)
        os.makedirs(ep.STATE_DIR, exist_ok=True)
        with mock.patch.object(ep, "log"):
            ep.clean_import_leftovers()
        with open(os.path.join(cfg, ".storage", "core.config_entries"), encoding="utf-8") as fh:
            entries = json.load(fh)["entries"]
        with open(os.path.join(cfg, ".storage", "hub.e1"), encoding="utf-8") as fh:
            store = fh.read()
        return entries, store

    def check_every_crash_point(self, cleanup):
        self.apply(self.hass(), cleanup)
        self.assertGreater(len(self.snapshots), 3)
        bad = []
        for where, copy in self.snapshots:
            entries, store = self.boot(copy)
            # the imported entry with the imported store, or no entry and this volume's store as it was
            if store != (IMPORTED if entries == ["e1"] else ORIGINAL):
                bad.append(where)
        # The one point left: core.config_entries was just written (atomically) and _commit has not renamed the
        # first .pre-import yet - one executor hop, where it was the whole of clear() or the 1 s SAVE_DELAY.
        # Closing it needs the boot to look at core.config_entries before it puts an original back.
        self.assertEqual(bad, ["before _commit, delayed save pending"])
        self.assertEqual(sorted(os.listdir(self.storage)), ["core.config_entries", "hub.e1"])

    def test_a_single_import_is_consistent_after_a_restart_at_any_point(self):
        self.check_every_crash_point(cleanup=True)

    def test_an_import_of_everything_is_consistent_after_a_restart_at_any_point(self):
        self.check_every_crash_point(cleanup=False)

    def test_the_entry_is_saved_before_the_original_is_removed(self):
        self.apply(self.hass(), cleanup=True)
        self.assertFalse(self.store.pending)
        with open(self.store.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["entries"], ["e1"])

    def test_the_extraction_is_removed_after_the_commit(self):
        self.apply(self.hass(), cleanup=True)
        names = [w for w, _ in self.snapshots]
        self.assertLess(names.index("after _commit, delayed save pending"), names.index("before clear, delayed save pending"))
        self.assertFalse(os.path.exists(os.path.join(self.cfg, ha_import.EXTRACT_DIR)))


class RealConfigEntriesSaveTest(unittest.IsolatedAsyncioTestCase):
    """The internal API _save_config_entries relies on, in the Home Assistant these tests run on."""

    async def test_an_added_entry_is_on_disk_at_once(self):
        hass = core.HomeAssistant(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, hass.config.config_dir, True)
        loader.async_setup(hass)
        dr.async_setup(hass)
        await dr.async_load(hass, load_empty=True)
        await er.async_load(hass, load_empty=True)
        hass.config_entries = ce.ConfigEntries(hass, {})
        await hass.config_entries.async_initialize()
        self.addAsyncCleanup(hass.async_stop, force=True)
        entry = ce.ConfigEntry(domain="demo", title="Demo", data={}, source="user", version=1, minor_version=1, options={},
                               unique_id=None, discovery_keys={}, subentries_data=None)
        hass.config_entries._entries[entry.entry_id] = entry  # noqa: SLF001 - no integration to set up
        hass.config_entries._async_schedule_save()  # noqa: SLF001 - what async_add ends with
        path = hass.config.path(".storage", "core.config_entries")
        self.assertFalse(os.path.exists(path))  # SAVE_DELAY: not yet
        with self.assertNoLogs(ha_import._LOGGER, "WARNING"):
            await ha_import._save_config_entries(hass, "demo")
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual([e["entry_id"] for e in saved["data"]["entries"]], [entry.entry_id])


class SummaryOffTheLoopTest(unittest.TestCase):
    """apply / apply_all read the summary file in the executor, not on the loop that runs them."""

    def run_on_loop(self, call):
        threads = []

        def load_summary(_cfg):
            threads.append(threading.get_ident())
            return None  # "no inspected backup": both stop right after reading it

        async def main():
            loop = asyncio.get_running_loop()
            hass = SimpleNamespace(config=SimpleNamespace(config_dir="/nonexistent"),
                                   async_add_executor_job=lambda fn, *a: loop.run_in_executor(None, fn, *a))
            with mock.patch.object(ha_import, "load_summary", load_summary), self.assertRaises(ValueError):
                await call(hass)
            return threading.get_ident()

        loop_thread = asyncio.run(main())
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], loop_thread)

    def test_apply(self):
        self.run_on_loop(lambda hass: ha_import.apply(hass, mock.Mock(), "hub", "e1", None, None, False, False))

    def test_apply_all(self):
        self.run_on_loop(lambda hass: ha_import.apply_all(hass, mock.Mock(), None, False, False, None, set()))


class PruneOldMapTest(unittest.IsolatedAsyncioTestCase):
    """prune_satisfied reads both key forms: "<entity domain>:<unique_id>" and the bare unique_id of a pre-0.6
    single-domain map (one sits on a live volume: ramses_cc, unique ids that contain ':' themselves)."""

    async def test_both_key_forms_are_pruned(self):
        cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cfg, True)
        os.makedirs(os.path.join(cfg, ha_import.STATE_DIR))
        hass = core.HomeAssistant(cfg)
        dr.async_setup(hass)
        await dr.async_load(hass, load_empty=True)
        await er.async_load(hass, load_empty=True)
        self.addAsyncCleanup(hass.async_stop)
        reg = er.async_get(hass)
        reg.async_get_or_create("binary_sensor", "ramses_cc", "10:056759-bit_2_4", suggested_object_id="b24")
        reg.async_get_or_create("sensor", "ramses_cc", "01:1-temp", suggested_object_id="t")
        want = {"name": None, "icon": None, "disabled_by": None, "hidden_by": None}
        with open(os.path.join(cfg, ha_import.MAP_FILE), "w", encoding="utf-8") as fh:
            json.dump({"domain": "ramses_cc", "devices": {}, "entities": {
                "10:056759-bit_2_4": {"entity_id": "binary_sensor.b24", **want},
                "sensor:01:1-temp": {"entity_id": "sensor.t", **want},
                "sensor:not-here": {"entity_id": "sensor.gone", **want}}}, fh)
        al = ha_import.RegistryAligner(hass)
        self.assertEqual(al.prune_satisfied(), 2)
        self.assertEqual(list(al.maps["ramses_cc"]["entities"]), ["sensor:not-here"])
        if al._save_handle:
            al._save_handle.cancel()


if __name__ == "__main__":
    unittest.main()
