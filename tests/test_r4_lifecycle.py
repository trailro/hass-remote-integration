"""Round 4 lifecycle review: crash-loop fallback only from a version that never booted (HRI-01), stops during
run.py's imports (HRI-03), HRI_TRACEMALLOC parsing (HRI-04), the status server's socket (HRI-15), debugpy's
bind address (HRI-20), and the smaller entrypoint / run.py / logbuffer / settings clean-ups."""

import asyncio
import importlib
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

import logbuffer
from tests.fakes import entrypoint_for

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
A, B = "2026.8.3", "2026.9.2"


class Stop(BaseException):
    pass


def make_venv(cfg, version):
    venv = os.path.join(cfg, f"venv-{version}")
    ha_pkg = os.path.join("lib", f"python{sys.version_info[0]}.{sys.version_info[1]}", "site-packages", "homeassistant")
    for folder in ("bin", ha_pkg):
        os.makedirs(os.path.join(venv, folder), exist_ok=True)
    for marker in (".ok", "bin/python", os.path.join(ha_pkg, "__init__.py"), "pyvenv.cfg"):
        open(os.path.join(venv, marker), "w").close()
    return venv


class EntrypointMainBase(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        self.ep = entrypoint_for(self, self.cfg)
        os.makedirs(self.ep.STATE_DIR, exist_ok=True)
        for v in (A, B):
            make_venv(self.cfg, v)
        self.execs = []

        def execv(path, argv):
            self.execs.append(path)
            raise Stop()

        for p in (mock.patch.object(self.ep.os, "execv", side_effect=execv), mock.patch.object(self.ep, "install", return_value=False),
                  mock.patch.object(self.ep, "ensure_extra_requirements"), mock.patch.object(self.ep.backupkit, "pending", return_value=False),
                  mock.patch.object(self.ep, "restrict_umask"), mock.patch.object(self.ep, "fits_this_python", return_value=True)):
            p.start()
            self.addCleanup(p.stop)

    def write(self, state):
        with open(self.ep.HA_FILE, "w", encoding="utf-8") as fh:
            json.dump(state, fh)

    def state(self):
        with open(self.ep.HA_FILE, encoding="utf-8") as fh:
            return json.load(fh)

    def boot(self):
        with self.assertRaises(Stop):
            self.ep.main()
        return os.path.basename(os.path.dirname(os.path.dirname(self.execs[-1])))[5:]


class CrashLoopFallbackTest(EntrypointMainBase):
    """HRI-01: three failed boots of a version that booted before are not a reason to downgrade."""

    def test_proven_version_is_kept_after_a_crash_loop(self):
        self.write({"current": B, "desired": B, "previous": A, "proven": B, "boot_failures": 3})
        self.assertEqual(self.boot(), B)
        self.assertTrue(os.path.isdir(os.path.join(self.cfg, f"venv-{B}")))
        state = self.state()
        self.assertEqual((state["current"], state["desired"], state["previous"]), (B, B, A))
        self.assertEqual(state["boot_failures"], 4)  # still counted
        self.assertIn(B, state["last_error"])
        self.assertNotIn("fallback_from", state)

    def test_existing_volume_without_proven_treats_current_as_proven(self):
        self.write({"current": B, "desired": B, "previous": A, "boot_failures": 3})
        self.assertEqual(self.boot(), B)
        self.assertTrue(os.path.isdir(os.path.join(self.cfg, f"venv-{B}")))
        self.assertEqual(self.state()["previous"], A)

    def test_a_change_that_never_booted_still_falls_back_and_keeps_its_venv_until_the_target_boots(self):
        self.write({"current": B, "desired": B, "previous": A, "proven": A, "change": {"to": B, "mode": "keep"}, "boot_failures": 3})
        self.assertEqual(self.boot(), A)
        state = self.state()
        self.assertEqual((state["current"], state["fallback_from"]), (A, B))
        self.assertNotIn("previous", state)
        self.assertTrue(os.path.isdir(os.path.join(self.cfg, f"venv-{B}")))  # not pruned before A booted

    def test_legacy_volume_mid_change_still_falls_back(self):
        self.write({"current": B, "desired": B, "previous": A, "change": {"to": B, "mode": "keep", "applied": True}, "boot_failures": 3})
        self.assertEqual(self.boot(), A)

    def test_a_fresh_volume_never_starts_below_the_image_floor(self):
        """HA_VERSION_DEFAULT is what a fresh volume installs and HA_VERSION_MIN the oldest the manager
        will switch to; a default under the floor would install a version the UI then refuses."""
        with mock.patch.object(self.ep, "MIN_VERSION", B), mock.patch.object(self.ep, "DEFAULT_VERSION", A), \
             mock.patch.dict(os.environ, {"HA_VERSION_LATEST": "0"}):
            self.write({})
            self.assertEqual(self.boot(), B)

    def test_a_fresh_volume_does_not_call_the_version_it_installed_proven(self):
        """"proven" means run.py saw this version reach STARTED.  The compatibility rule that reads an
        existing volume's "current" as proven used to claim a fresh volume's first install too, where
        nothing has ever booted."""
        self.write({"desired": B})
        self.assertEqual(self.boot(), B)
        state = self.state()
        self.assertEqual((state["current"], state["proven"]), (B, ""))

    def test_a_first_version_that_never_boots_is_not_told_it_booted_before(self):
        self.write({"desired": B})
        self.assertEqual(self.boot(), B)  # installs B and records it, unproven
        self.write({**self.state(), "boot_failures": 3})
        self.assertEqual(self.boot(), B)  # nothing to fall back to on a fresh volume
        error = self.state()["last_error"]
        self.assertNotIn("booted fine before", error)
        self.assertIn("no other version to go back to", error)

    def test_fallen_back_from_venv_is_pruned_once_the_target_booted(self):
        self.write({"current": A, "desired": A, "fallback_from": B, "proven": A, "boot_failures": 0})
        self.assertEqual(self.boot(), A)
        self.assertTrue(os.path.isdir(os.path.join(self.cfg, f"venv-{B}")))  # A has not booted yet
        self.write({**self.state(), "boot_failures": 0, "proven": A})
        state = self.state()
        state.pop("fallback_from")  # what run._mark_boot_ok does
        self.write(state)
        self.assertEqual(self.boot(), A)
        self.assertFalse(os.path.isdir(os.path.join(self.cfg, f"venv-{B}")))


class CorruptStateTest(EntrypointMainBase):
    def test_non_dict_ha_json_is_treated_as_corrupt(self):
        self.write(["not", "a", "dict"])
        os.symlink(os.path.join(self.cfg, f"venv-{B}"), os.path.join(self.cfg, "venv-current"))
        self.assertEqual(self.boot(), B)
        self.assertTrue(os.path.isdir(os.path.join(self.cfg, f"venv-{A}")))  # pruning disabled on a rebuilt state

    def test_string_boot_failures_does_not_crash_the_boot(self):
        self.write({"current": B, "desired": B, "boot_failures": "x"})
        self.assertEqual(self.boot(), B)
        self.assertEqual(self.state()["boot_failures"], 1)

    def test_venv_current_is_replaced_atomically(self):
        link = os.path.join(self.cfg, "venv-current")
        os.symlink(os.path.join(self.cfg, f"venv-{A}"), link)
        self.write({"current": B, "desired": B, "previous": A, "proven": B})
        with mock.patch.object(self.ep.os, "remove", side_effect=AssertionError("removed before the new link exists")):
            self.boot()
        self.assertEqual(os.readlink(link), os.path.join(self.cfg, f"venv-{B}"))


class TmpSweepTest(EntrypointMainBase):
    def test_old_json_tmp_files_are_swept_at_start(self):
        old = os.path.join(self.ep.STATE_DIR, "ha.json.abc123.tmp")
        new = os.path.join(self.ep.STATE_DIR, "state.json.def456.tmp")
        top = os.path.join(self.cfg, "x.json.ghi789.tmp")
        other = os.path.join(self.ep.STATE_DIR, "notes.tmp")
        for p in (old, new, top, other):
            open(p, "w").close()
        past = time.time() - 3600
        for p in (old, top, other):
            os.utime(p, (past, past))
        self.write({"current": B, "desired": B, "proven": B})
        self.boot()
        self.assertEqual([os.path.exists(p) for p in (old, new, top, other)], [False, True, False, True])


class StatusServerTest(unittest.TestCase):
    """HRI-15: the install page's server must release its port, the hold after a failed rollback binds it again."""

    def test_second_server_on_the_same_port_after_stop(self):
        ep = entrypoint_for(self, tempfile.mkdtemp())
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with mock.patch.object(ep, "PORT", port):
            first = ep.start_status_server()
            self.assertIsNotNone(first)
            ep.stop_status_server(first)
            second = ep.start_status_server()
            self.assertIsNotNone(second)
            ep.stop_status_server(second)

    def test_main_closes_the_install_page_server(self):
        import inspect

        ep = importlib.import_module("entrypoint")
        self.assertFalse("srv.shutdown()" in inspect.getsource(ep.main), "main stops the install page without closing its socket")


class PipProcessGroupTest(unittest.TestCase):
    """U4: a pip timeout also kills the build backends pip started."""

    def test_timeout_kills_grandchildren(self):
        ep = importlib.import_module("entrypoint")
        pidfile = os.path.join(tempfile.mkdtemp(), "pid")
        with open(os.devnull, "w") as out, self.assertRaises(subprocess.TimeoutExpired):
            ep._run_pip(["sh", "-c", f"sleep 300 & echo $! > {pidfile}; wait"], out, idle_timeout=1)
        time.sleep(0.3)
        with open(pidfile) as fh:
            pid = int(fh.read())
        try:
            with open(f"/proc/{pid}/stat") as fh:
                alive = fh.read().split()[2] != "Z"
        except OSError:
            alive = False
        if alive:
            os.kill(pid, signal.SIGKILL)
        self.assertFalse(alive, "the grandchild survived the timeout")

    def test_failure_raises_called_process_error(self):
        ep = importlib.import_module("entrypoint")
        with open(os.devnull, "w") as out, self.assertRaises(subprocess.CalledProcessError):
            ep._run_pip(["sh", "-c", "exit 3"], out, idle_timeout=10)


class InstallMarkerTest(unittest.TestCase):
    """U2: the .ok marker is durable only after what pip wrote is."""

    def test_ok_written_after_a_sync(self):
        ep = entrypoint_for(self, tempfile.mkdtemp())
        os.makedirs(ep.STATE_DIR, exist_ok=True)
        order = []

        def fake_run(cmd, **kw):
            os.makedirs(cmd[-1], exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0)

        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b""
        real_open = open

        def spy_open(path, *a, **kw):
            if str(path).endswith(".ok"):
                order.append("ok")
            return real_open(path, *a, **kw)

        with mock.patch.object(ep.subprocess, "run", fake_run), mock.patch.object(ep, "_run_pip"), \
                mock.patch.object(ep.urllib.request, "urlopen", return_value=resp), \
                mock.patch.object(ep.os, "sync", side_effect=lambda: order.append("sync")), mock.patch("builtins.open", spy_open):
            self.assertTrue(ep.install(B))
        self.assertEqual(order[:2], ["sync", "ok"])


CHILD = r"""
import importlib.abc, os, runpy, sys, time
class Stall(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "homeassistant":
            print("stalled", flush=True)
            time.sleep(60)
        return None
sys.meta_path.insert(0, Stall())
runpy.run_path(os.path.join(sys.argv[1], "run.py"), run_name="__main__")
"""


def run_child(cfg, **env):
    return subprocess.Popen([sys.executable, "-c", CHILD, ROOT], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env={**os.environ, "PYTHONPATH": ROOT, "HRI_CONFIG": cfg, "PYTHONDONTWRITEBYTECODE": "1", **env})


class EarlySignalTest(unittest.TestCase):
    """HRI-03 / HRI-04: run.py before its Home Assistant imports."""

    def ha(self, cfg, data=None):
        path = os.path.join(cfg, "integration_manager", "ha.json")
        if data is not None:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_sigterm_during_imports_exits_0_and_takes_back_the_count(self):
        cfg = tempfile.mkdtemp()
        self.ha(cfg, {"current": B, "boot_failures": 2})
        proc = run_child(cfg)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "stalled")
            proc.send_signal(signal.SIGTERM)
            _out, err = proc.communicate(timeout=15)
        finally:
            proc.kill()
        self.assertEqual(proc.returncode, 0, err[-2000:])
        self.assertEqual(self.ha(cfg)["boot_failures"], 1)

    def test_invalid_tracemalloc_value_does_not_crash_the_boot(self):
        cfg = tempfile.mkdtemp()
        proc = run_child(cfg, HRI_TRACEMALLOC="yes")
        try:
            line = proc.stdout.readline().strip()
            proc.send_signal(signal.SIGKILL)
            _out, err = proc.communicate(timeout=15)
        finally:
            proc.kill()
        self.assertEqual(line, "stalled", err[-2000:])
        self.assertIn("HRI_TRACEMALLOC", err)

    def test_importing_run_does_not_take_over_signals(self):
        import run  # noqa: F401 - imported by the other tests already; the handlers are only for the process run.py is

        self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)


class RunPyTest(unittest.IsolatedAsyncioTestCase):
    async def test_debugpy_binds_loopback_by_default_and_is_pinned(self):
        import run

        calls = []
        fake = types.SimpleNamespace(listen=lambda addr: calls.append(addr))
        with mock.patch.dict(sys.modules, {"debugpy": fake}), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HRI_DEBUGPY_HOST", None)
            self.assertTrue(run._start_debugpy("5678")["listening"])
            os.environ["HRI_DEBUGPY_HOST"] = "0.0.0.0"
            run._start_debugpy("5678")
        self.assertEqual(calls, [("127.0.0.1", 5678), ("0.0.0.0", 5678)])
        import inspect

        src = inspect.getsource(run)
        self.assertRegex(src, r"debugpy==\d")
        self.assertIn("async_add_executor_job(_start_debugpy", src)

    async def test_quiet_loggers_must_be_a_list_of_strings(self):
        import run

        cfg = tempfile.mkdtemp()
        os.makedirs(os.path.join(cfg, "integration_manager"))
        with open(os.path.join(cfg, "integration_manager", "state.json"), "w", encoding="utf-8") as fh:
            json.dump({"domain": "demo"}, fh)
        with open(os.path.join(cfg, "integration_manager", "registry.json"), "w", encoding="utf-8") as fh:
            json.dump({"integrations": {"demo": {"quiet_loggers": "my_lib"}}}, fh)
        with mock.patch.object(run, "CONFIG_DIR", cfg):
            self.assertEqual(run._quiet_loggers(), ["custom_components.demo"])
            with open(os.path.join(cfg, "integration_manager", "registry.json"), "w", encoding="utf-8") as fh:
                json.dump({"integrations": {"demo": {"quiet_loggers": ["my_lib", 3, ""]}}}, fh)
            self.assertEqual(run._quiet_loggers(), ["my_lib"])

    async def test_mark_boot_ok_records_the_proven_version(self):
        import run

        cfg = tempfile.mkdtemp()
        path = os.path.join(cfg, "integration_manager", "ha.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"current": run.HA_VERSION, "boot_failures": 1, "proven": "2020.1.0"}, fh)
        loop_thread = []
        real = run._mark_boot_ok

        def spy():
            import threading

            loop_thread.append(threading.current_thread() is threading.main_thread())
            real()

        event = asyncio.Event()
        event.set()
        with mock.patch.object(run, "CONFIG_DIR", cfg), mock.patch.object(run, "_boot_settled", False), mock.patch.object(run, "_mark_boot_ok", spy):
            await run._mark_boot_ok_after(event)
        self.assertEqual(loop_thread, [False])  # the fsyncs are not on the loop
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
        self.assertEqual((state["proven"], state["boot_failures"]), (run.HA_VERSION, 0))


class HaUpdaterAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_async_set_desired_writes_in_the_executor(self):
        import threading

        from custom_components.integration_manager.ha_updater import HaUpdater

        cfg = tempfile.mkdtemp()
        os.makedirs(os.path.join(cfg, "integration_manager"))
        loop = asyncio.get_running_loop()
        hass = types.SimpleNamespace(config=types.SimpleNamespace(path=lambda *p: os.path.join(cfg, *p), config_dir=cfg),
                                     async_add_executor_job=lambda f, *a: loop.run_in_executor(None, f, *a))
        up = HaUpdater(hass)
        threads = []
        real = up.set_desired
        with mock.patch.object(up, "validate", mock.AsyncMock()), \
                mock.patch.object(up, "set_desired", side_effect=lambda *a, **k: (threads.append(threading.current_thread() is threading.main_thread()), real(*a, **k))[1]):
            state = await up.async_set_desired(" 2026.9.2 ")
        self.assertEqual((state["desired"], threads), (B, [False]))


class SettingsBoolTest(unittest.TestCase):
    def test_strings_are_coerced(self):
        from custom_components.integration_manager.settings import Settings

        s = Settings(tempfile.mkdtemp())
        for raw, want in (("false", False), ("0", False), ("no", False), ("off", False), ("", False), ("true", True), ("1", True),
                          (True, True), (False, False), (0, False), (1, True)):
            s.data["auto_rollback"] = raw
            self.assertIs(s.bool_("auto_rollback"), want, raw)
        s.data["auto_rollback"] = "maybe"
        self.assertIs(s.bool_("auto_rollback"), True)  # the default


class LogBufferBytesTest(unittest.TestCase):
    def test_rotation_counts_bytes(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "process.log")
        handler = logbuffer.FileLogHandler(path, max_bytes=1400)
        self.addCleanup(handler.close)
        rec = logging.LogRecord("x", logging.INFO, __file__, 0, "ä" * 300, (), None)  # a line of ~730 bytes but ~430 characters
        handler.handle(rec)
        handler.handle(rec)  # ~1460 bytes would exceed max_bytes (in characters it would not): rotated first
        self.assertTrue(os.path.exists(path + ".1"))
        self.assertLessEqual(os.path.getsize(path), 1400)


if __name__ == "__main__":
    unittest.main()
