"""External review: an import that adds its config entry while a stop is finishing.

The view decides whether the entry is imported enabled from the running integration, then awaits the
whole import.  A stop that finishes in between must not leave an enabled (and set up) entry behind
while the manager reports the integration as stopped.
"""

import asyncio
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from homeassistant.config_entries import ConfigEntryState

from custom_components.integration_manager import ha_import, http_util, import_views
from custom_components.integration_manager.installer import Installer

SUMMARY = {"domains": {"hub": {"storage_files": [], "entries": [{"entry_id": "e1", "title": "Hub", "data": {"host": "h"},
                                                                "options": {}, "version": 1, "minor_version": 1}]}}}


class _Entries:
    """The part of hass.config_entries the import and the stop use."""

    def __init__(self, on_add):
        self.entries = []
        self._on_add = on_add

    def async_entries(self, domain=None):
        return [e for e in self.entries if domain is None or e.domain == domain]

    def async_get_entry(self, entry_id):
        return next((e for e in self.entries if e.entry_id == entry_id), None)

    async def async_add(self, entry):
        await self._on_add()
        object.__setattr__(entry, "state", ConfigEntryState.LOADED)  # HA sets an enabled entry up here; state is frozen
        self.entries.append(entry)


class ImportDuringStopTest(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp(prefix="hri-import-")
        self.addCleanup(shutil.rmtree, self.cfg, ignore_errors=True)
        os.makedirs(os.path.join(self.cfg, "integration_manager"))

    def _run(self):
        at_add, resume = asyncio.Event(), asyncio.Event()

        async def on_add():
            at_add.set()
            await resume.wait()

        async def executor(fn, *args):
            return fn(*args)

        hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg, components={"hub"}),
                               config_entries=_Entries(on_add), async_add_executor_job=executor)
        installer = Installer(hass)
        installer.state.domain = "hub"
        installer.state.installed = {"hub": {"versions": {"v1": {}}, "running_tag": "v1"}}
        view = import_views.ImportApplyView(hass, aligner=None, installer=installer)
        body = {"domain": "hub", "entry_id": "e1", "align": False, "copy_storage": False}

        async def scenario():
            task = asyncio.ensure_future(view.post(mock.Mock()))
            await at_add.wait()
            stopped = await installer.stop()
            resume.set()
            return stopped, await task

        with mock.patch.object(http_util, "_json_object", mock.AsyncMock(return_value=body)), \
                mock.patch.object(view, "json", side_effect=lambda d, **k: d), \
                mock.patch.object(Installer, "dismiss_patch_notification", lambda self, domain: None), \
                mock.patch.object(ha_import, "load_summary", lambda cfg: SUMMARY), \
                mock.patch.object(ha_import, "clear", lambda cfg: None), \
                mock.patch.object(ha_import, "loader", mock.Mock(async_get_integration=mock.AsyncMock(
                    return_value=SimpleNamespace(is_built_in=False)))):
            stopped, result = asyncio.run(scenario())
        return installer, hass, stopped, result

    def test_a_stop_and_an_import_never_end_with_an_enabled_entry_and_a_stopped_manager(self):
        installer, hass, stopped, result = self._run()
        self.assertTrue(result.get("ok"), result)
        entry = hass.config_entries.async_get_entry(result["entry_id"])
        if stopped.get("ok"):
            self.assertIsNotNone(entry.disabled_by, "imported enabled although the integration was stopped meanwhile")
        else:
            self.assertEqual(installer.running, "hub")  # the stop did not go through: the entry's integration runs
        self.assertFalse(installer.busy, "the import kept the manager busy after it finished")

    def test_an_import_started_during_another_action_is_refused(self):
        calls = []

        async def never():
            calls.append("ran")

        installer = SimpleNamespace(busy=True)
        with self.assertRaises(ValueError):
            asyncio.run(import_views._locked(never, installer))
        self.assertEqual(calls, [])
        self.assertTrue(installer.busy)  # left to the action that holds it


if __name__ == "__main__":
    unittest.main()
