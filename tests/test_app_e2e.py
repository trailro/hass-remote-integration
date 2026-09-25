"""The end-to-end test of the app on Home Assistant OS 18.3 / Supervisor 2026.09.2.

F2  with debug on, Home Assistant's blocking-call detector reported HRI's own boot: the sweep of deploy leftovers
    (os.listdir), the copy of the manager component and the read of state.json, all on the event loop; then, from
    the same run: the check for the http config module in _http_config (it imports homeassistant.components.http,
    which reads package metadata and loads the CA bundle), the reconcile's check of the running integration's
    requirements (importlib.metadata) and the import of auth.py, which reads templates/login.html
"""

import asyncio
import builtins
import glob
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import run
from custom_components.integration_manager import installer as inst_mod


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
        self.http_config_on_loop, self.setup_component = [], mock.AsyncMock(return_value=True)

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

        def http_config():
            self.http_config_on_loop.append(threading.get_ident() in loop_threads)
            return {"http": {"server_port": run.HTTP_PORT}}

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
            mock.patch.object(run, "_http_config", http_config),
            mock.patch.object(run, "_load_base_functionality", mock.AsyncMock(return_value=True)),
            mock.patch.object(run, "async_setup_component", self.setup_component),
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

    def test_the_http_section_is_worked_out_off_the_loop(self):
        self._boot()
        self.assertEqual(self.http_config_on_loop, [False])  # once, in a thread: find_spec imports homeassistant.components.http
        self.assertEqual(self.setup_component.await_args_list[0].args[2]["http"], {"server_port": run.HTTP_PORT})  # what it returned still goes in

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


class ReconcileRequirementsOffTheLoopTest(unittest.TestCase):
    """The boot reconcile asks importlib.metadata whether each requirement of the running integration is
    installed (listdir, read_text, open): in a thread, not on the loop."""

    def test_the_requirements_are_checked_off_the_loop(self):
        cfg = tempfile.mkdtemp(prefix="hri-reconcile-")
        self.addCleanup(shutil.rmtree, cfg, True)
        os.makedirs(os.path.join(cfg, "integration_manager"))
        with open(os.path.join(cfg, "integration_manager", "state.json"), "w", encoding="utf-8") as fh:
            json.dump({"domain": "hub", "installed": {"hub": {"versions": {"v1": {}}, "running_tag": "v1"}}}, fh)
        loop_threads, checked = set(), []

        async def executor(fn, *args):
            return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

        async def nothing(*args, **kwargs):
            return []

        async def requirements(domain):
            return ["foo==1", "bar>=2"]

        def is_installed(req):
            checked.append((req, threading.get_ident() in loop_threads))
            return True

        inst = inst_mod.Installer(SimpleNamespace(config=SimpleNamespace(config_dir=cfg, components=set()),
                                                  async_add_executor_job=executor))
        inst._ensure_deployed = lambda domain, tag, force=False: False
        inst._requirements_for = requirements
        inst._install_requirements = mock.Mock(side_effect=AssertionError("nothing is missing"))
        inst._enable_entries = nothing
        inst._patch_rows = lambda domain: []
        inst._notify_patches = lambda domain, rows: None
        inst._tree_hash = lambda domain: None

        async def reconcile():
            loop_threads.add(threading.get_ident())
            await inst.async_reconcile()

        with mock.patch.object(inst_mod.pkg_util, "is_installed", is_installed):
            asyncio.run(reconcile())
        self.assertEqual(checked, [("foo==1", False), ("bar>=2", False)])


class AuthImportOffTheLoopTest(unittest.TestCase):
    """async_setup imports auth.py, whose import reads templates/login.html: in a thread, not on the loop."""

    NAME = "custom_components.integration_manager.auth"

    def test_login_html_is_read_off_the_loop(self):
        import custom_components.integration_manager as im
        from custom_components.integration_manager import events, hostguard, ui

        old = sys.modules.pop(self.NAME, None)  # a fresh import, as at boot
        old_attr = im.__dict__.pop("auth", None)

        def restore():
            sys.modules.pop(self.NAME, None)
            im.__dict__.pop("auth", None)
            if old is not None:
                sys.modules[self.NAME] = old
            if old_attr is not None:
                im.auth = old_attr
        self.addCleanup(restore)

        loop_threads, opened = set(), []
        real_open = builtins.open

        def spy_open(file, *args, **kwargs):
            if str(file).endswith(os.path.join("templates", "login.html")):
                opened.append(threading.get_ident() in loop_threads)
            return real_open(file, *args, **kwargs)

        class Stop(Exception):
            pass

        async def executor(fn, *args):
            if getattr(fn, "__name__", "") == "_configured_password":
                raise Stop  # async_setup_auth's first step: the import is done by then
            return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

        hass = mock.MagicMock()
        hass.async_add_executor_job = executor

        async def setup():
            loop_threads.add(threading.get_ident())
            await im.async_setup(hass, {})

        with mock.patch.object(im, "Installer", lambda _hass: mock.MagicMock()), \
                mock.patch.object(im, "track_delayed_stores", lambda: None), \
                mock.patch.object(im.writer, "async_register", lambda _hass: None), \
                mock.patch.object(events, "Events", mock.MagicMock()), mock.patch.object(events, "EVENTS", None), \
                mock.patch.object(hostguard, "install_host_guard", lambda *a: None), \
                mock.patch.object(builtins, "open", spy_open), self.assertRaises(Stop):
            asyncio.run(setup())
        self.assertEqual(opened, [False])
        with open(os.path.join(ui.TEMPLATE_DIR, "login.html"), encoding="utf-8") as fh:
            self.assertEqual(sys.modules[self.NAME].LOGIN_HTML, fh.read())  # the page serves the template unchanged


if __name__ == "__main__":
    unittest.main()
