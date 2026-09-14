"""Installer: state loading and the patch notification."""

import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager.installer import Installer, State


class StateLoadTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hri-unit-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        os.makedirs(os.path.join(self.dir, "integration_manager"))

    def installer(self, state=None, raw=None):
        path = os.path.join(self.dir, "integration_manager", "state.json")
        if state is not None or raw is not None:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(raw if raw is not None else json.dumps(state))
        return Installer(SimpleNamespace(config=SimpleNamespace(config_dir=self.dir)))

    def test_without_release_updates(self):
        inst = self.installer({"domain": "demo", "installed": {"demo": {"versions": {"1.0.0": {}}, "running_tag": "1.0.0"}}})
        self.assertEqual(inst.updates, {})
        self.assertEqual(inst.state.release_updates, {})
        self.assertEqual(inst.running_tag, "1.0.0")

    def test_release_updates_restored(self):
        inst = self.installer({"domain": "demo", "installed": {}, "release_updates": {"demo": "1.2.0"}, "gone_field": 1})
        self.assertEqual(inst.updates, {"demo": "1.2.0"})
        inst.updates.pop("demo")
        self.assertEqual(inst.state.release_updates, {"demo": "1.2.0"})

    def test_null_release_updates(self):
        self.assertEqual(self.installer({"installed": {}, "release_updates": None}).updates, {})

    def test_no_file_and_bad_files(self):
        self.assertEqual(self.installer().state, State())
        with self.assertLogs("custom_components.integration_manager.installer", "ERROR"):
            self.assertEqual(self.installer({"domain": "demo"}).state, State())
        with self.assertLogs("custom_components.integration_manager.installer", "ERROR"):
            self.assertEqual(self.installer(raw="{broken").state, State())
        self.assertTrue(any(n.startswith("state.json.corrupt-") for n in os.listdir(os.path.join(self.dir, "integration_manager"))))


class NotifyPatchesTest(unittest.TestCase):
    NID = "integration_manager_patches_demo"

    def notify(self, statuses):
        inst = object.__new__(Installer)
        inst.hass = object()
        results = [{"name": f"p{i}.patch", "status": s} for i, s in enumerate(statuses)]
        with mock.patch("homeassistant.components.persistent_notification.create") as create, \
                mock.patch("homeassistant.components.persistent_notification.dismiss") as dismiss:
            inst._notify_patches("demo", results)
        return inst.hass, create, dismiss

    def test_fine_statuses_dismiss(self):
        for statuses in (["applied", "Already Applied", "skipped", "pending", " applied "], []):
            hass, create, dismiss = self.notify(statuses)
            create.assert_not_called()
            dismiss.assert_called_once_with(hass, self.NID)

    def test_bad_statuses_notify(self):
        for bad in ("not applicable (context changed in mod.py)", "absent (b/x.py not found)", "failed: RuntimeError: boom", "error: x"):
            hass, create, dismiss = self.notify(["applied", bad, "skipped"])
            dismiss.assert_not_called()
            create.assert_called_once()
            args, kwargs = create.call_args
            self.assertIs(args[0], hass)
            self.assertIn(f"p1.patch: {bad}", args[1])
            self.assertNotIn("p0.patch", args[1])
            self.assertEqual(kwargs["notification_id"], self.NID)


if __name__ == "__main__":
    unittest.main()
