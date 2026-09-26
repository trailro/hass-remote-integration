"""C5: write ordering and durability.  ha.json read-modify-writes under one
per-file lock, state.json and ha.json fsynced, one ordered writer thread for
the saves that used to be loose executor jobs (snapshot on the loop, errors
back to the caller, drained at restart/final write/backup/exit), the import
map flushed at the final write, and the root log handlers behind a queue."""

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE, EVENT_HOMEASSISTANT_STOP

import jsonio
import logbuffer
import run
from custom_components.integration_manager import events, manager_device as md, mqtt_rules
from custom_components.integration_manager.ha_import import RegistryAligner
from custom_components.integration_manager.ha_updater import HaUpdater
from custom_components.integration_manager.installer import Installer, State
from custom_components.integration_manager.mqtt_publisher import MqttConfig, MqttPublisher
from custom_components.integration_manager.settings import Settings
from custom_components.integration_manager.views import MqttRulesView
from custom_components.integration_manager import http_util
from custom_components.integration_manager import writer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _slow_writes(delay=0.3):
    """jsonio.write_json (what the writer thread calls) and the names modules bound before C5, delayed."""
    real = jsonio.write_json

    def slow(*a, **k):
        time.sleep(delay)
        return real(*a, **k)

    return slow


class _Gate:
    """The resetter's first read of ha.json waits (at most 1 s) for the other writer to finish."""

    def __init__(self):
        self.read_done, self.other_done = threading.Event(), threading.Event()
        self.real = jsonio.read_json

    def read(self, path, default=None):
        data = self.real(path, default)
        if threading.current_thread().name == "resetter" and not self.read_done.is_set():
            self.read_done.set()
            self.other_done.wait(1.0)
        return data


class HaJsonLostUpdateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "integration_manager"))
        self.path = os.path.join(self.dir, "integration_manager", "ha.json")
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"current": "2026.9.1", "boot_failures": 2}, fh)
        self.updater = object.__new__(HaUpdater)
        self.updater.file = self.path

    def _race(self, resetter):
        gate = _Gate()

        def other():
            gate.read_done.wait(2)
            try:
                self.updater.set_desired("2026.9.2")
            finally:
                gate.other_done.set()

        with mock.patch.object(jsonio, "read_json", gate.read), mock.patch.object(run, "read_json", gate.read):
            threads = [threading.Thread(target=resetter, name="resetter"), threading.Thread(target=other)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        return _read(self.path)

    def test_executor_undo_does_not_drop_set_desired(self):
        state = self._race(lambda: Installer._undo_boot_failure(SimpleNamespace(hass=SimpleNamespace(data={}), state_dir=os.path.dirname(self.path))))
        self.assertEqual(state.get("desired"), "2026.9.2")
        self.assertEqual(state["boot_failures"], 1)  # this boot's increment, not the earlier crash

    def test_run_boot_ok_does_not_drop_set_desired(self):
        with mock.patch.object(run, "CONFIG_DIR", self.dir):
            state = self._race(run._mark_boot_ok)
        self.assertEqual(state.get("desired"), "2026.9.2")
        self.assertEqual(state["boot_failures"], 0)

    def test_update_json_threads_never_lose_a_field(self):
        def bump(key):
            for _ in range(40):
                jsonio.update_json(self.path, lambda d: {**d, key: d.get(key, 0) + 1}, fsync=False)

        threads = [threading.Thread(target=bump, args=(f"k{i}",)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        state = _read(self.path)
        self.assertEqual([state.get(f"k{i}") for i in range(6)], [40] * 6)

    def test_ha_json_writes_are_fsynced(self):
        with mock.patch("os.fsync", wraps=os.fsync) as fsync:
            self.updater.set_desired("2026.9.2")
        self.assertTrue(fsync.called, "set_desired")
        with mock.patch.object(run, "CONFIG_DIR", self.dir), mock.patch("os.fsync", wraps=os.fsync) as fsync:
            run._update_ha_json(lambda s: {**s, "boot_failures": 1})
        self.assertTrue(fsync.called, "run._update_ha_json")

    def test_unreadable_ha_json_still_refused_and_kept(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"current": ')
        with self.assertRaises(ValueError):
            self.updater.set_desired("2026.9.2")
        Installer._undo_boot_failure(SimpleNamespace(hass=SimpleNamespace(data={}), state_dir=os.path.dirname(self.path)))
        with mock.patch.object(run, "CONFIG_DIR", self.dir):
            run._mark_boot_ok()
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), '{"current": ')


class StateJsonTest(unittest.TestCase):
    def test_save_state_is_fsynced(self):
        with tempfile.TemporaryDirectory() as d:
            inst = object.__new__(Installer)
            inst.state_file = os.path.join(d, "state.json")
            inst.state = State()
            with mock.patch("os.fsync", wraps=os.fsync) as fsync:
                inst._save_state()
            self.assertTrue(fsync.called)
            self.assertIsInstance(_read(inst.state_file), dict)


class WriterTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.w = writer.Writer()
        self.addCleanup(self.w.stop, 5)

    def test_last_of_rapid_saves_wins(self):
        path = os.path.join(self.dir, "a.json")

        async def main():
            await asyncio.gather(*(self.w.async_write(path, {"n": i}) for i in range(50)))

        asyncio.run(main())
        self.assertEqual(_read(path), {"n": 49})

    def test_snapshot_taken_at_submit(self):
        path = os.path.join(self.dir, "b.json")
        data = {"rules": {"a": {"exclude": True}}}

        async def main():
            with mock.patch.object(jsonio, "write_json", _slow_writes()):
                task = asyncio.ensure_future(self.w.async_write(path, data))
                await asyncio.sleep(0)  # submitted
                data["rules"]["b"] = {"exclude": True}
                await task

        asyncio.run(main())
        self.assertEqual(_read(path), {"rules": {"a": {"exclude": True}}})

    def test_error_reaches_the_caller(self):
        async def main():
            with mock.patch.object(jsonio, "write_json", side_effect=OSError("disk full")):
                await self.w.async_write(os.path.join(self.dir, "c.json"), {})

        with self.assertRaisesRegex(OSError, "disk full"):
            asyncio.run(main())

    def test_drain_waits_for_pending_writes(self):
        path = os.path.join(self.dir, "d.json")
        with mock.patch.object(jsonio, "write_json", _slow_writes(0.5)):
            self.w.write_nowait(path, {"x": 1})
            self.assertTrue(self.w.drain(5))
            self.assertTrue(os.path.isfile(path))

            async def main():
                self.w.write_nowait(path, {"x": 2})
                return await self.w.async_drain(5)

            self.assertTrue(asyncio.run(main()))
        self.assertEqual(_read(path), {"x": 2})

    def test_drain_timeout_is_reported(self):
        with mock.patch.object(jsonio, "write_json", _slow_writes(1.0)):
            self.w.write_nowait(os.path.join(self.dir, "e.json"), {})
            self.assertFalse(self.w.drain(0.1))
            self.assertTrue(self.w.drain(5))

    def test_writes_after_stop_still_land(self):
        path = os.path.join(self.dir, "f.json")
        self.assertTrue(self.w.stop(5))
        self.w.write_nowait(path, {"late": True})
        self.assertTrue(self.w.drain(5))
        self.assertEqual(_read(path), {"late": True})

    def test_timing_200_settings_saves(self):
        st = Settings(self.dir)

        async def main():
            with mock.patch.object(writer, "_WRITER", self.w):
                t0 = time.perf_counter()
                for i in range(200):
                    st.data["backup_keep"] = i
                    await st.async_save()
                seq = time.perf_counter() - t0
                t0 = time.perf_counter()
                futures = []
                for i in range(200, 400):
                    st.data["backup_keep"] = i
                    futures.append(self.w.write_nowait(st.path, st.data, indent=1, mode=0o600))  # each copied at its own value
                self.assertTrue(await self.w.async_drain(60))
                burst = time.perf_counter() - t0
                self.assertTrue(all(f.done() and f.exception() is None for f in futures))
            return seq, burst

        seq, burst = asyncio.run(main())
        print(f"\n[c5 timing] 200 awaited settings saves {seq * 1000:.0f} ms; 200 queued at once {burst * 1000:.0f} ms", file=sys.stderr)
        self.assertEqual(_read(st.path)["backup_keep"], 399)
        self.assertEqual(os.stat(st.path).st_mode & 0o777, 0o600)
        self.assertLess(seq, 30)


class RoutedSavesTest(unittest.TestCase):
    """The call sites that ran json.dump in the executor over dicts the loop mutates, or saved out of order."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_rules_view_writes_what_was_posted(self):
        path = os.path.join(self.dir, "mqtt_rules.json")
        publisher = mock.Mock()
        publisher.rules = mqtt_rules.MqttRules(path)
        publisher.async_apply_rules = mock.AsyncMock(return_value={})
        view = MqttRulesView(publisher)
        entered, release = threading.Event(), threading.Event()
        real = jsonio.write_json

        def gated(*a, **k):
            entered.set()
            release.wait(5)
            return real(*a, **k)

        async def main():
            loop = asyncio.get_running_loop()
            publisher.hass.async_add_executor_job = lambda f, *a: loop.run_in_executor(None, f, *a)
            with mock.patch.object(http_util, "_json_object", mock.AsyncMock(return_value={"rules": {"sensor.a": {"exclude": True}}})), \
                    mock.patch.object(view, "json", side_effect=lambda d, **k: d), \
                    mock.patch.object(jsonio, "write_json", gated), mock.patch.object(mqtt_rules, "write_json", gated, create=True):
                task = asyncio.ensure_future(view.post(mock.Mock()))
                await loop.run_in_executor(None, entered.wait, 5)
                publisher.rules.rules["sensor.b"] = {"exclude": True}  # the next request, on the loop
                release.set()
                return await task

        self.assertTrue(asyncio.run(main())["ok"])
        self.assertEqual(_read(path), {"rules": {"sensor.a": {"exclude": True}}})

    def test_remember_latest_lands_in_order(self):
        device = md.ManagerDevice.__new__(md.ManagerDevice)
        device._latest_file = os.path.join(self.dir, md.LATEST_FILE)
        device._latest_saved = {}
        device.manager_tag = device._ha_latest = None
        device.manager_releases = []
        first = threading.Event()
        real = jsonio.write_json

        def first_slow(*a, **k):
            if not first.is_set():
                first.set()
                time.sleep(0.5)
            return real(*a, **k)

        async def main():
            loop = asyncio.get_running_loop()
            device.hass = mock.Mock()
            device.hass.async_add_executor_job = lambda f, *a: loop.run_in_executor(None, f, *a)
            with mock.patch.object(jsonio, "write_json", first_slow), mock.patch.object(md, "write_json", first_slow):
                device.manager_latest = "1.0.0"
                device._remember_latest()
                await asyncio.sleep(0.05)
                device.manager_latest = "2.0.0"
                device._remember_latest()
                await asyncio.sleep(1.2)
                await writer.async_drain(5)

        asyncio.run(main())
        self.assertEqual(_read(device._latest_file)["manager"], "2.0.0")

    def test_mqtt_saves_queued_together_keep_both_changes(self):
        pub = object.__new__(MqttPublisher)
        pub.path = os.path.join(self.dir, "mqtt.json")
        pub.config = MqttConfig()
        pub._saved = pub._disk_read = None

        async def main():
            loop = asyncio.get_running_loop()
            pub.hass = mock.Mock()
            pub.hass.async_add_executor_job = lambda f, *a: loop.run_in_executor(None, f, *a)
            with mock.patch.object(jsonio, "write_json", _slow_writes(0.2)):
                a, b = await asyncio.gather(pub.async_save({"host": "broker.lan"}), pub.async_save({"port": 1884}))
            return a, b

        _, b = asyncio.run(main())
        on_disk = _read(pub.path)
        self.assertEqual((on_disk["host"], on_disk["port"]), ("broker.lan", 1884))
        self.assertEqual((b.host, b.port), ("broker.lan", 1884))
        self.assertEqual(os.stat(pub.path).st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            asyncio.run(pub.async_save({"port": "x"}))


class DrainPointsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.target = os.path.join(self.dir, "settings.json")

    def _installer(self, loop):
        inst = object.__new__(Installer)
        inst.busy = False
        inst.state = SimpleNamespace(restart_required=True, last_action=None)
        inst._save_state = lambda: None
        inst.state_dir = self.dir
        inst.hass = mock.Mock()
        inst.hass.async_add_executor_job = lambda f, *a: loop.run_in_executor(None, f, *a)
        inst.hass.config_entries = SimpleNamespace()
        return inst

    def test_restart_drains_before_stopping(self):
        seen = []

        async def main():
            inst = self._installer(asyncio.get_running_loop())
            inst.hass.async_stop = mock.Mock(side_effect=lambda *a: seen.append(os.path.isfile(self.target)))
            with mock.patch.object(jsonio, "write_json", _slow_writes(0.5)), mock.patch.object(events, "emit"):
                writer.write_nowait(self.target, {"x": 1})
                return await inst.restart()

        self.assertTrue(asyncio.run(main())["ok"])
        self.assertEqual(seen, [True])

    def test_backup_flush_drains(self):
        async def main():
            inst = self._installer(asyncio.get_running_loop())
            with mock.patch.object(jsonio, "write_json", _slow_writes(0.5)):
                writer.write_nowait(self.target, {"x": 1})
                await inst.async_flush_stores()
                return os.path.isfile(self.target)

        self.assertTrue(asyncio.run(main()))

    def test_final_write_drains(self):
        listeners = {}
        hass = mock.Mock()
        hass.bus.async_listen_once = lambda ev, cb: listeners.setdefault(ev, []).append(cb)
        writer.async_register(hass)

        async def main():
            with mock.patch.object(jsonio, "write_json", _slow_writes(0.5)):
                writer.write_nowait(self.target, {"x": 1})
                for cb in listeners[EVENT_HOMEASSISTANT_FINAL_WRITE]:
                    await cb(None)
                return os.path.isfile(self.target)

        self.assertTrue(asyncio.run(main()))


class AlignerFinalWriteTest(unittest.TestCase):
    def test_pending_map_save_is_flushed_at_stop(self):
        with tempfile.TemporaryDirectory() as d:
            listeners = {}

            async def main():
                loop = asyncio.get_running_loop()
                hass = mock.Mock()
                hass.loop = loop
                hass.config.path = lambda *p: os.path.join(d, *p)
                hass.async_add_executor_job = lambda f, *a: loop.run_in_executor(None, f, *a)
                hass.bus.async_listen = mock.Mock()
                hass.bus.async_listen_once = lambda ev, cb: listeners.setdefault(ev, []).append(cb)
                aligner = RegistryAligner(hass)
                os.makedirs(os.path.dirname(aligner.path), exist_ok=True)
                aligner.async_start()
                aligner.merge_map({"domain": "demo", "entities": {"sensor:a": {"entity_id": "sensor.a"}}, "devices": {}})
                for ev in (EVENT_HOMEASSISTANT_STOP, EVENT_HOMEASSISTANT_FINAL_WRITE):
                    for cb in listeners.get(ev, []):
                        res = cb(None)
                        if asyncio.iscoroutine(res):
                            await res
                flushed = os.path.isfile(aligner.path) and "sensor:a" in _read(aligner.path)["domains"]["demo"]["entities"]
                aligner.merge_map({"domain": "demo", "entities": {"sensor:b": {"entity_id": "sensor.b"}}, "devices": {}})
                debounced = aligner._save_handle is not None
                await asyncio.sleep(0.3)
                late = "sensor:b" in _read(aligner.path)["domains"]["demo"]["entities"] if os.path.isfile(aligner.path) else False
                return flushed, debounced, late

            flushed, debounced, late = asyncio.run(main())
            self.assertTrue(flushed, "a change followed by a stop before the debounce fired is on disk")
            self.assertFalse(debounced, "no debounced save after the stop")
            self.assertTrue(late)


class LogQueueTest(unittest.TestCase):
    def _logger(self, name):
        logger = logging.getLogger(name)
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        self.addCleanup(setattr, logger, "handlers", [])
        return logger

    def test_handlers_run_off_the_logging_thread(self):
        logger = self._logger("hri.c5.thread")
        seen = []

        class Recorder(logging.Handler):
            def emit(self, record):
                seen.append(threading.current_thread().name)

        logger.addHandler(Recorder())
        logbuffer.activate_queue(logger)
        self.addCleanup(logbuffer.stop_queue, 5, logger)
        logger.warning("from the caller")
        self.assertTrue(logbuffer.flush_queue(5, logger))
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0], threading.current_thread().name)

    def test_ids_increase_and_nothing_is_lost(self):
        with tempfile.TemporaryDirectory() as d:
            logger = self._logger("hri.c5.ids")
            handler = logbuffer.FileLogHandler(os.path.join(d, "process.log"))
            logger.addHandler(handler)
            logbuffer.activate_queue(logger)

            def spam(n):
                for i in range(200):
                    logger.info("t%s line %s", n, i)

            threads = [threading.Thread(target=spam, args=(n,)) for n in range(6)]
            for t in threads:
                t.start()
            try:
                1 / 0
            except ZeroDivisionError:
                logger.exception("boom %s", "here")
            for t in threads:
                t.join(30)
            self.assertTrue(logbuffer.stop_queue(10, logger))
            self.assertIs(logger.handlers[0], handler)  # direct again: late lines at exit are still written
            logger.info("after stop")
            handler.close()
            with open(handler.path, encoding="utf-8") as fh:
                recs = [json.loads(line) for line in fh]
            ids = [r["id"] for r in recs]
            self.assertEqual(ids, sorted(set(ids)))
            self.assertEqual(len(recs), 6 * 200 + 2)
            boom = next(r for r in recs if r["message"] == "boom here")
            self.assertIn("ZeroDivisionError", boom["exc"])
            self.assertNotIn("Traceback", boom["message"])
            self.assertEqual(recs[-1]["message"], "after stop")

    def test_find_and_query_through_the_queue(self):
        root = logging.getLogger()
        saved = root.handlers[:]
        with tempfile.TemporaryDirectory() as d:
            handler = logbuffer.FileLogHandler(os.path.join(d, "process.log"))
            root.handlers = [handler]
            try:
                logbuffer.activate_queue()
                self.assertIs(logbuffer.install(handler.path), handler)  # not a second handler
                logging.getLogger("hri.c5.find").warning("logged just before the query")
                self.assertTrue(logbuffer.flush_queue(5))
                self.assertIs(logbuffer.find(), handler)
                recs, _ = logbuffer.find().query(text="just before")
                self.assertEqual([r["message"] for r in recs], ["logged just before the query"])
                self.assertEqual(logbuffer.find().loggers.get("hri.c5.find"), 1)
            finally:
                logbuffer.stop_queue(5)
                root.handlers = saved
                handler.close()


CHILD = r"""
import json, logging, os, sys, threading, time
import jsonio, logbuffer, run
mode, target = sys.argv[1], sys.argv[2]
if mode == "exit":
    from custom_components.integration_manager import writer
    real = jsonio.write_json
    def slow(*a, **k):
        time.sleep(0.5)
        return real(*a, **k)
    jsonio.write_json = slow

    def fake_run_loop(boot):
        log = logging.getLogger("hri.c5.exit")
        for i in range(3000):
            log.info("filler %d", i)
        writer.write_nowait(target, {"written": True})
        log.warning("stopping: last line")
        return 7

    run._run_loop = fake_run_loop
    run.main()
elif mode == "watchdog":
    logging.basicConfig(level=logging.INFO)
    logbuffer.install(os.path.join(run.CONFIG_DIR, "integration_manager", "process.log"))
    if hasattr(logbuffer, "activate_queue"):
        logbuffer.activate_queue()

    def flood():  # stderr is a pipe nobody reads: its handler blocks for good
        log = logging.getLogger("hri.c5.flood")
        while True:
            log.info("x" * 200)

    threading.Thread(target=flood, daemon=True).start()
    time.sleep(0.5)
    run._arm_stop_watchdog(0.5)
    threading.Event().wait()
"""


class ExitPathTest(unittest.TestCase):
    def _child(self, mode, cfg, target=""):
        env = {**os.environ, "PYTHONPATH": ROOT, "HRI_CONFIG": cfg, "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.Popen([sys.executable, "-c", CHILD, mode, target], cwd=ROOT, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE if mode == "watchdog" else subprocess.DEVNULL)

    def _records(self, cfg):
        out = []
        base = os.path.join(cfg, "integration_manager", "process.log")
        for p in (base + ".2", base + ".1", base):
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as fh:
                    out += [json.loads(line) for line in fh if line.strip()]
        return out

    def test_main_drains_writer_and_log_before_exit(self):
        with tempfile.TemporaryDirectory() as cfg:
            target = os.path.join(cfg, "late.json")
            proc = self._child("exit", cfg, target)
            self.assertEqual(proc.wait(60), 7)
            recs = self._records(cfg)
            self.assertEqual(recs[-1]["message"], "stopping: last line")
            self.assertEqual(_read(target), {"written": True})

    def test_watchdog_line_written_even_with_a_blocked_handler(self):
        with tempfile.TemporaryDirectory() as cfg:
            proc = self._child("watchdog", cfg)
            try:
                rc = proc.wait(20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                self.fail("the stop watchdog never exited (its log line blocked on stderr)")
            finally:
                proc.stderr.close()
            self.assertEqual(rc, 1)
            self.assertTrue(any(r["level"] == "CRITICAL" and "exiting hard" in r["message"] for r in self._records(cfg)))


if __name__ == "__main__":
    unittest.main()
