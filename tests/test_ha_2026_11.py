"""Home Assistant 2026.11: no <config>/deps any more (async_get_user_site is gone), and the entity registry load
waits for the config entries, so a failed registry gather has to cancel what it started, as bootstrap does."""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from homeassistant import config_entries as ce, core, loader
from homeassistant.util import async_ as ha_async

import run


class MountLocalLibPathTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        self.path = list(sys.path)
        self.addCleanup(setattr, sys, "path", self.path)

    async def test_older_home_assistant_mounts_deps(self):
        lib = os.path.join(self.cfg, "deps", "lib")
        fake = mock.Mock(async_get_user_site=mock.AsyncMock(return_value=lib))
        with mock.patch.dict(sys.modules, {"homeassistant.util.package": fake}):
            self.assertEqual(await run._mount_local_lib_path(self.cfg), os.path.join(self.cfg, "deps"))
        fake.async_get_user_site.assert_awaited_once_with(os.path.join(self.cfg, "deps"))
        self.assertEqual(sys.path[0], lib)

    async def test_without_async_get_user_site_nothing_is_mounted(self):
        fake = mock.Mock(spec=[])  # 2026.11's homeassistant.util.package: no async_get_user_site
        with mock.patch.dict(sys.modules, {"homeassistant.util.package": fake}):
            self.assertEqual(await run._mount_local_lib_path(self.cfg), "")
        self.assertEqual(sys.path, self.path)


class FailedRegistryLoadTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_failed_gather_leaves_no_task_waiting(self):
        hass = core.HomeAssistant(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, hass.config.config_dir, True)
        self.addAsyncCleanup(hass.async_stop, force=True)
        loader.async_setup(hass)
        hass.config_entries = ce.ConfigEntries(hass, {})
        started = []
        real = ha_async.create_eager_task

        def spy(coro, *a, **kw):
            started.append(task := real(coro, *a, **kw))
            return task

        with mock.patch.object(ha_async, "create_eager_task", spy), \
                mock.patch.object(hass.config_entries, "async_initialize", mock.AsyncMock(side_effect=RuntimeError("boom"))), \
                self.assertLogs(run._LOGGER, "ERROR"):
            self.assertFalse(await asyncio.wait_for(run._load_base_functionality(hass), 30))
        await asyncio.sleep(0)
        self.assertTrue(started)
        self.assertEqual([t for t in started if not t.done()], [])  # 2026.11: the entity registry waited for ever


if __name__ == "__main__":
    unittest.main()


class SerializeSchemaTest(unittest.TestCase):
    """2026.10 removed homeassistant.helpers.data_entry_flow.to_field_list (its flow views call probatio's): the
    fallback to voluptuous_serialize then answered "UNSUPPORTED" and every form failed.  Run on the installed HA."""

    def test_a_form_serialises_on_this_home_assistant(self):
        import voluptuous as vol

        from custom_components.integration_manager import flows

        out = flows._serialize_schema(vol.Schema({vol.Required("name"): str, vol.Optional("initial", default=1): int}))
        self.assertIsInstance(out, list)
        self.assertEqual([f["name"] for f in out], ["name", "initial"])

    def test_visible_conditions_reach_the_form(self):
        from homeassistant import data_entry_flow

        if not hasattr(data_entry_flow, "add_visible_conditions_to_serialized_schema"):
            self.skipTest("Home Assistant < 2026.11 has no visible conditions")
        from custom_components.integration_manager import flows

        out = flows._serialize_schema(self._visible_schema())
        self.assertNotIn("visible", out[0])
        self.assertIs(out[1]["visible"], True)

    @staticmethod
    def _visible_schema():
        import voluptuous as vol

        from homeassistant import data_entry_flow

        return vol.Schema({data_entry_flow.Required("mode"): str, data_entry_flow.Optional("level", visible=True): int})
