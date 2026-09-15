"""The MQTT install_integration action runs the start gate on the stored copy before starting it."""

import asyncio
import unittest
from unittest import mock

from custom_components.integration_manager import manager_device as md


def _device():
    device = md.ManagerDevice.__new__(md.ManagerDevice)
    device.hass = mock.Mock()
    inst = mock.Mock()
    inst.running, inst.running_tag, inst.LOCAL_TAG = "demo", "v1.0.0", "local"
    inst.state.installed = {"demo": {"versions": {"v1.0.0": {}}}}
    inst.install = mock.AsyncMock(return_value={"ok": True})
    inst.start = mock.AsyncMock(return_value={"ok": True, "restart_required": True})
    device.installer = inst
    device.integration_latest = lambda: "v2.0.0"
    return device, inst


class InstallIntegrationGateTest(unittest.TestCase):
    def _run(self, gate):
        device, inst = _device()
        with mock.patch.object(md.preflight, "run", mock.AsyncMock(return_value={"ok": True, "blockers": []})), \
                mock.patch.object(md.preflight, "gate", mock.AsyncMock(return_value=gate)) as gate_mock:
            try:
                res = asyncio.run(device._do_install_integration())
                err = None
            except ValueError as exc:
                res, err = None, exc
        return res, err, inst, gate_mock

    def test_blocked_stored_copy_is_not_started(self):
        res, err, inst, gate = self._run({"blocked": True, "report": {"ok": False, "blockers": ["legacy.py:5: bad"]}, "skipped": None})
        self.assertIsNotNone(err)
        self.assertIn("stored demo v2.0.0", str(err))
        self.assertIn("legacy.py:5", str(err))
        inst.install.assert_awaited_once()
        gate.assert_awaited_once()
        self.assertEqual(gate.await_args.args[2:], ("demo", "v2.0.0"))
        inst.start.assert_not_awaited()

    def test_clean_stored_copy_is_started(self):
        res, err, inst, _ = self._run({"blocked": False, "report": {"ok": True, "blockers": []}, "skipped": None})
        self.assertIsNone(err)
        self.assertTrue(res["ok"])
        self.assertTrue(res["restart"])
        inst.start.assert_awaited_once_with("demo", "v2.0.0")


if __name__ == "__main__":
    unittest.main()
