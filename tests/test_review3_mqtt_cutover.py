"""Third external review, M16: Cutover "Enable discovery" went ahead while an import ran, a restore, rollback or
clean start was scheduled for the next restart, or a smoke test was pending.  That restart replaces .storage (the
entity ids behind the unique ids just announced) or mqtt.json: the main Home Assistant keeps entities nobody
publishes and loses what was customised on them.  The health watchdog refuses to restart in the same states."""

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import import_views, parity, views
from tests.test_view_handlers import FakePublisher, _call


def _view(tmp, **installer):
    hass = mock.Mock()
    hass.async_add_executor_job = mock.AsyncMock(side_effect=lambda f, *a: f(*a))
    os.makedirs(os.path.join(tmp, "integration_manager"), exist_ok=True)
    inst = mock.Mock(running="ramses_cc", running_tag="0.60.4", busy=False, smoke={"pending": None},
                     state=SimpleNamespace(pending_smoke=None), config_dir=tmp,
                     state_dir=os.path.join(tmp, "integration_manager"))
    inst.rollback_restore_refusal.return_value = None
    inst.settings.data = {}
    for k, v in installer.items():
        setattr(inst, k, v)
    view = parity.CutoverView(hass, inst, FakePublisher())
    view.publisher.config.discovery_enabled = False
    return view


class CutoverRefusesWhatTheRestartReplacesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def _refused(self, view, body=None, why=""):
        res = _call(view, "enable", body)
        self.assertFalse(res["ok"], res)
        self.assertIn(why, res["error"])
        view.publisher.async_save.assert_not_awaited()
        view.publisher.async_republish_all.assert_not_awaited()
        return res

    def test_nothing_pending_enables(self):
        res = _call(_view(self.tmp), "enable")
        self.assertTrue(res["ok"], res)
        self.assertFalse(res["smoke_skipped"])

    def test_an_action_running_refuses(self):
        self._refused(_view(self.tmp, busy=True), why="another action is running")

    def test_an_import_running_refuses(self):
        async def run():
            async with import_views._IMPORT_LOCK:
                return await asyncio.to_thread(_call, _view(self.tmp), "enable")
        res = asyncio.run(run())
        self.assertFalse(res["ok"])
        self.assertIn("import", res["error"])

    def test_a_version_change_being_prepared_refuses(self):
        async def run():
            async with views._HA_CHANGE_LOCK:
                return await asyncio.to_thread(_call, _view(self.tmp), "enable")
        res = asyncio.run(run())
        self.assertFalse(res["ok"])
        self.assertIn("being prepared", res["error"])

    def test_a_scheduled_restore_refuses_even_forced(self):
        with mock.patch("backupkit.pending", return_value=True):
            self._refused(_view(self.tmp), {"force": True}, why="a restore is scheduled for the next restart")

    def test_a_scheduled_clean_start_refuses(self):
        view = _view(self.tmp)
        with open(os.path.join(self.tmp, "integration_manager", "ha.json"), "w") as fh:
            json.dump({"change": {"to": "2099.1.0", "mode": "rebuild"}}, fh)
        self._refused(view, {"force": True}, why="2099.1.0 is scheduled")

    def test_a_pending_smoke_test_refuses_unless_forced(self):
        for pending in ({"smoke": {"pending": {"domain": "ramses_cc"}}},
                        {"state": SimpleNamespace(pending_smoke={"domain": "ramses_cc"})}):
            with self.subTest(pending=pending):
                self._refused(_view(self.tmp, **pending), why="smoke test is pending")
                res = _call(_view(self.tmp, **pending), "enable", {"force": True})
                self.assertTrue(res["ok"], res)
                self.assertTrue(res["smoke_skipped"])
                self.assertFalse(res["forced"], "no main Home Assistant configured: no check of it was skipped")
                self.assertFalse(res["checked"])


if __name__ == "__main__":
    unittest.main()
