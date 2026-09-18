"""A test campaign wedged every executor thread with hri_probe.stuck and then asked for a restart: the
manager answered ok, nothing stopped, and the hard exit that exists for exactly that case never fired.
Here: the restart path off the executor and under the watchdog, the manager result after the restart and
not before it, a bound on the manager action lock, a bound on the HTTP service call, and a notification
burst that no longer flushes the timeline's page."""

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import jsonio
import run
from custom_components.integration_manager import events, installer as inst_mod
from custom_components.integration_manager import manager_device as md
from custom_components.integration_manager import notifications as notif
from custom_components.integration_manager import services_page
from custom_components.integration_manager.mqtt_publisher import CALLS_IN_FLIGHT_MAX
from tests.fakes import FakeInstaller, FakePublisher, FakeUpdater

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCHDOG_KEY = "hri_stop_watchdog"  # run.py publishes the arming function here, installer.restart calls it


def _tmp(test):
    tmp = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, tmp, True)
    return tmp


def _events(test):
    events.EVENTS = events.Events(os.path.join(_tmp(test), "events.jsonl"))
    test.addCleanup(setattr, events, "EVENTS", None)


class WedgedExecutor:
    """hass.async_add_executor_job while every worker is blocked in hri_probe.stuck:
    the job is queued behind them and its future never completes."""

    def __init__(self):
        self.jobs = []

    def __call__(self, func, *args):
        self.jobs.append(func)
        return asyncio.get_running_loop().create_future()


def _hass(executor=None, data=None):
    stopped = []
    hass = SimpleNamespace(data=data if data is not None else {}, stopped=stopped)
    hass.async_add_executor_job = executor or (
        lambda func, *a: asyncio.get_running_loop().run_in_executor(None, func, *a))

    async def _stop():
        stopped.append(True)

    hass.async_stop = _stop
    hass.async_create_task = lambda coro, *a, **k: asyncio.get_running_loop().create_task(coro)
    return hass


def _installer(test, hass):
    ins = object.__new__(inst_mod.Installer)
    ins.hass = hass
    ins.busy = False
    ins.state_dir = _tmp(test)
    ins.state_file = os.path.join(ins.state_dir, "state.json")
    ins.state = SimpleNamespace(restart_required=True, last_action="")
    ins._save_state = lambda: jsonio.write_json(ins.state_file, {"last_action": ins.state.last_action})
    return ins


class RestartOffTheExecutorTest(unittest.IsolatedAsyncioTestCase):
    """The boot-failure reset queued behind the wedged pool, so the stop never started."""

    def setUp(self):
        _events(self)

    async def test_restart_stops_although_no_executor_job_can_run(self):
        wedged = WedgedExecutor()
        hass = _hass(executor=wedged)
        ins = _installer(self, hass)
        with open(os.path.join(ins.state_dir, "ha.json"), "w", encoding="utf-8") as fh:
            json.dump({"boot_failures": 3}, fh)

        res = await asyncio.wait_for(ins.restart(), 5)
        await asyncio.sleep(0)

        self.assertEqual(res, {"ok": True})
        self.assertEqual(hass.stopped, [True], "the restart answered ok and nothing stopped")
        self.assertEqual(wedged.jobs, [], "the restart still hands work to the exhausted pool")
        with open(os.path.join(ins.state_dir, "ha.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["boot_failures"], 2)  # this boot's increment only

    async def test_restart_arms_the_stop_watchdog_before_it_asks_ha_to_stop(self):
        armed = []
        hass = _hass(executor=WedgedExecutor(), data={WATCHDOG_KEY: lambda: armed.append(True)})
        ins = _installer(self, hass)

        self.assertTrue((await asyncio.wait_for(ins.restart(), 5))["ok"])
        await asyncio.sleep(0)

        self.assertEqual(armed, [True], "armed only at EVENT_HOMEASSISTANT_STOP, which a hung stop never reaches")

    async def test_an_unarmable_watchdog_does_not_stop_the_restart(self):
        def boom():
            raise RuntimeError("no watchdog here")

        hass = _hass(data={WATCHDOG_KEY: boom})
        ins = _installer(self, hass)

        with self.assertLogs(inst_mod._LOGGER, "ERROR"):
            self.assertTrue((await ins.restart())["ok"])
        await asyncio.sleep(0)

        self.assertEqual(hass.stopped, [True])

    async def test_a_restart_that_fails_before_stopping_still_frees_busy(self):
        hass = _hass()
        ins = _installer(self, hass)
        ins._save_state = mock.Mock(side_effect=OSError("read-only file system"))

        with self.assertLogs(inst_mod._LOGGER, "ERROR"):
            res = await ins.restart()

        self.assertFalse(res["ok"])
        self.assertFalse(ins.busy)
        self.assertEqual(hass.stopped, [])

    async def test_busy_stays_set_once_the_stop_is_under_way(self):
        hass = _hass()
        ins = _installer(self, hass)

        self.assertTrue((await ins.restart())["ok"])
        await asyncio.sleep(0)

        self.assertTrue(ins.busy, "an install must not start while the process goes down")


class StopWatchdogArmingTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, run, "_stop_watchdog", None)
        run._stop_watchdog = None

    def test_arming_twice_starts_one_thread(self):
        first = run._arm_stop_watchdog(3600)  # a daemon thread: it dies with the process
        self.assertIs(run._arm_stop_watchdog(3600), first,
                      "a restart and then the stop event would each start one")

    def test_both_ends_of_the_arming_hand_off_agree(self):
        """run.py publishes the arming function, installer.restart calls it: the
        key is all that ties them together."""
        for name in ("run.py", os.path.join("custom_components", "integration_manager", "installer.py")):
            with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
                self.assertIn(f'"{WATCHDOG_KEY}"', fh.read(), name)


def _device(installer=None):
    log = []
    ins = installer or FakeInstaller(log=log)
    return md.ManagerDevice(_hass(), ins, FakeUpdater(), FakePublisher(log=ins.log))


class ManagerRestartResultTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _events(self)

    async def test_the_result_goes_out_after_the_restart_started(self):
        dev = _device()

        await dev.async_action("restart")

        kinds = [e[0] for e in dev.installer.log]
        self.assertLess(kinds.index("restart"), kinds.index("result"),
                        "the consuming HA is told the restart succeeded before it is even asked for")

    async def test_a_restart_that_never_starts_is_reported_as_failed(self):
        ins = FakeInstaller()
        ins.restart_result = {"ok": False, "error": "restart failed before stopping: no space left on device"}
        dev = _device(ins)

        res = await dev.async_action("restart")

        self.assertFalse(res["ok"])
        self.assertIn("no space left", res["error"])
        published = [e[1] for e in dev.publisher.log if e[0] == "result"]
        self.assertEqual([r["ok"] for r in published], [False], "the only result published says it worked")


class ManagerActionLockTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _events(self)

    async def test_a_hung_action_stops_blocking_new_ones(self):
        dev = _device()
        never = asyncio.Event()

        async def hung():
            await never.wait()
            return {"ok": True}

        dev._do_backup = hung
        first = asyncio.create_task(dev.async_action("backup"))
        self.addCleanup(first.cancel)
        await asyncio.sleep(0)
        self.assertEqual(dev._running, "backup")

        refused = await dev.async_action("check_updates")  # a restart would wait for it first
        self.assertIn("backup is still running", refused["error"])

        dev._running_since = time.monotonic() - 7200  # the same lock, held far past any real action
        res = await dev.async_action("restart")

        self.assertTrue(res["ok"], "one hung action refuses every command for the rest of the process")
        self.assertIn(("restart",), dev.installer.log)


def _post(body):
    async def payload():
        return body

    return SimpleNamespace(content_type="application/json", json=payload)


class ServiceCallBoundTest(unittest.IsolatedAsyncioTestCase):
    """POST /api/services/call held the request open for as long as the service ran."""

    def _view(self, handler):
        hass = SimpleNamespace()
        hass.services = SimpleNamespace(
            has_service=lambda d, s: True,
            supports_response=lambda d, s: services_page.SupportsResponse.NONE,
            async_call=lambda *a, **k: handler(),
        )
        hass.async_create_task = lambda coro, *a, **k: asyncio.get_running_loop().create_task(coro)
        view = services_page.ServiceCallView(hass)
        self.addCleanup(setattr, type(view), "_in_flight", 0)
        setattr(type(view), "_in_flight", 0)
        return view

    @staticmethod
    def _stuck():
        never = asyncio.Event()

        async def handler():
            await never.wait()

        return handler

    async def test_a_service_that_never_returns_times_out_with_the_mqtt_wording(self):
        view = self._view(self._stuck())

        with mock.patch.object(services_page, "CALL_TIMEOUT_S", 1, create=True):
            res = await asyncio.wait_for(view.post(_post({"domain": "hri_probe", "service": "stuck"})), 10)

        self.assertEqual(json.loads(res.body)["error"], "timeout after 1s (service still running)")
        self.assertEqual(type(view)._in_flight, 1, "the timeout did not end the call: it still counts")

    async def test_the_cap_refuses_once_too_many_calls_are_still_running(self):
        """A timeout does not end the call, so bounding the request is not
        enough on its own: a client that keeps retrying piles them up."""
        view = self._view(self._stuck())
        setattr(type(view), "_in_flight", CALLS_IN_FLIGHT_MAX)

        res = await asyncio.wait_for(view.post(_post({"domain": "d", "service": "s"})), 5)

        self.assertIn("too many calls in progress", json.loads(res.body)["error"])

    async def test_a_service_that_answers_in_time_is_unaffected(self):
        async def handler():
            return None

        view = self._view(handler)
        body = json.loads((await view.post(_post({"domain": "d", "service": "s"}))).body)

        self.assertTrue(body["ok"])
        self.assertIsInstance(body["ms"], int)
        self.assertEqual(type(view)._in_flight, 0)


class NotificationBurstTest(unittest.IsolatedAsyncioTestCase):
    """120 notifications filled all 100 rows the Manager page asks for."""

    WAIT_S = 2.5  # longer than the coalescing window

    def setUp(self):
        _events(self)

    def _watch(self):
        registered = []
        hass = SimpleNamespace(loop=asyncio.get_running_loop())
        with mock.patch.object(notif.pn, "async_register_callback", lambda h, cb: registered.append(cb)):
            notif.async_watch(hass)
        return registered[0]

    async def _raised(self, changed):
        cb = self._watch()
        cb(notif.pn.UpdateType.ADDED, changed)
        await asyncio.sleep(self.WAIT_S)

    async def test_a_burst_is_one_line_that_still_says_how_many(self):
        events.emit("mqtt", "connected to broker")
        events.emit("health", "ok -> degraded")

        await self._raised({f"n{i}": {"title": "Probe notification", "message": f"body {i}"} for i in range(120)})

        rows = events.EVENTS.recent(500)
        notifies = [r for r in rows if r["kind"] == "notify"]
        self.assertEqual(len(notifies), 1, "a burst pushes every operational line off the page")
        self.assertIn("120 notifications", notifies[0]["message"])
        self.assertEqual(notifies[0]["data"]["count"], 120)
        self.assertEqual([r["kind"] for r in rows[:2]], ["mqtt", "health"])

    async def test_the_default_page_still_shows_the_operational_lines(self):
        for i in range(6):
            events.emit("mqtt", f"line {i}")

        await self._raised({f"n{i}": {"title": "Probe", "message": "x"} for i in range(120)})

        page = events.EVENTS.recent(100)  # what GET /api/events answers by default
        self.assertEqual(len([r for r in page if r["kind"] == "mqtt"]), 6)

    async def test_a_few_notifications_still_get_a_line_each(self):
        await self._raised({f"n{i}": {"title": f"Title {i}", "message": "body"} for i in range(3)})

        self.assertEqual([r["message"] for r in events.EVENTS.recent(50)],
                         [f"Title {i}: body" for i in range(3)])

    async def test_a_notification_re_created_unchanged_is_not_recorded_twice(self):
        cb = self._watch()
        one = {"n1": {"title": "Probe", "message": "m"}}
        cb(notif.pn.UpdateType.ADDED, one)
        cb(notif.pn.UpdateType.UPDATED, one)
        await asyncio.sleep(self.WAIT_S)

        self.assertEqual(len(events.EVENTS.recent(50)), 1)


if __name__ == "__main__":
    unittest.main()
