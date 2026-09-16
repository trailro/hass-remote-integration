"""Review round 3, core: restart failure path (16), timers cancelled at stop (17), EMFILE stop (19),
boot-failure count right before exec (20), bounded log queue (C5), pip timeout (C8), exit/signals (C11),
stop budget (D1), cancelled boot (D2)."""

import asyncio
import errno
import importlib
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from homeassistant import core
from homeassistant.util.async_ import get_scheduled_timer_handles

import logbuffer
import run
from custom_components.integration_manager import scheduler as sched_mod
from custom_components.integration_manager.installer import Installer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RestartFailureTest(unittest.IsolatedAsyncioTestCase):
    """16: a restart that fails before stopping leaves busy unset and says so."""

    def installer(self):
        inst = Installer(SimpleNamespace(config=SimpleNamespace(config_dir=tempfile.mkdtemp())))
        self.stops = []

        async def job(fn, *args):
            return fn(*args)

        async def stop():
            return None

        def create_task(coro):
            self.stops.append(coro)
            coro.close()

        inst.hass = SimpleNamespace(async_add_executor_job=job, async_create_task=create_task, async_stop=stop)
        return inst

    async def test_enospc_on_save(self):
        inst = self.installer()
        inst.state.restart_required = True
        inst.state.last_action = "installed x"
        with mock.patch.object(inst, "_save_state", side_effect=OSError(errno.ENOSPC, "No space left on device")), \
                self.assertLogs("custom_components.integration_manager.installer", "ERROR"):
            res = await inst.restart()
        self.assertFalse(res["ok"])
        self.assertIn("No space left", res["error"])
        self.assertFalse(inst.busy)
        self.assertEqual((inst.state.restart_required, inst.state.last_action), (True, "installed x"))
        self.assertEqual(self.stops, [])
        self.assertEqual(await inst.restart(), {"ok": True})  # not stuck: the next one goes through
        self.assertEqual(len(self.stops), 1)

    async def test_failure_in_executor_step(self):
        inst = self.installer()
        with mock.patch.object(inst, "_reset_boot_failures", side_effect=RuntimeError("boom")), self.assertLogs(level="ERROR"):
            res = await inst.restart()
        self.assertEqual((res["ok"], inst.busy, self.stops), (False, False, []))

    async def test_cancelled_request_resets_busy_and_propagates(self):
        inst = self.installer()

        async def job(fn, *args):
            raise asyncio.CancelledError

        inst.hass.async_add_executor_job = job
        with self.assertRaises(asyncio.CancelledError):
            await inst.restart()
        self.assertFalse(inst.busy)


class _Bus:
    def __init__(self):
        self.once = {}

    def async_listen_once(self, event, cb):
        self.once[event] = cb


class SchedulerStopTest(unittest.IsolatedAsyncioTestCase):
    """17: HA's cancel_on_shutdown does not reach async_call_later timers; the scheduler cancels them itself."""

    def pending(self, loop, target):
        return [h for h in get_scheduled_timer_handles(loop) if not h.cancelled() and target in repr(h._args)]  # noqa: SLF001

    async def test_ha_does_not_cancel_call_later_but_stop_does(self):
        from homeassistant.const import EVENT_HOMEASSISTANT_STOP
        from homeassistant.helpers.event import async_call_later

        loop = asyncio.get_running_loop()
        hass = SimpleNamespace(loop=loop, bus=_Bus())
        inst = SimpleNamespace(settings=SimpleNamespace(int_=lambda *_a: 3, bool_=lambda _k: True), busy=True, backup_running=False)
        sch = sched_mod.Scheduler(hass, inst)
        daily_unsub = mock.Mock()
        with mock.patch.object(sched_mod, "async_track_time_change", return_value=daily_unsub), \
                mock.patch.object(sched_mod, "async_call_later", async_call_later):
            sch.start()
            await sch._daily(None)  # busy: arms the 30 min retry
        self.assertEqual(len(self.pending(loop, "release check at boot")), 1)
        self.assertEqual(len(self.pending(loop, "daily backup retry")), 1)

        core.HomeAssistant._cancel_cancellable_timers(SimpleNamespace(loop=loop))  # what HA does at stop: misses them
        self.assertEqual(len(self.pending(loop, "release check at boot")), 1, "HA now cancels call_later timers: simplify")

        hass.bus.once[EVENT_HOMEASSISTANT_STOP](None)
        self.assertEqual(self.pending(loop, "release check at boot"), [])
        self.assertEqual(self.pending(loop, "daily backup retry"), [])
        daily_unsub.assert_called_once()


class EmfileTest(unittest.TestCase):
    """19: EMFILE in the loop stops it, like homeassistant.runner."""

    def run_handler(self, exc):
        loop = asyncio.new_event_loop()
        try:
            loop.call_soon(run._loop_exception_handler, loop, {"message": "accept failed", "exception": exc})
            loop.call_later(0.5, loop.stop)  # the fallback stop, for the non-fatal case
            t0 = loop.time()
            with self.assertLogs(run._LOGGER, "ERROR") as logs:
                loop.run_forever()
            return loop.time() - t0, "\n".join(logs.output)
        finally:
            loop.close()

    def test_emfile_stops_the_loop(self):
        took, out = self.run_handler(OSError(errno.EMFILE, "Too many open files"))
        self.assertLess(took, 0.4)
        self.assertIn("Fatal error", out)

    def test_other_errors_do_not(self):
        took, out = self.run_handler(OSError(errno.ENOENT, "nope"))
        self.assertGreaterEqual(took, 0.4)
        self.assertNotIn("Fatal error", out)

    def test_stopped_boot_exits_non_zero(self):
        async def boot():
            loop = asyncio.get_running_loop()
            loop.call_soon(run._loop_exception_handler, loop, {"message": "x", "exception": OSError(errno.EMFILE, "Too many open files")})
            await asyncio.sleep(3600)
            return 0

        with self.assertLogs(run._LOGGER, "ERROR"), self.assertRaises(RuntimeError):  # main() logs it and exits 1
            run._run_loop(boot)


class CancelledBootTest(unittest.TestCase):
    """D2: only a boot cancelled by the boot signal handler is a clean exit."""

    async def _cancelled(self):
        raise asyncio.CancelledError

    def test_without_signal_non_zero(self):
        with mock.patch.object(run, "_boot_signalled", False), self.assertLogs(run._LOGGER, "CRITICAL"):
            self.assertEqual(run._run_loop(self._cancelled), 1)

    def test_after_signal_zero(self):
        with mock.patch.object(run, "_boot_signalled", True):
            self.assertEqual(run._run_loop(self._cancelled), 0)


class StopBudgetTest(unittest.TestCase):
    """D1: the watchdog exits before Docker's SIGKILL (240 s from SIGTERM)."""

    def test_budget(self):
        from homeassistant.core import STOPPING_STAGE_SHUTDOWN_TIMEOUT

        grace = 240
        with open(os.path.join(ROOT, "docker-compose.yml"), encoding="utf-8") as fh:
            self.assertIn(f"stop_grace_period: {grace}s", fh.read())
        self.assertLess(STOPPING_STAGE_SHUTDOWN_TIMEOUT + run.STOP_WATCHDOG_S + run.WATCHDOG_DRAIN_S + run.LOG_FLUSH_S, grace)


CHILD = r"""
import asyncio, logging, os, sys
logging.basicConfig(level=logging.INFO)
import run, signal
from types import SimpleNamespace
from homeassistant import core

async def boot():
    never = asyncio.Event()

    async def async_stop(_code):
        await never.wait()  # a stop that hangs: the second signal must still kill

    hass = SimpleNamespace(state=core.CoreState.starting, data={}, async_stop=async_stop)
    run._install_boot_signal_handlers(asyncio.current_task(), lambda: hass)
    print("ready", flush=True)
    await never.wait()

run._run_loop(boot)
"""


class SecondSignalTest(unittest.TestCase):
    """C11: after the boot signal handler ran, a second SIGTERM kills the process."""

    def test_second_sigterm_kills(self):
        cfg = tempfile.mkdtemp()
        env = {**os.environ, "PYTHONPATH": ROOT, "HRI_CONFIG": cfg, "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.Popen([sys.executable, "-c", CHILD], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(proc.stdout.readline().strip(), "ready")
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(1)
            self.fail("the first SIGTERM should stop cleanly, not exit at once")
        except subprocess.TimeoutExpired:
            pass
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, -signal.SIGTERM)

    def test_boot_does_not_let_ha_reattach_its_handlers(self):
        import inspect

        src = inspect.getsource(run._boot)
        self.assertIn("async_run(attach_signals=False)", src)
        self.assertIn("if not _boot_signalled:", src)


class ExitTest(unittest.TestCase):
    """C11: a stuck log listener (blocked stderr) skips the stream flushes too."""

    def test_no_flush_when_listener_stuck(self):
        err = mock.Mock()
        with mock.patch.object(run.logbuffer, "stop_queue", return_value=False), mock.patch.object(run.sys, "stderr", err), \
                mock.patch.object(run.sys, "stdout", mock.Mock()), mock.patch.object(run.os, "_exit") as ex, \
                mock.patch.object(run.logging, "shutdown") as shut:
            run._exit(3)
        err.flush.assert_not_called()
        shut.assert_not_called()
        ex.assert_called_once_with(3)

    def test_flush_when_listener_done(self):
        err = mock.Mock()
        with mock.patch.object(run.logbuffer, "stop_queue", return_value=True), mock.patch.object(run.sys, "stderr", err), \
                mock.patch.object(run.sys, "stdout", mock.Mock()), mock.patch.object(run.os, "_exit"), mock.patch.object(run.logging, "shutdown"):
            run._exit(0)
        err.flush.assert_called_once()


class _Blocking(logging.Handler):
    def __init__(self):
        super().__init__()
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.messages = []

    def emit(self, record):
        self.entered.set()
        self.gate.wait()
        self.messages.append(record.getMessage())


class BoundedLogQueueTest(unittest.TestCase):
    """C5: a stuck listener drops records instead of growing memory; the count is logged once it runs again."""

    def test_drop_and_report(self):
        logger = logging.getLogger("hri-test-bounded")
        logger.propagate = False
        sink = _Blocking()
        logger.handlers = [sink]
        logger.setLevel(logging.INFO)
        logbuffer.activate_queue(logger, maxsize=3)
        try:
            logger.info("first")  # taken by the listener, which then blocks
            self.assertTrue(sink.entered.wait(5))
            for i in range(10):
                logger.info("r%s", i)  # 3 fit, 7 dropped, no exception in this thread
            self.assertFalse(logbuffer.flush_queue(0.2, logger))  # no room for the marker: bounded, not stuck
            sink.gate.set()
            self.assertTrue(logbuffer.flush_queue(5, logger))
        finally:
            sink.gate.set()
            self.assertTrue(logbuffer.stop_queue(5, logger))
        notes = [m for m in sink.messages if "dropped" in m]
        self.assertEqual([m for m in sink.messages if m not in notes], ["first", "r0", "r1", "r2"])
        self.assertEqual(len(notes), 1)  # reported once, as soon as the listener moves again
        self.assertIn("7 log records were dropped", notes[0])


class EntrypointTest(unittest.TestCase):
    def ep(self, cfg):
        os.environ["HRI_CONFIG"] = cfg
        sys.modules.pop("entrypoint", None)
        mod = importlib.import_module("entrypoint")
        self.addCleanup(lambda: (os.environ.pop("HRI_CONFIG", None), sys.modules.pop("entrypoint", None)))
        return mod

    def test_boot_failure_counted_right_before_exec(self):
        """20: a SIGTERM while old venvs are pruned is not a failed boot."""
        cfg = tempfile.mkdtemp()
        ep = self.ep(cfg)
        os.makedirs(ep.STATE_DIR, exist_ok=True)
        with open(ep.HA_FILE, "w", encoding="utf-8") as fh:
            json.dump({"current": "2026.9.2", "desired": "2026.9.2", "boot_failures": 1}, fh)

        class Stop(BaseException):
            pass

        def count():
            with open(ep.HA_FILE, encoding="utf-8") as fh:
                return json.load(fh)["boot_failures"]

        common = [mock.patch.object(ep, "venv_ok", return_value=True), mock.patch.object(ep, "ensure_extra_requirements"),
                  mock.patch.object(ep, "apply_config_changes", side_effect=lambda s, w, c: w), mock.patch.object(ep, "restrict_umask"),
                  mock.patch.object(ep.os, "symlink")]
        for p in common:
            p.start()
            self.addCleanup(p.stop)
        with mock.patch.object(ep, "prune", side_effect=Stop), self.assertRaises(Stop):
            ep.main()  # interrupted during prune
        self.assertEqual(count(), 1)

        seen = []
        with mock.patch.object(ep, "prune"), mock.patch.object(ep.os, "execv", side_effect=lambda *a: (seen.append(count()), (_ for _ in ()).throw(Stop()))), \
                self.assertRaises(Stop):
            ep.main()
        self.assertEqual(seen, [2])

    def test_pip_install_has_an_idle_budget(self):
        """C8: a hung pip fails the install cleanly - on silence now, not on the wall clock."""
        cfg = tempfile.mkdtemp()
        ep = self.ep(cfg)
        os.makedirs(ep.STATE_DIR, exist_ok=True)
        calls = []

        def fake_run(cmd, **kw):
            calls.append(kw)
            if "homeassistant==2026.9.2" in cmd:
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout") or kw.get("idle_timeout"))
            os.makedirs(cmd[-1], exist_ok=True)  # the venv
            return subprocess.CompletedProcess(cmd, 0)

        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b""
        with mock.patch.object(ep.subprocess, "run", fake_run), mock.patch.object(ep.urllib.request, "urlopen", return_value=resp), \
                mock.patch.object(ep, "_run_pip", side_effect=lambda cmd, out, **kw: fake_run(cmd, **kw)):  # pip runs in its own process group
            self.assertFalse(ep.install("2026.9.2"))
        self.assertGreaterEqual(ep.PIP_IDLE_TIMEOUT_S, 600)  # the budget install() leaves to a silent pip
        self.assertFalse(os.path.exists(ep.venv_dir("2026.9.2")))


class FaulthandlerTest(unittest.TestCase):
    def test_main_enables_it(self):
        import inspect

        self.assertIn("faulthandler.enable(file=sys.stderr)", inspect.getsource(run.main))


if __name__ == "__main__":
    unittest.main()
