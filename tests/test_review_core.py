"""Review fixes in run.py and the restart path: bounded process exit, boot
accounting (signals, late setup), TZ, excepthooks, restart vs busy, timers."""

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import run
from custom_components.integration_manager import manager_device as md
from custom_components.integration_manager import scheduler as sched_mod
from custom_components.integration_manager import writer as writer_mod
from custom_components.integration_manager.installer import Installer
from tests.fakes import FakeInstaller, FakePublisher, FakeUpdater

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHILD = r"""
import asyncio, json, logging, os, sys, threading, time
logging.basicConfig(level=logging.INFO)
import run
mode = sys.argv[1]
if mode == "stuck":
    block = threading.Event()

    async def boot():
        asyncio.get_running_loop().run_in_executor(None, block.wait)  # blocked in C: SystemExit never reaches it
        await asyncio.sleep(0.1)
        return 3

    rc = run._run_loop(boot)
elif mode == "signal":
    async def boot():
        run._install_boot_signal_handlers(asyncio.current_task(), lambda: None)
        print("ready", flush=True)
        await asyncio.sleep(3600)
        return 5

    rc = run._run_loop(boot)
elif mode == "watchdog":
    run._arm_stop_watchdog(0.5)
    threading.Event().wait()
print("returned", rc, flush=True)
logging.shutdown()
os._exit(rc)
"""


def child(mode, cfg):
    env = {**os.environ, "PYTHONPATH": ROOT, "HRI_CONFIG": cfg, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.Popen([sys.executable, "-c", CHILD, mode], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def write_ha(cfg, data):
    os.makedirs(os.path.join(cfg, "integration_manager"), exist_ok=True)
    with open(os.path.join(cfg, "integration_manager", "ha.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def read_ha(cfg):
    with open(os.path.join(cfg, "integration_manager", "ha.json"), encoding="utf-8") as fh:
        return json.load(fh)


class ProcessExitTest(unittest.TestCase):
    """M2: the process exits even with an executor job that never returns."""

    def test_stuck_executor_job_does_not_block_exit(self):
        t0 = time.monotonic()
        proc = child("stuck", tempfile.mkdtemp())
        try:
            out, err = proc.communicate(timeout=25)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("process hung with a stuck executor job")
        wall = time.monotonic() - t0
        self.assertEqual(proc.returncode, 3, err[-2000:])
        self.assertIn("returned 3", out)
        self.assertLess(wall, 25)
        print(f"\n[M2] stuck executor job: exit {proc.returncode} after {wall:.1f} s", file=sys.stderr)

    def test_watchdog_exits_hard(self):
        proc = child("watchdog", tempfile.mkdtemp())
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("exiting hard", err)

    def test_sigterm_during_boot_exits_0_and_takes_back_the_failure(self):
        cfg = tempfile.mkdtemp()
        write_ha(cfg, {"current": "2026.9.2", "boot_failures": 2})
        proc = child("signal", cfg)
        self.assertEqual(proc.stdout.readline().strip(), "ready")
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, err[-2000:])
        self.assertEqual(read_ha(cfg), {"current": "2026.9.2", "boot_failures": 1})


class BootAccountingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        self._patches = [mock.patch.object(run, "CONFIG_DIR", self.cfg), mock.patch.object(run, "_boot_settled", False)]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_undo_once_and_never_below_zero(self):
        write_ha(self.cfg, {"boot_failures": 1, "current": "x"})
        run._undo_boot_failure()
        run._undo_boot_failure()
        self.assertEqual(read_ha(self.cfg), {"boot_failures": 0, "current": "x"})
        run._boot_settled = False
        run._undo_boot_failure()
        self.assertEqual(read_ha(self.cfg)["boot_failures"], 0)

    def test_no_undo_after_boot_ok(self):
        write_ha(self.cfg, {"boot_failures": 1, "change": {"to": run.HA_VERSION}, "desired": "y"})
        run._mark_boot_ok()
        write_ha(self.cfg, {**read_ha(self.cfg), "boot_failures": 1})  # the next boot's count, say
        run._undo_boot_failure()
        self.assertEqual(read_ha(self.cfg), {"boot_failures": 1, "desired": "y", "proven": run.HA_VERSION})  # proven: HRI-01

    def test_unreadable_ha_json_is_left_alone(self):
        path = os.path.join(self.cfg, "integration_manager", "ha.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{broken")
        run._undo_boot_failure()
        run._mark_boot_ok()
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "{broken")

    async def test_boot_ok_waits_for_setup(self):
        done = asyncio.Event()
        with mock.patch.object(run, "_mark_boot_ok") as mark:
            task = asyncio.create_task(run._mark_boot_ok_after(done, cap=30))
            await asyncio.sleep(0.05)
            mark.assert_not_called()
            done.set()
            await asyncio.wait_for(task, 1)
            mark.assert_called_once()

    async def test_boot_ok_cap(self):
        with mock.patch.object(run, "_mark_boot_ok") as mark, self.assertLogs(run._LOGGER, "WARNING"):
            await asyncio.wait_for(run._mark_boot_ok_after(asyncio.Event(), cap=0.05), 1)
        mark.assert_called_once()


class TimeZoneTest(unittest.TestCase):
    def test_valid_invalid_unset(self):
        with mock.patch.dict(os.environ, {"TZ": "Europe/Athens"}):
            self.assertEqual(run._time_zone(), "Europe/Athens")
        for bad in ("Nope/Nope", "Europe", "../etc/passwd"):
            with mock.patch.dict(os.environ, {"TZ": bad}), self.assertLogs(run._LOGGER, "ERROR"):
                self.assertEqual(run._time_zone(), "UTC")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TZ", None)
            self.assertEqual(run._time_zone(), "UTC")


class ExceptHookTest(unittest.TestCase):
    def setUp(self):
        self.saved = (sys.excepthook, threading.excepthook)

    def tearDown(self):
        sys.excepthook, threading.excepthook = self.saved

    def test_thread_crash_is_logged(self):
        run._install_excepthooks()

        def boom():
            raise RuntimeError("thread boom")

        with self.assertLogs(run._LOGGER, "CRITICAL") as logs:
            t = threading.Thread(target=boom, name="crasher")
            t.start()
            t.join()
            try:
                raise ValueError("main boom")
            except ValueError:
                sys.excepthook(*sys.exc_info())
        text = "\n".join(logs.output)
        self.assertIn("crasher", text)
        self.assertIn("thread boom", text)
        self.assertIn("main boom", text)

    def test_system_exit_in_thread_is_quiet(self):
        run._install_excepthooks()

        def leave():
            raise SystemExit

        with mock.patch.object(run._LOGGER, "critical") as crit:
            t = threading.Thread(target=leave)
            t.start()
            t.join()
        crit.assert_not_called()


class RestartBusyTest(unittest.IsolatedAsyncioTestCase):
    def installer(self):
        inst = Installer(SimpleNamespace(config=SimpleNamespace(config_dir=tempfile.mkdtemp())))
        self.busy_at_first_await, self.stops = [], []

        async def drain(_timeout):  # the only await restart() has left before the stop
            self.busy_at_first_await.append(inst.busy)
            return True

        async def job(fn, *args):
            return fn(*args)

        async def stop():
            return None

        def create_task(coro):
            self.stops.append(coro)
            coro.close()

        patch = mock.patch.object(writer_mod, "async_drain", drain)
        patch.start()
        self.addCleanup(patch.stop)
        inst.hass = SimpleNamespace(data={}, async_add_executor_job=job, async_create_task=create_task, async_stop=stop)
        return inst

    async def test_restart_marks_busy_before_first_await(self):
        inst = self.installer()
        self.assertEqual(await inst.restart(), {"ok": True})
        self.assertEqual(self.busy_at_first_await, [True])
        self.assertEqual(len(self.stops), 1)

    async def test_restart_refused_while_busy(self):
        inst = self.installer()
        inst.busy = True
        res = await inst.restart()
        self.assertFalse(res["ok"])
        self.assertEqual((self.busy_at_first_await, self.stops), ([], []))

    async def _action_with_restart(self, action, result):
        inst = FakeInstaller()
        dev = md.ManagerDevice(SimpleNamespace(), inst, FakeUpdater(), FakePublisher(log=inst.log))

        async def do():
            inst.busy = True  # an install/start clicked meanwhile, still running after the wait
            return dict(result, restart=True)

        setattr(dev, f"_do_{action}", do)

        async def no_wait(_s):
            return None

        with mock.patch.object(md.asyncio, "sleep", no_wait):
            res = await dev.async_action(action)
        return inst, res

    async def test_mqtt_restart_skipped_while_busy(self):
        inst, res = await self._action_with_restart("restart", {"ok": True})
        self.assertNotIn(("restart",), inst.log)
        self.assertFalse(res["ok"])
        self.assertIn("restart skipped", res["error"])
        self.assertEqual(inst.log[-1], ("result", res))

    async def test_action_ok_but_its_restart_skipped(self):
        inst, res = await self._action_with_restart("check_updates", {"ok": True, "note": "found 1.2"})
        self.assertNotIn(("restart",), inst.log)
        self.assertTrue(res["ok"])
        self.assertEqual(res["note"], "found 1.2; restart skipped: an install/start is still running")


class SchedulerTimerTest(unittest.IsolatedAsyncioTestCase):
    async def test_one_busy_retry_pending_and_cancelled_on_shutdown(self):
        armed = []

        def call_later(_hass, delay, job):
            unsub = mock.Mock()
            armed.append((delay, job, unsub))
            return unsub

        listeners = {}
        hass = SimpleNamespace(bus=SimpleNamespace(async_listen_once=lambda ev, cb: listeners.__setitem__(ev, cb)))
        inst = SimpleNamespace(settings=SimpleNamespace(bool_=lambda _k: True, int_=lambda *_a: 3), busy=True, backup_running=False)
        sch = sched_mod.Scheduler(hass, inst)
        daily = mock.Mock()
        with mock.patch.object(sched_mod, "async_call_later", call_later), \
                mock.patch.object(sched_mod, "async_track_time_change", return_value=daily):
            sch.start()
            await sch._daily(None)
            await sch._daily(None)
        self.assertEqual([d for d, _j, _u in armed], [120, 1800, 1800])
        armed[1][2].assert_called_once()  # the first retry was cancelled when the second was armed
        armed[2][2].assert_not_called()
        armed[0][2].assert_not_called()
        from homeassistant.const import EVENT_HOMEASSISTANT_STOP

        listeners[EVENT_HOMEASSISTANT_STOP](None)  # the stop cancels what is still pending: boot check, retry, daily tick
        armed[0][2].assert_called_once()
        armed[2][2].assert_called_once()
        daily.assert_called_once()


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
