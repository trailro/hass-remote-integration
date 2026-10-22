"""The check before a Home Assistant switch resolves the running integration's requirements (and the requirements
of the components it depends on) against the target version's pins, as the integration preflight does for a new
integration version.

ramses_cc 0.61.1 requires serialx>=1.11.0: a switch to a Home Assistant that pins serialx==1.10.0 cannot install it
(needs_force on the API and the System page, refused over MQTT); one that pins 1.11.0 can."""

import json
import os
import tempfile
import shutil
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import installer as inst_mod, preflight, views
from tests.test_ha_preflight_views import CLEAN, TARGET, _GateBase
from tests.test_preflight_target_pins import OFFLINE, _ha_files, _resolve

MANIFEST = {"domain": "ramses_cc", "version": "0.61.1", "requirements": ["serialx>=1.11.0"], "dependencies": ["usb"]}


def _tree(test, files):
    root = tempfile.mkdtemp(prefix="hri-switchreq-")
    test.addCleanup(shutil.rmtree, root, ignore_errors=True)
    for rel, text in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return root


class SwitchRequirementsTest(_GateBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        for cache in (preflight._HA_PINS, preflight._SWITCH_REPORTS, preflight._HA_MODULE_NAMES):
            cache.clear()
            self.addCleanup(cache.clear)
        comp = os.path.join(self.cfg, "custom_components", "ramses_cc")
        os.makedirs(comp)
        with open(os.path.join(comp, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(MANIFEST, fh)
        self.installer.running, self.installer.running_tag = "ramses_cc", "0.61.1"
        self.installer.config_dir = self.cfg
        self.installer.component_dir = lambda d: os.path.join(self.cfg, "custom_components", d)
        self.installer._entries_of = lambda d: []
        self.hass.config.components = set()
        self.pins = "1.11.0"
        self.where = mock.Mock(side_effect=lambda *a: (_tree(self, _ha_files(self.pins)), None, ""))
        self.pip = mock.Mock(side_effect=_resolve)
        usb = SimpleNamespace(requirements=["aiousbwatcher==1.1.2", "serialx==1.11.0"], dependencies=[], after_dependencies=[],
                              is_built_in=True)  # the running version's usb: never the one judged
        for p in (mock.patch.object(preflight, "_target_ha_root", self.where),
                  mock.patch.object(preflight, "_pip_dry_run", self.pip),
                  mock.patch.object(inst_mod.loader, "async_get_integration", mock.AsyncMock(return_value=usb))):
            p.start()
            self.addCleanup(p.stop)
        self._view(CLEAN)

    async def test_a_target_pinning_an_older_version_needs_force(self):
        self.pins = "1.10.0"
        r = await self._post({"version": TARGET})
        self.assertFalse(r["ok"])
        self.assertTrue(r["needs_force"])
        self.assertIn("the requirements of ramses_cc 0.61.1 cannot be resolved against Home Assistant", r["error"])
        self.assertIn("serialx", r["error"])
        self.installer.async_backup.assert_not_awaited()
        reqs = self.pip.call_args.args[1]
        self.assertIn("serialx==1.10.0", reqs, "the usb component's requirements are the target's")
        r = await self._post({"version": TARGET, "force": True})
        self.assertTrue(r["ok"], r)

    async def test_check_shows_it(self):
        self.pins = "1.10.0"
        r = await self._post({"version": TARGET}, "check")
        self.assertFalse(r["check"]["ok"])
        self.assertTrue(any("serialx" in b for b in r["check"]["blockers"]), r["check"])

    async def test_a_target_pinning_a_version_that_fits_goes_through(self):
        r = await self._post({"version": TARGET})
        self.assertTrue(r["ok"], r)

    async def test_the_answer_is_cached(self):
        for _ in range(2):
            r = await self._post({"version": TARGET}, "check")
            self.assertTrue(r["check"]["ok"], r["check"])
        self.assertEqual(self.pip.call_count, 1)

    async def test_target_pins_unreadable_warns_and_goes_through(self):
        self.where.side_effect = lambda *a: (None, None, "PyPI could not be reached")
        r = await self._post({"version": TARGET}, "check")
        self.assertTrue(r["check"]["ok"], r["check"])
        self.assertTrue(any("requirements of ramses_cc" in w and "PyPI could not be reached" in w for w in r["check"]["warnings"]))
        self.pip.assert_not_called()

    async def test_pypi_unreachable_warns_goes_through_and_is_asked_again(self):
        self.pip.side_effect = lambda *a: {"ok": False, "install": [], "stderr": OFFLINE, "stderr_full": OFFLINE}
        for _ in range(2):
            r = await self._post({"version": TARGET}, "check")
            self.assertTrue(r["check"]["ok"], r["check"])
            self.assertTrue(any("PyPI could not be reached" in w for w in r["check"]["warnings"]), r["check"])
        self.assertEqual(self.pip.call_count, 2, "a could-not-check answer is not cached")

    async def test_a_stopped_integration_is_not_checked(self):
        self.pins = "1.10.0"
        self.installer.running, self.installer.running_tag = None, None
        r = await self._post({"version": TARGET}, "check")
        self.assertTrue(r["check"]["ok"], r["check"])
        self.pip.assert_not_called()
        self.where.assert_not_called()

    async def test_mqtt_refuses_with_no_force(self):
        from custom_components.integration_manager import manager_device

        self.pins = "1.10.0"
        self.up.available = mock.AsyncMock(return_value={"latest_stable": "2099.1.0"})
        dev = manager_device.ManagerDevice.__new__(manager_device.ManagerDevice)
        dev.hass, dev.installer, dev.updater = self.hass, self.installer, self.up
        change = mock.AsyncMock()
        with mock.patch.object(views, "async_change_ha_version", change):
            with self.assertRaisesRegex(ValueError, "serialx"):
                await dev._do_install_home_assistant()
        change.assert_not_awaited()
