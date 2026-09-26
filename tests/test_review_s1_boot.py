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
