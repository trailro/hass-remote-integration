"""Review round 3, boot: entrypoint.py, run.py, backupkit.py.

M4  a restore schedule naming its archive with something that is not a plain file name
M5  the manager component swapped in, not deleted and then copied
M6  the stop watchdog a manager restart arms before HA's first stop stage
M7  the pre-boot status server's per-connection timeout
M11 a restore that failed does not cancel a scheduled clean start
C3  no HA_VERSION_DEFAULT literal to drift from the Dockerfile
C4  glob.escape on the set-aside .storage copies
"""

import errno
import http.server
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import backupkit
import run
from tests.fakes import entrypoint_for

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = "2026.8.3"  # the clean start's version
CURRENT = "2026.9.2"  # what runs before it


def _volume(test, prefix="tmp"):
    cfg = tempfile.mkdtemp(prefix=prefix)
    test.addCleanup(shutil.rmtree, cfg, True)
    for d in (".storage", backupkit.STATE_DIR, "custom_components/x"):
        os.makedirs(os.path.join(cfg, d))
    with open(os.path.join(cfg, ".storage", "core.config_entries"), "w", encoding="utf-8") as fh:
        fh.write("the configuration")
    with open(os.path.join(cfg, backupkit.MARKER), "w", encoding="utf-8") as fh:
        fh.write("{}")
    with open(os.path.join(cfg, backupkit.STATE_DIR, "ha.json"), "w", encoding="utf-8") as fh:
        json.dump({"current": CURRENT}, fh)
    return cfg


def _make_venv(cfg, version):
    venv = os.path.join(cfg, f"venv-{version}")
    pkg = os.path.join("lib", f"python{sys.version_info[0]}.{sys.version_info[1]}", "site-packages", "homeassistant")
    for folder in ("bin", pkg):
        os.makedirs(os.path.join(venv, folder))
    for marker in (".ok", "bin/python", os.path.join(pkg, "__init__.py")):
        open(os.path.join(venv, marker), "w").close()


class PendingArchiveNameTest(unittest.TestCase):
    """M4: "zip" in restore-pending.json joined without a type or path check."""

    def setUp(self):
        self.cfg = _volume(self)
        self.backup = backupkit.create(self.cfg, "b", storage_version=CURRENT)["name"]
        backupkit.schedule_restore(self.cfg, self.backup, ["storage"])
        self.meta_path = os.path.join(self.cfg, backupkit.PENDING_META)
        with open(self.meta_path, encoding="utf-8") as fh:
            self.meta = json.load(fh)
        # what a "zip" that walks out of the state dir would reach: present, so only the name check refuses it
        shutil.copy(backupkit.pending_archive(self.cfg), os.path.join(self.cfg, "x.zip"))
        os.makedirs(os.path.join(self.cfg, backupkit.STATE_DIR, "a"))
        shutil.copy(backupkit.pending_archive(self.cfg), os.path.join(self.cfg, backupkit.STATE_DIR, "a", "b.zip"))

    def edit(self, zip_value):
        with open(self.meta_path, "w", encoding="utf-8") as fh:
            json.dump({**self.meta, "zip": zip_value}, fh)

    def test_a_normal_schedule_is_still_found(self):
        self.assertEqual(os.path.basename(backupkit.pending_archive(self.cfg)), self.meta["zip"])
        self.assertTrue(backupkit.pending(self.cfg))

    def test_a_zip_that_is_not_a_plain_file_name_is_no_schedule(self):
        for bad in (123, ["a"], "../x.zip", "a/b.zip", "..", {"a": 1}):
            with self.subTest(zip=bad):
                self.edit(bad)
                self.assertIsNone(backupkit.pending_archive(self.cfg))
                self.assertFalse(backupkit.pending(self.cfg))

    def test_the_boot_drops_it_with_a_message_and_goes_on(self):
        for bad in (123, ["a"], "../x.zip", "a/b.zip"):
            with self.subTest(zip=bad):
                self.edit(bad)
                ep = entrypoint_for(self, self.cfg)
                state, logged = {}, []
                with mock.patch.object(ep, "log", logged.append), mock.patch.object(ep, "save_state", return_value=True):
                    self.assertEqual(ep.apply_config_changes(state, CURRENT, CURRENT), CURRENT)
                self.assertFalse(os.path.exists(self.meta_path), "the schedule stayed: pending forever")
                self.assertFalse(state["last_restore"]["ok"])
                self.assertIn("names no archive file", state["last_restore"]["error"])
                self.assertTrue(any("names no archive file" in line for line in logged), logged)
                self.assertEqual(backupkit.pending_parts(self.cfg), list(backupkit.PARTS))
                with open(os.path.join(self.cfg, ".storage", "core.config_entries"), encoding="utf-8") as fh:
                    self.assertEqual(fh.read(), "the configuration", "the archive outside the state dir was restored")
                self.assertTrue(os.path.isfile(os.path.join(self.cfg, "x.zip")), "only the state dir's own copies are removed")
                with open(self.meta_path, "w", encoding="utf-8") as fh:
                    json.dump(self.meta, fh)  # the next subtest starts from the real schedule again


class ManagerComponentSwapTest(unittest.TestCase):
    """M5: the manager component was deleted before its copy was made."""

    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        self.src = os.path.join(self.cfg, "image", "integration_manager")
        os.makedirs(self.src)
        with open(os.path.join(self.src, "manifest.json"), "w", encoding="utf-8") as fh:
            fh.write('{"version": "new"}')
        self.dst = os.path.join(self.cfg, "custom_components", "integration_manager")
        os.makedirs(self.dst)
        with open(os.path.join(self.dst, "manifest.json"), "w", encoding="utf-8") as fh:
            fh.write('{"version": "old"}')
        for patch in (mock.patch.object(run, "CONFIG_DIR", self.cfg), mock.patch.object(run, "MANAGER_SRC", self.src)):
            patch.start()
            self.addCleanup(patch.stop)

    def version(self):
        with open(os.path.join(self.dst, "manifest.json"), encoding="utf-8") as fh:
            return json.load(fh)["version"]

    def leftovers(self):
        return sorted(n for n in os.listdir(os.path.dirname(self.dst)) if n.startswith("integration_manager."))

    def boot(self):
        run._sweep_deploy_leftovers()
        run._sync_manager_component()

    def test_the_new_copy_replaces_the_old_one(self):
        self.boot()
        self.assertEqual(self.version(), "new")
        self.assertEqual(self.leftovers(), [])

    def test_a_copy_that_fails_keeps_the_one_there(self):
        real = shutil.copytree

        def full_disk(src, dst, *a, **k):
            real(src, dst, *a, **k)  # half written, then the volume is full
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch.object(run.shutil, "copytree", full_disk), self.assertLogs(run._LOGGER, "ERROR"):
            self.boot()
        self.assertEqual(self.version(), "old")
        self.assertEqual(self.leftovers(), [])

    def test_a_kill_between_the_renames_is_recovered_at_the_next_boot(self):
        real = os.rename
        calls = []

        def rename(a, b):
            calls.append(b)
            real(a, b)
            if len(calls) == 1:
                raise KeyboardInterrupt  # docker kill: the old copy is aside, the new one not in place yet

        with mock.patch.object(run.os, "rename", rename), self.assertRaises(KeyboardInterrupt):
            run._sync_manager_component()
        self.assertFalse(os.path.exists(self.dst))
        with self.assertLogs(run._LOGGER, "WARNING"):
            self.boot()
        self.assertEqual(self.version(), "new")
        self.assertEqual(self.leftovers(), [])

    def test_a_kill_during_the_copy_leaves_the_old_one_in_place(self):
        with mock.patch.object(run.shutil, "copytree", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            run._sync_manager_component()
        self.assertEqual(self.version(), "old")

    def test_a_link_is_still_replaced_and_its_target_kept(self):
        target = os.path.join(self.cfg, "checkout")
        shutil.move(self.dst, target)
        os.symlink(target, self.dst)
        self.boot()
        self.assertFalse(os.path.islink(self.dst))
        self.assertEqual(self.version(), "new")
        with open(os.path.join(target, "manifest.json"), encoding="utf-8") as fh:
            self.assertIn("old", fh.read())

    def test_nothing_to_keep_and_no_copy_still_fails_the_boot(self):
        shutil.rmtree(self.dst)
        with mock.patch.object(run.shutil, "copytree", side_effect=OSError(errno.ENOSPC, "full")), self.assertRaises(OSError):
            self.boot()


class ManagerRestartWatchdogBudgetTest(unittest.TestCase):
    """M6: installer.restart arms the watchdog before async_stop, so it has to cover HA's first stage as well."""

    def test_the_manager_path_covers_every_stop_stage(self):
        from homeassistant import core

        stages = (core.STOPPING_STAGE_SHUTDOWN_TIMEOUT + core.STOP_STAGE_SHUTDOWN_TIMEOUT
                  + core.FINAL_WRITE_STAGE_SHUTDOWN_TIMEOUT + core.CLOSE_STAGE_SHUTDOWN_TIMEOUT)
        armed = []
        with mock.patch.object(run, "_arm_stop_watchdog", side_effect=armed.append):
            run._arm_manager_stop_watchdog()
        self.assertGreater(armed[0], stages, "the hard exit cut HA's own bounded stop stages short")
        # a `docker stop` arriving during that restart still gets its 240 s
        self.assertLess(armed[0] + run.WATCHDOG_DRAIN_S + run.LOG_FLUSH_S, 240)
        self.assertEqual(run.STOPPING_STAGE_S, core.STOPPING_STAGE_SHUTDOWN_TIMEOUT)

    def test_it_is_what_run_publishes_for_the_installer(self):
        with open(os.path.join(ROOT, "run.py"), encoding="utf-8") as fh:
            self.assertIn('hass.data["hri_stop_watchdog"] = _arm_manager_stop_watchdog\n', fh.read())


class StatusServerTimeoutTest(unittest.TestCase):
    """M7: a connection that sends nothing held its thread in readline() for good."""

    def test_a_silent_connection_is_closed(self):
        ep = entrypoint_for(self, tempfile.mkdtemp())
        self.assertEqual(ep._StatusHandler.timeout, 30)
        with mock.patch.object(ep._StatusHandler, "timeout", 0.3):
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ep._StatusHandler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(ep.stop_status_server, srv)
            with socket.create_connection(srv.server_address, timeout=5) as sock:
                started = time.monotonic()
                try:
                    data = sock.recv(1)
                except socket.timeout:
                    self.fail("the server still holds a connection that never sent a request")
                self.assertEqual(data, b"")
                self.assertLess(time.monotonic() - started, 4)

    def test_a_request_is_still_answered(self):
        ep = entrypoint_for(self, tempfile.mkdtemp())
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ep._StatusHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(ep.stop_status_server, srv)
        with socket.create_connection(srv.server_address, timeout=5) as sock:
            sock.sendall(b"GET /api/status HTTP/1.1\r\nHost: localhost\r\n\r\n")
            self.assertTrue(sock.recv(64).startswith(b"HTTP/1.0 503"))


class FailedRestoreKeepsCleanStartTest(unittest.TestCase):
    """M11: a restore that failed and changed nothing cancelled the clean start scheduled for the same boot."""

    def setUp(self):
        self.cfg = _volume(self)
        _make_venv(self.cfg, CURRENT)
        self.ep = entrypoint_for(self, self.cfg)
        self.backup = backupkit.create(self.cfg, "pre-change", storage_version=TARGET)["name"]
        os.makedirs(os.path.join(self.ep.STATE_DIR, "import-extracted"))
        with open(os.path.join(self.ep.STATE_DIR, "import-extracted", "summary.json"), "w", encoding="utf-8") as fh:
            json.dump({"type": "ha-downgrade-rebuild"}, fh)
        self.logged = []

    def plan(self, stage="reset", **extra):
        with open(self.ep.REBUILD_FILE, "w", encoding="utf-8") as fh:
            json.dump({"stage": stage, "to": TARGET, "backup": self.backup, **extra}, fh)

    def read_plan(self):
        with open(self.ep.REBUILD_FILE, encoding="utf-8") as fh:
            return json.load(fh)

    def boot(self, *, torn):
        backupkit.schedule_restore(self.cfg, self.backup, ["storage"], for_version=TARGET, force=True)
        if torn:
            with open(backupkit.pending_archive(self.cfg), "r+b") as fh:
                fh.truncate(64)  # fails validation: nothing on the volume is touched
        state = {"current": CURRENT, "change": {"to": TARGET, "mode": "rebuild", "backup": self.backup, "at": "2000-01-01T00:00:00"}}
        with mock.patch.object(self.ep, "log", self.logged.append), mock.patch.object(self.ep, "save_state", return_value=True):
            booted = self.ep.apply_config_changes(state, TARGET, CURRENT)
        return booted, state

    def test_the_clean_start_still_happens(self):
        self.plan()
        booted, state = self.boot(torn=True)
        self.assertFalse(state["last_restore"]["ok"])
        self.assertEqual(booted, TARGET, self.logged)
        self.assertTrue(state["change"]["applied"])
        self.assertEqual(self.read_plan()["stage"], "import")
        self.assertEqual(os.listdir(os.path.join(self.cfg, ".storage")), [])
        self.assertFalse([line for line in self.logged if "clean start dropped" in line], self.logged)

    def test_a_clean_start_already_under_way_keeps_its_plan(self):
        aside = ".storage.pre-rebuild-20260101-000000"
        os.rename(os.path.join(self.cfg, ".storage"), os.path.join(self.cfg, aside))
        os.makedirs(os.path.join(self.cfg, ".storage"))
        self.plan("import", aside=aside, boot_backup="boot.zip")
        booted, state = self.boot(torn=True)
        self.assertFalse(state["last_restore"]["ok"])
        self.assertEqual(booted, TARGET)
        self.assertEqual(self.read_plan()["stage"], "import", "the rebuild after the boot was cancelled")
        self.assertTrue(os.path.isdir(os.path.join(self.cfg, aside)))

    def test_a_restore_that_applied_still_replaces_the_clean_start(self):
        self.plan()
        booted, state = self.boot(torn=False)
        self.assertTrue(state["last_restore"]["ok"])
        self.assertFalse(os.path.isfile(self.ep.REBUILD_FILE))
        self.assertTrue(any("a restore was applied at this boot" in line for line in self.logged), self.logged)


class DefaultVersionTest(unittest.TestCase):
    """C3: a literal fallback for HA_VERSION_DEFAULT had drifted from the Dockerfile the canary moves."""

    def test_no_fallback_literal(self):
        cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cfg, True)
        ep = entrypoint_for(self, cfg)
        with mock.patch.dict(os.environ):
            os.environ.pop("HA_VERSION_DEFAULT")
            sys.modules.pop("entrypoint", None)
            import entrypoint as unset
        self.assertEqual(unset.DEFAULT_VERSION, "")
        self.assertEqual(ep.DEFAULT_VERSION, "2026.8.3")  # what entrypoint_for passes

    def test_the_boot_refuses_to_start_without_it(self):
        cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cfg, True)
        ep = entrypoint_for(self, cfg, HA_VERSION_DEFAULT="")
        logged = []
        with mock.patch.object(ep, "log", logged.append), mock.patch.object(ep, "restrict_umask"), \
                mock.patch.object(ep, "start_status_server", side_effect=AssertionError("started")), \
                mock.patch.object(ep, "_prepare", side_effect=AssertionError("prepared")), \
                self.assertRaises(SystemExit) as ctx:
            ep.main()
        self.assertEqual(ctx.exception.code, 2)
        self.assertTrue(any("HA_VERSION_DEFAULT" in line for line in logged), logged)

    def test_the_image_sets_it(self):
        with open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8") as fh:
            self.assertIn("HA_VERSION_DEFAULT=${HA_VERSION}", fh.read())


class RebuildGlobEscapeTest(unittest.TestCase):
    """C4: an earlier clean start's copy was not removed when the volume's path holds glob characters."""

    def test_an_earlier_copy_goes_on_a_path_with_brackets(self):
        cfg = _volume(self, prefix="vol[1]")
        ep = entrypoint_for(self, cfg)
        os.makedirs(os.path.join(ep.STATE_DIR, "import-extracted"))
        with open(os.path.join(ep.STATE_DIR, "import-extracted", "summary.json"), "w", encoding="utf-8") as fh:
            json.dump({"type": "ha-downgrade-rebuild"}, fh)
        with open(ep.REBUILD_FILE, "w", encoding="utf-8") as fh:
            json.dump({"stage": "reset", "to": TARGET, "backup": "b.zip", "boot_backup": "earlier.zip"}, fh)
        old = os.path.join(cfg, ".storage.pre-rebuild-20200101-000000")
        os.makedirs(old)
        with mock.patch.object(backupkit, "validate", return_value={}), mock.patch.object(ep, "log"):
            self.assertTrue(ep.reset_storage_for_rebuild(TARGET, False))
        self.assertFalse(os.path.isdir(old))


if __name__ == "__main__":
    unittest.main()
