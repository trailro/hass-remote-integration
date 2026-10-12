"""Where the Home Assistant preflight hooks in: POST /api/ha/update refuses before it schedules anything,
``force`` goes through, POST /api/ha/check answers for the System page, and the MQTT install action refuses too."""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import backupkit
from custom_components.integration_manager import views
from custom_components.integration_manager.ha_updater import HaUpdater

TARGET = "2026.1.0"
BLOCKED = {"version": TARGET, "ok": False, "checked": True, "missing": ["lru-dict==1.3.0"], "warnings": [], "notes": [],
           "blockers": [f"Home Assistant {TARGET} needs lru-dict==1.3.0, with no wheel for Python 3.14.7 on aarch64; "
                        "this image has no compiler to build it"]}
CLEAN = {"version": TARGET, "ok": True, "checked": True, "missing": [], "blockers": [], "warnings": [], "notes": ["all 48 pinned"]}
UNCHECKED = {"version": TARGET, "ok": True, "checked": False, "missing": [], "blockers": [], "warnings": [],
             "notes": [f"could not check Home Assistant {TARGET}: error: resolution-too-deep"]}


def _request(body):
    async def body_json():
        return body

    return SimpleNamespace(content_type="application/json", json=body_json)


class _GateBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        os.makedirs(os.path.join(self.cfg, "integration_manager"))
        with open(os.path.join(self.cfg, "integration_manager", "ha.json"), "w", encoding="utf-8") as fh:
            json.dump({"current": views.HA_VERSION, "previous": TARGET}, fh)
        loop = asyncio.get_running_loop()
        self.hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg, path=lambda *p: os.path.join(self.cfg, *p)),
                                    async_add_executor_job=lambda f, *a: loop.run_in_executor(None, f, *a))
        self.up = HaUpdater(self.hass)
        self.up.validate = mock.AsyncMock()
        self.installer = SimpleNamespace(hass=self.hass, busy=False, running=None, running_tag=None,
                                         min_ha_of=lambda *a: None, async_backup=mock.AsyncMock(return_value={"name": "b.zip"}),
                                         settings=SimpleNamespace(backup_keep=5), protected_backups=set)
        self.view = views.HaActionView(self.up, self.installer)
        self.view.json = lambda d, *a, **k: d  # json_message passes status_code and headers through json()

    def _view(self, check):
        self.up.dependency_check = mock.AsyncMock(return_value=check)
        return self.view

    async def _post(self, body, action="update"):
        with mock.patch.object(backupkit, "prune", return_value=[]):
            return await self.view.post(_request(body), action)


class HaUpdateGateTest(_GateBase):
    async def test_a_blocked_version_is_refused_with_the_package_and_needs_force(self):
        self._view(BLOCKED)
        r = await self._post({"version": TARGET})
        self.assertFalse(r["ok"])
        self.assertTrue(r["needs_force"])
        self.assertIn("lru-dict==1.3.0", r["error"])
        self.assertEqual(r["check"], BLOCKED)

    async def test_the_refusal_schedules_nothing(self):
        self._view(BLOCKED)
        await self._post({"version": TARGET})
        self.installer.async_backup.assert_not_awaited()  # no backup, so nothing was prepared
        self.assertIsNone(self.up._read().get("desired"))

    async def test_force_goes_through(self):
        view = self._view(BLOCKED)
        r = await self._post({"version": TARGET, "force": True})
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.up._read().get("desired"), TARGET)
        view.updater.dependency_check.assert_not_awaited()  # not even asked: the operator already answered

    async def test_a_clean_version_goes_through_without_force(self):
        self._view(CLEAN)
        r = await self._post({"version": TARGET})
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.up._read().get("desired"), TARGET)

    async def test_could_not_check_does_not_refuse(self):
        self._view(UNCHECKED)
        r = await self._post({"version": TARGET})
        self.assertTrue(r["ok"], r)

    async def test_the_requires_python_refusal_still_comes_first(self):
        self.up.validate = mock.AsyncMock(side_effect=ValueError("Home Assistant 2026.1.0 needs Python >=3.15; this image has 3.14.7"))
        self._view(BLOCKED)
        r = await self._post({"version": TARGET})
        self.assertFalse(r["ok"])
        self.assertIn("needs Python", r["error"])
        self.assertNotIn("needs_force", r)  # rebuilding the image is not something force can do
        self.up.dependency_check.assert_not_awaited()

    async def test_cancelling_a_scheduled_change_is_not_gated(self):
        self.up.set_desired(TARGET)
        self._view(BLOCKED)
        r = await self._post({"version": views.HA_VERSION})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["cancelled"], TARGET)
        self.up.dependency_check.assert_not_awaited()


class HaCheckActionTest(HaUpdateGateTest):
    async def test_check_answers_the_report_and_schedules_nothing(self):
        self._view(BLOCKED)
        r = await self._post({"version": TARGET}, "check")
        self.assertTrue(r["ok"])
        self.assertEqual(r["check"], BLOCKED)
        self.installer.async_backup.assert_not_awaited()

    async def test_check_reports_a_requires_python_refusal_instead_of_raising(self):
        self.up.validate = mock.AsyncMock(side_effect=ValueError("Home Assistant 2026.1.0 needs Python >=3.15; this image has 3.14.7"))
        self._view(CLEAN)
        r = await self._post({"version": TARGET}, "check")
        self.assertTrue(r["ok"])
        self.assertFalse(r["check"]["ok"])
        self.assertIn("needs Python", r["check"]["blockers"][0])

    async def test_check_of_the_running_version_needs_no_pip(self):
        self._view(BLOCKED)
        r = await self._post({"version": views.HA_VERSION}, "check")
        self.assertTrue(r["check"]["ok"])
        self.assertFalse(r["check"]["checked"])
        self.up.dependency_check.assert_not_awaited()

    async def test_an_unknown_action_is_still_refused(self):
        r = await self.view.post(_request({}), "explode")
        self.assertEqual(r["message"], "unknown action")


class HaSwitchIntegrationTest(_GateBase):
    """The running integration is checked against the target before a switch (ramses_cc 0.60.4 reads cv.vol, which
    Home Assistant 2026.10.0 no longer has), and a version the smoke test left is not switched to again unasked."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        from custom_components.integration_manager import preflight
        from tests.test_preflight_ha_attributes import TARGET_TREE, _tree

        running = dict(TARGET_TREE)
        running["homeassistant/helpers/config_validation.py"] = "import voluptuous as vol\n" + TARGET_TREE["homeassistant/helpers/config_validation.py"]
        running_root, target_root = _tree(self, running), _tree(self, TARGET_TREE)
        for p in (mock.patch.object(preflight, "_running_ha_root", return_value=running_root),
                  mock.patch.object(preflight, "_target_ha_root", return_value=(target_root, None, ""))):
            p.start()
            self.addCleanup(p.stop)
        preflight._HA_MODULE_NAMES.clear()
        self.addCleanup(preflight._HA_MODULE_NAMES.clear)
        comp = os.path.join(self.cfg, "custom_components", "ramses_cc")
        os.makedirs(comp)
        with open(os.path.join(comp, "ha_compat.py"), "w", encoding="utf-8") as fh:
            fh.write("from homeassistant.helpers import config_validation as cv\nif cv.vol is not None:\n    pass\n")
        self.installer.running, self.installer.running_tag = "ramses_cc", "0.60.4"
        self.installer.config_dir = self.cfg
        self.installer.component_dir = lambda d: os.path.join(self.cfg, "custom_components", d)
        self.installer._entries_of = lambda d: []
        self.hass.config.components = set()

    async def test_the_running_integration_that_reads_a_removed_name_needs_force(self):
        self._view(CLEAN)
        r = await self._post({"version": TARGET})
        self.assertFalse(r["ok"])
        self.assertTrue(r["needs_force"])
        self.assertIn("ramses_cc 0.60.4 uses names Home Assistant", r["error"])
        self.assertIn("ha_compat.py:2 uses homeassistant.helpers.config_validation.vol", r["error"])
        self.installer.async_backup.assert_not_awaited()
        self.assertIsNone(self.up._read().get("desired"))
        r = await self._post({"version": TARGET, "force": True})
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.up._read().get("desired"), TARGET)

    async def test_check_shows_it_before_the_switch(self):
        self._view(CLEAN)
        r = await self._post({"version": TARGET}, "check")
        self.assertFalse(r["check"]["ok"])
        self.assertTrue(any("config_validation.vol" in b for b in r["check"]["blockers"]), r["check"])

    async def test_an_integration_the_target_can_run_goes_through(self):
        with open(os.path.join(self.cfg, "custom_components", "ramses_cc", "ha_compat.py"), "w", encoding="utf-8") as fh:
            fh.write("from homeassistant.helpers import config_validation as cv\nS = cv.string\n")
        self._view(CLEAN)
        r = await self._post({"version": TARGET})
        self.assertTrue(r["ok"], r)

    async def test_a_target_that_cannot_be_read_warns_and_goes_through(self):
        from custom_components.integration_manager import preflight

        self._view(CLEAN)
        with mock.patch.object(preflight, "_target_ha_root", return_value=(None, None, "PyPI could not be reached")):
            r = await self._post({"version": TARGET}, "check")
        self.assertTrue(r["check"]["ok"], r["check"])
        self.assertTrue(any("PyPI could not be reached" in w for w in r["check"]["warnings"]))

    async def test_a_version_the_smoke_test_left_needs_force(self):
        with open(os.path.join(self.cfg, "custom_components", "ramses_cc", "ha_compat.py"), "w", encoding="utf-8") as fh:
            fh.write("X = 1\n")
        with open(os.path.join(self.cfg, "integration_manager", "ha.json"), "w", encoding="utf-8") as fh:
            json.dump({"current": views.HA_VERSION, "smoke_fallback": {"from": TARGET, "to": views.HA_VERSION, "domain": "ramses_cc",
                                                                      "reason": "config entry 'x' is setup_error", "at": "2026-10-12T10:00:00"}}, fh)
        self._view(CLEAN)
        r = await self._post({"version": TARGET})
        self.assertTrue(r["needs_force"], r)
        self.assertIn(f"Home Assistant {TARGET} was left automatically", r["error"])
        r = await self._post({"version": TARGET, "force": True})
        self.assertTrue(r["ok"], r)

    async def test_the_left_version_is_not_offered_as_an_update(self):
        newer = "2099.1.0"
        with open(os.path.join(self.cfg, "integration_manager", "ha.json"), "w", encoding="utf-8") as fh:
            json.dump({"current": views.HA_VERSION, "smoke_fallback": {"from": newer, "to": views.HA_VERSION}}, fh)
        self.up.available = mock.AsyncMock(return_value={"latest_stable": newer, "recent": [newer]})
        self.up.config_backups = lambda *a, **k: {}
        self.up.verdicts = lambda *a, **k: {}
        with mock.patch.object(backupkit, "list_backups", return_value=[]):
            status = await self.up.status()
        self.assertFalse(status["update_available"])
        with open(os.path.join(self.cfg, "integration_manager", "ha.json"), "w", encoding="utf-8") as fh:
            json.dump({"current": views.HA_VERSION}, fh)
        with mock.patch.object(backupkit, "list_backups", return_value=[]):
            self.assertTrue((await self.up.status())["update_available"])


class MqttInstallTest(unittest.IsolatedAsyncioTestCase):
    """The manager device's "install Home Assistant" button takes the same refusal (no force on a device)."""

    def _device(self, check):
        from custom_components.integration_manager import manager_device

        dev = manager_device.ManagerDevice.__new__(manager_device.ManagerDevice)
        dev.hass = None
        dev.installer = SimpleNamespace()
        dev.updater = SimpleNamespace(available=mock.AsyncMock(return_value={"latest_stable": "2099.1.0"}),
                                      validate=mock.AsyncMock(), dependency_check=mock.AsyncMock(return_value=check),
                                      switch_blockers=mock.AsyncMock(return_value=([], [])))
        return dev

    async def test_a_version_the_integration_cannot_run_on_or_that_was_left_is_not_scheduled(self):
        dev = self._device(CLEAN)
        dev.updater.switch_blockers = mock.AsyncMock(return_value=(["Home Assistant 2099.1.0 was left automatically on t: demo did not set up on it (x)"], []))
        change = mock.AsyncMock()
        with mock.patch.object(views, "async_change_ha_version", change):
            with self.assertRaisesRegex(ValueError, "left automatically"):
                await dev._do_install_home_assistant()
        change.assert_not_awaited()

    async def test_a_blocked_version_is_not_scheduled(self):
        dev = self._device({**BLOCKED, "version": "2099.1.0"})
        change = mock.AsyncMock()
        with mock.patch.object(views, "async_change_ha_version", change):
            with self.assertRaises(ValueError) as ctx:
                await dev._do_install_home_assistant()
        self.assertIn("lru-dict==1.3.0", str(ctx.exception))
        change.assert_not_awaited()

    async def test_a_clean_version_is_scheduled(self):
        dev = self._device(CLEAN)
        change = mock.AsyncMock(return_value={"backup": "b.zip"})
        with mock.patch.object(views, "async_change_ha_version", change):
            res = await dev._do_install_home_assistant()
        self.assertTrue(res["ok"])
        change.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
