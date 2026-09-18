"""A restart asked for from the UI used to zero boot_failures, so the crashes of earlier boots were wiped and
a version that never boots could restart-loop without the count ever reaching MAX_BOOT_FAILURES.  Here: the
restart takes back only its own boot's increment, like run.py's stop path, once, and the fallback still fires."""

import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import jsonio
import run
from custom_components.integration_manager import events
from custom_components.integration_manager import installer as inst_mod
from tests.test_r4_lifecycle import A, B, EntrypointMainBase

UNDO_KEY = "hri_undo_boot_failure"  # run.py publishes its stop-path undo here; installer.restart calls it


def _tmp(test):
    tmp = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, tmp, True)
    return tmp


class UiRestartCountingTest(unittest.IsolatedAsyncioTestCase):
    """installer.restart against the same ha.json run.py writes, with run.py's undo published."""

    def setUp(self):
        self.cfg = _tmp(self)
        self.state_dir = os.path.join(self.cfg, "integration_manager")
        os.makedirs(self.state_dir)
        events.EVENTS = events.Events(os.path.join(self.cfg, "events.jsonl"))
        self.addCleanup(setattr, events, "EVENTS", None)
        for patch in (mock.patch.object(run, "CONFIG_DIR", self.cfg), mock.patch.object(run, "_boot_settled", False)):
            patch.start()
            self.addCleanup(patch.stop)

    def write(self, state):
        with open(os.path.join(self.state_dir, "ha.json"), "w", encoding="utf-8") as fh:
            json.dump(state, fh)

    def state(self):
        with open(os.path.join(self.state_dir, "ha.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def installer(self, shared: bool = True):
        data = {UNDO_KEY: run._undo_boot_failure} if shared else {}
        ins = object.__new__(inst_mod.Installer)
        ins.hass = SimpleNamespace(data=data, stopped=[])
        ins.hass.async_stop = self._noop
        ins.hass.async_create_task = lambda coro, *a, **k: coro.close()
        ins.busy = False
        ins.state_dir = self.state_dir
        ins.state_file = os.path.join(self.state_dir, "state.json")
        ins.state = SimpleNamespace(restart_required=True, last_action="")
        ins._save_state = lambda: jsonio.write_json(ins.state_file, {"last_action": ins.state.last_action})
        return ins

    async def _noop(self):
        return None

    async def test_a_restart_during_an_unsettled_boot_keeps_the_earlier_failures(self):
        self.write({"current": B, "boot_failures": 3})  # two crashed boots plus this one
        self.assertEqual(await self.installer().restart(), {"ok": True})
        self.assertEqual(self.state()["boot_failures"], 2, "the crashes before this boot were wiped")

    async def test_the_stop_listener_does_not_take_the_same_increment_back_twice(self):
        self.write({"boot_failures": 3})
        await self.installer().restart()
        run._undo_boot_failure()  # what EVENT_HOMEASSISTANT_STOP does right after
        self.assertEqual(self.state()["boot_failures"], 2)

    async def test_a_restart_after_a_good_boot_changes_nothing(self):
        self.write({"boot_failures": 2, "current": run.HA_VERSION})
        run._mark_boot_ok()
        before = self.state()
        self.assertEqual((before["boot_failures"], before["proven"]), (0, run.HA_VERSION))
        await self.installer().restart()
        self.assertEqual(self.state(), before)

    async def test_without_run_py_the_restart_takes_back_one_increment_too(self):
        self.write({"boot_failures": 3})
        await self.installer(shared=False).restart()  # a test, or HA started some other way
        self.assertEqual(self.state()["boot_failures"], 2)

    async def test_nothing_to_take_back_stays_at_zero(self):
        self.write({"boot_failures": 0, "current": "x"})
        await self.installer(shared=False).restart()
        self.assertEqual(self.state(), {"boot_failures": 0, "current": "x"})

    async def test_a_hand_written_count_is_left_alone(self):
        self.write({"boot_failures": "x"})
        await self.installer(shared=False).restart()
        self.assertEqual(self.state()["boot_failures"], "x")  # entrypoint.py reads anything but a number as 0


class CrashLoopThroughAUiRestartTest(EntrypointMainBase):
    """Two crashes, then a restart from the UI during the third (unsettled) boot: the fallback still fires."""

    def restart_from_the_ui(self):
        hass = SimpleNamespace(data={})
        inst_mod.Installer._undo_boot_failure(SimpleNamespace(hass=hass, state_dir=self.ep.STATE_DIR))

    def test_a_ui_restart_does_not_clear_the_crashes_before_it(self):
        self.write({"current": B, "desired": B, "previous": A,
                    "proven": A, "change": {"to": B, "mode": "keep"}, "boot_failures": 0})
        for _ in range(self.ep.MAX_BOOT_FAILURES):  # every boot of a version that never booted
            self.assertEqual(self.boot(), B)
        self.assertEqual(self.state()["boot_failures"], 3)
        self.restart_from_the_ui()  # the third boot had not settled: only its own increment goes
        self.assertEqual(self.state()["boot_failures"], 2)
        self.assertEqual(self.boot(), B)  # the fourth boot counts the third one's crash again
        self.assertEqual(self.boot(), A, "the fallback never fires while restarts clear the count")
        self.assertEqual(self.state()["fallback_from"], B)


class ProvenVersionStillNeverLeftTest(EntrypointMainBase):
    def test_a_version_that_booted_once_is_kept_although_the_count_now_survives_restarts(self):
        self.write({"current": B, "desired": B, "previous": A,
                    "proven": B, "boot_failures": 2})
        inst_mod.Installer._undo_boot_failure(SimpleNamespace(hass=SimpleNamespace(data={}), state_dir=self.ep.STATE_DIR))
        self.assertEqual(self.state()["boot_failures"], 1)
        for _ in range(self.ep.MAX_BOOT_FAILURES + 1):
            self.assertEqual(self.boot(), B)
        self.assertNotIn("fallback_from", self.state())


if __name__ == "__main__":
    unittest.main()
