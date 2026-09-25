"""The end-to-end test of the app on Home Assistant OS 18.3 / Supervisor 2026.09.2.

F2  with debug on, Home Assistant's blocking-call detector reported HRI's own boot: the sweep of deploy leftovers
    (os.listdir), the copy of the manager component and the read of state.json, all on the event loop
"""

import asyncio
import builtins
import glob
import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

import run


class BootFileIoOffTheLoopTest(unittest.TestCase):
    """F2: _boot, run up to the port check with Home Assistant itself replaced, opens, lists or copies nothing on
    the thread that runs the event loop - what block_async_io watches (open, os.listdir, os.scandir, os.walk, glob)."""

    WATCHED = ((builtins, "open"), (os, "listdir"), (os, "scandir"), (os, "walk"), (glob, "glob"), (glob, "iglob"))

    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        self.src = os.path.join(tempfile.mkdtemp(), "integration_manager")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.src), True)
        os.makedirs(self.src)
        with open(os.path.join(self.src, "manifest.json"), "w", encoding="utf-8") as fh:
            fh.write('{"domain": "integration_manager", "version": "new"}')
        cc = os.path.join(self.cfg, "custom_components")
        os.makedirs(os.path.join(cc, "foo.deploying"))  # a deploy killed half-way: the sweep removes it
        os.makedirs(os.path.join(cc, "integration_manager.replaced"))  # killed between the two renames: put back
        with open(os.path.join(cc, "integration_manager.replaced", "manifest.json"), "w", encoding="utf-8") as fh:
            fh.write('{"version": "old"}')
        os.makedirs(os.path.join(self.cfg, "integration_manager"))
        with open(os.path.join(self.cfg, "integration_manager", "state.json"), "w", encoding="utf-8") as fh:
            json.dump({"domain": "foo"}, fh)
        self.addCleanup(os.chdir, os.getcwd())  # _boot changes into the config dir

    def _boot(self):
        loop_threads, on_loop, running_domain = set(), [], []

        def watched(name, original):
            def call(*args, **kwargs):
                if threading.get_ident() in loop_threads:
                    on_loop.append((name, args[0] if args else None))
                return original(*args, **kwargs)
            return call

        hass = mock.MagicMock()
        hass.data = {run.loader.DATA_PRELOAD_PLATFORMS: []}
        hass.config.api.port = run.HTTP_PORT + 1  # the boot ends at the port check, before anything starts

        async def executor(fn, *args):
            return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

        hass.async_add_executor_job = executor
        real_running_domain = run._running_domain

        def running_domain_spy():
            running_domain.append(real_running_domain())
            return running_domain[-1]

        async def boot():
            loop_threads.add(threading.get_ident())
            return await run._boot()

        patches = [
            mock.patch.object(run, "CONFIG_DIR", self.cfg),
            mock.patch.object(run, "MANAGER_SRC", self.src),
            mock.patch.object(run, "_install_boot_signal_handlers", lambda *a: None),
            mock.patch.object(run.core, "HomeAssistant", lambda _cfg: hass),
            mock.patch.object(run.loader, "async_setup", lambda _hass: None),
            mock.patch.object(run.loader, "async_get_custom_components", mock.AsyncMock(return_value={})),
            mock.patch.object(run.conf_util, "async_ensure_config_exists", mock.AsyncMock(return_value=True)),
            mock.patch.object(run.conf_util, "process_ha_config_upgrade", lambda _hass: None),
            mock.patch.object(run.config_entries, "ConfigEntries", mock.MagicMock()),
            mock.patch.object(run, "_mount_local_lib_path", mock.AsyncMock(return_value="")),
            mock.patch.object(run, "_http_config", lambda: {}),
            mock.patch.object(run, "_load_base_functionality", mock.AsyncMock(return_value=True)),
            mock.patch.object(run, "async_setup_component", mock.AsyncMock(return_value=True)),
            mock.patch.object(run, "async_process_ha_core_config", mock.AsyncMock()),
            mock.patch.object(run, "_yaml_config_for", lambda _hass, _domain: None),
            mock.patch.object(run, "drop_foreign_http_port", lambda *a: None),
            mock.patch.object(run, "_running_domain", running_domain_spy),
        ] + [mock.patch.object(owner, name, watched(name, getattr(owner, name))) for owner, name in self.WATCHED]
        for patch in patches:
            patch.start()
        try:
            with self.assertLogs(run._LOGGER, "WARNING"):  # the sweep reports what it cleaned
                rc = asyncio.run(boot())
        finally:
            for patch in reversed(patches):
                patch.stop()
        return rc, on_loop, running_domain

    def test_no_file_io_on_the_loop_thread(self):
        rc, on_loop, running_domain = self._boot()
        self.assertEqual(rc, 1)  # the port check, the end the test set up
        self.assertEqual(on_loop, [])
        self.assertEqual(running_domain, ["foo", "foo"])  # state.json was still read, twice as before

    def test_the_sweep_and_the_copy_still_run_in_order(self):
        self._boot()
        cc = os.path.join(self.cfg, "custom_components")
        self.assertEqual(sorted(os.listdir(cc)), ["integration_manager"])
        with open(os.path.join(cc, "integration_manager", "manifest.json"), encoding="utf-8") as fh:
            self.assertIn('"new"', fh.read())  # the sweep put the set-aside copy back, then the image's copy replaced it
        self.assertEqual(os.path.realpath(os.getcwd()), os.path.realpath(self.cfg))

    def test_a_copy_that_cannot_be_made_still_fails_the_boot(self):
        shutil.rmtree(self.src)
        shutil.rmtree(os.path.join(self.cfg, "custom_components", "integration_manager.replaced"))
        with self.assertRaises(OSError):
            self._boot()


if __name__ == "__main__":
    unittest.main()
