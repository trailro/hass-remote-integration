"""Review of b4cd1a1, boot and process (S1, X-1).

S1-1  In the Home Assistant app every restart HRI asked for left the app stopped: the Supervisor passes no restart
      policy and starts an app again only with its Watchdog toggle on (off by default).  A restart asked for from the
      manager now starts over in place under HRI_APP, and the entrypoint turns the Watchdog on once per volume.
S1-2  After an image rollback across Python versions the boot installed and started an older Home Assistant (the
      newest release for that Python, or the newest venv after a failed install) on a newer configuration.
X-1   A Supervisor restore of a backup taken while a downgrade with restore or clean start was scheduled booted the
      older version on the newer configuration: the change's restore could not happen and no other venv was there.
S1-3  An unreadable settings.json (an OSError, not bad JSON) was replaced by the defaults at the next save.
S1-4  HRI_DEBUG=0 turned debug logging and block_async_io on.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from types import SimpleNamespace
from unittest import mock

import backupkit
import run
from custom_components.integration_manager import settings as settings_mod
from custom_components.integration_manager.manage_views import SettingsView
from tests.fakes import entrypoint_for
from tests.test_camp_restart import _events, _hass, _installer
from tests.test_entrypoint import make_backup
from tests.test_watchdog_settings import _Request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLD, NEW = "2026.8.3", "2026.9.2"


def _tmp(test):
    tmp = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, tmp, True)
    return tmp


def _venv(cfg, version):
    venv = os.path.join(cfg, f"venv-{version}")
    ha_pkg = os.path.join("lib", f"python{sys.version_info[0]}.{sys.version_info[1]}", "site-packages", "homeassistant")
    for folder in ("bin", ha_pkg):
        os.makedirs(os.path.join(venv, folder), exist_ok=True)
    for marker in (".ok", "bin/python", os.path.join(ha_pkg, "__init__.py")):  # what venv_ok looks for
        open(os.path.join(venv, marker), "w").close()


class OlderThanTheConfigurationTest(unittest.TestCase):
    """S1-2 + X-1: one guard right before the boot - Home Assistant is never started on a configuration a newer
    version wrote (.HA_VERSION), unless that configuration was just put there for it or the operator kept it."""

    def setUp(self):
        self.cfg = _tmp(self)
        self.ep = entrypoint_for(self, self.cfg)
        os.makedirs(os.path.join(self.cfg, ".storage"))
        os.makedirs(os.path.join(self.cfg, "integration_manager"))
        self.write(os.path.join(".storage", "core.config_entries"), json.dumps({"from": NEW}))
        self.write(backupkit.MARKER, "{}")
        self.write(".HA_VERSION", NEW + "\n")  # what Home Assistant NEW wrote at its last boot here

    def write(self, rel, text):
        with open(os.path.join(self.cfg, rel), "w", encoding="utf-8") as fh:
            fh.write(text)

    def ha(self, **state):
        self.write(os.path.join("integration_manager", "ha.json"), json.dumps(state))

    def storage(self):
        with open(os.path.join(self.cfg, ".storage", "core.config_entries"), encoding="utf-8") as fh:
            return json.load(fh)["from"]

    def prepare(self, fits=True, newest=None, installs=()):
        lines = []

        def install(version):
            if version in installs:
                _venv(self.cfg, version)
                return True
            return False

        with mock.patch.object(self.ep, "latest_stable", return_value=newest), \
                mock.patch.object(self.ep, "fits_this_python", return_value=fits), \
                mock.patch.object(self.ep, "ensure_apt_packages", lambda state: None), \
                mock.patch.object(self.ep, "ensure_extra_requirements", lambda version: None), \
                mock.patch.object(self.ep, "install", side_effect=install) as installed, \
                mock.patch.object(self.ep, "log", lines.append):
            try:
                python, code = self.ep._prepare(), None
            except SystemExit as err:
                python, code = None, err.code
        with open(os.path.join(self.cfg, "integration_manager", "ha.json"), encoding="utf-8") as fh:
            state = json.load(fh)
        return SimpleNamespace(python=python, code=code, state=state, lines=lines, installed=installed)

    def assert_refused(self, r, wanted):
        self.assertIsNone(r.python, f"Home Assistant {wanted} was started on a configuration {NEW} wrote")
        self.assertEqual(r.code, 1)
        self.assertIn(f"Home Assistant {wanted} was not started", r.state["last_error"])
        self.assertIn(f"last written by Home Assistant {NEW}", r.state["last_error"])
        self.assertTrue(any("was not started" in line for line in r.lines), r.lines)
        self.assertFalse(os.path.lexists(os.path.join(self.cfg, "venv-current")))
        self.assertEqual(self.storage(), NEW)

    def test_x1_a_restored_downgrade_whose_restore_cannot_happen(self):
        # a Supervisor restore brought back ha.json with a downgrade scheduled, but not its restore (restore-pending*
        # and backups/ are not in the app's backup) nor any venv
        change = {"to": OLD, "mode": "restore", "backup": f"pre-ha-{OLD}.zip", "at": "2026-09-20T10:00:00"}
        self.ha(desired=OLD, current=NEW, proven=NEW, change=change)
        r = self.prepare(installs=(OLD,))
        self.assert_refused(r, OLD)
        self.assertEqual(r.state["desired"], OLD, "desired is not changed silently")
        self.assertEqual(r.state["current"], NEW)
        self.assertEqual(r.state["change"], change, "the switch is not marked applied: nothing booted")

    def test_x1_the_same_with_a_clean_start(self):
        change = {"to": OLD, "mode": "rebuild", "backup": f"pre-ha-{OLD}.zip", "at": "2026-09-20T10:00:00"}
        self.ha(desired=OLD, current=NEW, proven=NEW, change=change)
        self.assert_refused(self.prepare(installs=(OLD,)), OLD)

    def test_s1_2_the_newest_release_for_an_older_images_python(self):
        # the image went back to an older Python: NEW has no venv for it and does not support it
        self.ha(desired=NEW, current=NEW, proven=NEW)
        r = self.prepare(fits=False, newest=OLD, installs=(OLD,))
        self.assert_refused(r, OLD)
        self.assertEqual(r.state["desired"], NEW, "the substitute is not recorded: back on the newer image, NEW boots")

    def test_s1_2_the_fallback_after_a_failed_install(self):
        _venv(self.cfg, OLD)
        self.ha(desired=NEW, current=NEW, proven=NEW)
        r = self.prepare(installs=())  # NEW does not install (no network) and OLD is the newest venv here
        self.assert_refused(r, OLD)
        self.assertEqual(r.state["desired"], NEW)

    def test_without_ha_version_the_proven_version_counts(self):
        os.remove(os.path.join(self.cfg, ".HA_VERSION"))
        self.ha(desired=NEW, current=NEW, proven=NEW)
        self.assert_refused(self.prepare(fits=False, newest=OLD, installs=(OLD,)), OLD)

    def test_a_downgrade_with_its_restore_still_boots(self):
        _venv(self.cfg, OLD)
        _venv(self.cfg, NEW)
        make_backup(self.cfg, "old.zip", OLD)
        backupkit.schedule_restore(self.cfg, "old.zip", ["storage"], for_version=OLD)
        self.ha(desired=OLD, current=NEW, proven=NEW,
                change={"to": OLD, "mode": "restore", "backup": "pre.zip", "parts": ["storage"], "at": "2020-01-01T00:00:00"})
        r = self.prepare()
        self.assertEqual(r.python, os.path.join(self.cfg, f"venv-{OLD}", "bin", "python"), r.lines)
        self.assertEqual(self.storage(), OLD)
        self.assertEqual(r.state["current"], OLD)
        self.assertNotIn("_config_for", r.state, "a marker for this boot only")

    def test_a_restore_applied_at_a_boot_that_was_killed_still_counts(self):
        _venv(self.cfg, OLD)
        _venv(self.cfg, NEW)
        self.write(os.path.join(".storage", "core.config_entries"), json.dumps({"from": OLD}))  # restored, then killed
        self.ha(desired=OLD, current=NEW, proven=NEW,
                change={"to": OLD, "mode": "restore", "backup": "pre.zip", "parts": ["storage"], "at": "2026-09-20T10:00:00"},
                last_restore={"ok": True, "for_version": OLD, "parts": ["storage"], "backup": "old.zip", "at": "2026-09-20T10:01:00"})
        self.assertEqual(self.prepare().python, os.path.join(self.cfg, f"venv-{OLD}", "bin", "python"))

    def test_a_downgrade_kept_as_it_is_still_boots(self):
        _venv(self.cfg, OLD)
        _venv(self.cfg, NEW)
        self.ha(desired=OLD, current=NEW, proven=NEW, change={"to": OLD, "mode": "keep", "backup": "pre.zip"})
        r = self.prepare()
        self.assertEqual(r.python, os.path.join(self.cfg, f"venv-{OLD}", "bin", "python"), r.lines)

    def test_a_crash_fallback_with_its_restore_still_boots(self):
        _venv(self.cfg, OLD)
        _venv(self.cfg, NEW)
        make_backup(self.cfg, "pre.zip", OLD)
        self.ha(desired=NEW, current=NEW, previous=OLD, proven=OLD, boot_failures=3,
                change={"to": NEW, "mode": "keep", "backup": "pre.zip", "applied": True, "at": "2020-01-01T00:00:00"})
        r = self.prepare()
        self.assertEqual(r.python, os.path.join(self.cfg, f"venv-{OLD}", "bin", "python"), r.lines)
        self.assertEqual(self.storage(), OLD)
        self.assertEqual(r.state["fallback_from"], NEW)

    def test_an_upgrade_and_the_same_version_boot(self):
        _venv(self.cfg, NEW)
        self.ha(desired=NEW, current=NEW, proven=NEW)
        self.assertEqual(self.prepare().python, os.path.join(self.cfg, f"venv-{NEW}", "bin", "python"))
        self.write(".HA_VERSION", OLD)
        self.ha(desired=NEW, current=OLD, proven=OLD)
        self.assertEqual(self.prepare().python, os.path.join(self.cfg, f"venv-{NEW}", "bin", "python"))


class UnreadableSettingsTest(unittest.TestCase):
    """S1-3: settings.json that exists but cannot be read (a permission, an I/O error) is never replaced by the
    defaults: the tokens and allowed hosts in it would be gone for good."""

    def setUp(self):
        self.dir = _tmp(self)
        self.path = os.path.join(self.dir, "settings.json")
        self.original = json.dumps({"github_token": "ghp_x", "parent_ha_token": "tok", "allowed_hosts": "hri.example"})
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(self.original)
        with mock.patch.object(settings_mod, "open", create=True, side_effect=PermissionError(13, "Permission denied")), \
                mock.patch.object(settings_mod.events, "emit"), self.assertLogs(settings_mod._LOGGER, logging.WARNING):
            self.settings = settings_mod.Settings(self.dir)

    def test_the_load_says_what_happens(self):
        self.assertEqual(self.settings.data, settings_mod.DEFAULTS)
        self.assertIn("cannot be read", self.settings.load_error)
        self.assertIn("no setting is saved", self.settings.load_error)

    def test_a_save_is_refused_and_the_file_kept(self):
        with mock.patch.object(settings_mod.writer, "async_write", mock.AsyncMock()) as write:
            with self.assertRaises(OSError) as ctx:
                asyncio.run(self.settings.async_save())
        write.assert_not_called()
        self.assertIn("not saved", str(ctx.exception))
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), self.original)

    def test_the_settings_page_gets_the_reason(self):
        installer = SimpleNamespace(settings=self.settings, hass=None, _releases_cache={}, scheduler=None)
        with mock.patch.object(settings_mod.writer, "async_write", mock.AsyncMock()) as write:
            out = json.loads(asyncio.run(SettingsView(installer).post(_Request({"backup_keep": 3}))).body.decode())
        write.assert_not_called()
        self.assertFalse(out["ok"])
        self.assertIn("could not be read when the manager started", out["error"])
        self.assertEqual(self.settings.data["backup_keep"], settings_mod.DEFAULTS["backup_keep"], "the change is rolled back")


class DebugFlagTest(unittest.TestCase):
    """S1-4: HRI_DEBUG is read the way settings.bool_ reads a word: 0, false, no, off and empty are off."""

    def _debug(self, value):
        logger = logging.getLogger("custom_components.integration_manager")
        level = logger.level
        self.addCleanup(logger.setLevel, level)
        logger.setLevel(logging.NOTSET)
        with mock.patch.dict(os.environ, {"HRI_DEBUG": value}), \
                mock.patch.object(run, "_run_loop", return_value=0), \
                mock.patch.object(run, "_quiet_loggers", return_value=[]), \
                mock.patch("homeassistant.block_async_io.enable") as enable:
            run._boot_with_logging()
        return enable.called, logger.level == logging.DEBUG

    def test_off_however_it_is_spelled(self):
        for value in ("", "0", "false", "no", "off", " OFF ", "False"):
            with self.subTest(value=value):
                self.assertEqual(self._debug(value), (False, False))

    def test_on(self):
        for value in ("1", "true", "yes", "on"):
            with self.subTest(value=value):
                self.assertEqual(self._debug(value), (True, True))


if __name__ == "__main__":
    unittest.main()
