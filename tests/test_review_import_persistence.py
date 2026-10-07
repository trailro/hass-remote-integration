"""The import's persistence check against Home Assistant's own ConfigEntries and Store.

The check used to compare the whole saved entry with the live ``as_dict()`` read after the flush: any
``async_update_entry`` on the new entry while the executor read the file (its setup, a token refresh, only
``modified_at``) looked like a failed write, and the import removed a working entry and put the old stores back.
"""

import json
import shutil
import tempfile
import unittest
from unittest import mock

from homeassistant import config_entries as ce
from homeassistant import core, loader
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import storage
from homeassistant.util.file import WriteError

from custom_components.integration_manager import ha_import


class RealPersistenceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hass = core.HomeAssistant(tempfile.mkdtemp(prefix="hri-import-persist-"))
        self.addCleanup(shutil.rmtree, self.hass.config.config_dir, True)
        loader.async_setup(self.hass)
        dr.async_setup(self.hass)
        await dr.async_load(self.hass, load_empty=True)
        await er.async_load(self.hass, load_empty=True)
        self.hass.config_entries = ce.ConfigEntries(self.hass, {})
        await self.hass.config_entries.async_initialize()
        self.addAsyncCleanup(self.hass.async_stop, force=True)
        self.entry = ce.ConfigEntry(domain="demo", title="Demo", data={"token": "imported"}, source="import", version=2,
                                    minor_version=1, options={"scan": 30}, unique_id=None, discovery_keys={},
                                    subentries_data=None)
        self.hass.config_entries._entries[self.entry.entry_id] = self.entry  # noqa: SLF001 - no integration to set up
        self.hass.config_entries._async_schedule_save()  # noqa: SLF001 - what async_add ends with

    def saved(self):
        with open(self.hass.config.path(".storage", "core.config_entries"), encoding="utf-8") as fh:
            return json.load(fh)["data"]["entries"]

    def update_during_write(self, **changes):
        """async_update_entry on the loop right after the Store's executor write of the flush: the file holds the
        entry as it was, the live entry has moved on (and so has modified_at) when the check reads both."""
        real = self.hass.async_add_executor_job
        writes = []

        async def job(fn, *args):
            result = await real(fn, *args)
            if getattr(fn, "__name__", "").startswith("_write_") and not writes:
                writes.append(fn)
                self.hass.config_entries.async_update_entry(self.entry, **changes)
            return result

        return mock.patch.object(self.hass, "async_add_executor_job", job)

    async def test_a_token_refresh_during_the_check_is_saved_not_rolled_back(self):
        with self.update_during_write(data={"token": "refreshed"}), \
                mock.patch.object(ha_import, "_flush_config_entries", wraps=ha_import._flush_config_entries) as flush:
            await ha_import._save_config_entries(self.hass, "demo", self.entry.entry_id)
        self.assertEqual(flush.call_count, 2)  # the first comparison saw the update the write missed
        self.assertEqual([e["data"] for e in self.saved()], [{"token": "refreshed"}])

    async def test_a_change_to_what_the_import_did_not_set_is_not_compared(self):
        with self.update_during_write(title="Renamed by setup"), \
                mock.patch.object(ha_import, "_flush_config_entries", wraps=ha_import._flush_config_entries) as flush:
            await ha_import._save_config_entries(self.hass, "demo", self.entry.entry_id)
        self.assertEqual(flush.call_count, 1)  # title and modified_at differ on disk: not a mismatch
        self.assertEqual([e["title"] for e in self.saved()], ["Demo"])

    async def test_a_write_store_swallows_is_still_not_confirmed(self):
        with mock.patch.object(storage, "write_utf8_file_atomic", side_effect=WriteError("disk full")), \
                mock.patch.object(storage, "write_utf8_file", side_effect=WriteError("disk full")), \
                self.assertLogs("homeassistant.helpers.storage", "ERROR"):
            with self.assertRaisesRegex(ValueError, "could not be confirmed"):
                await ha_import._save_config_entries(self.hass, "demo", self.entry.entry_id)

    async def test_a_removal_is_confirmed_on_disk(self):
        await ha_import._save_config_entries(self.hass, "demo", self.entry.entry_id)
        del self.hass.config_entries._entries[self.entry.entry_id]  # noqa: SLF001
        await ha_import._save_config_entries(self.hass, "demo", self.entry.entry_id, removed=True)
        self.assertEqual(self.saved(), [])
