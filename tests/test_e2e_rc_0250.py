"""End-to-end run on the 0.25.0 release candidate.

E1  with HRI_DEBUG=1 Home Assistant 2026.5.0 could not boot: run.py turned on the blocking-call detector before the
    HomeAssistant object existed, and creating it imports dateutil.relativedelta on the loop; reporting that import
    looks the integration up in hass.data, which the loader had not set up yet (KeyError: 'integrations').
"""

import asyncio
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from homeassistant.core import State

import backupkit
import run
from custom_components.integration_manager import discovery as disc
from custom_components.integration_manager import mqtt_publisher as mp
from custom_components.integration_manager.mqtt_rules import MqttRules
from tests import test_r10_rollback_race as r10
from tests.fakes import entrypoint_for
from tests.test_e2e_pub_discovery import StartWindowCase, _entry
from tests.test_review_s1_boot import _venv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DetectorAfterHassTest(unittest.TestCase):
    """E1: the detector is turned on where bootstrap.async_setup_hass turns it on: after the HomeAssistant object and
    the loader's data exist and configuration.yaml is ensured, before the config upgrade."""

    class Stop(Exception):
        pass

    def _boot(self, debug):
        cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cfg, True)
        self.addCleanup(os.chdir, os.getcwd())  # _boot changes into the config dir
        order = []

        def new_hass(_cfg):
            order.append("hass")
            hass = mock.MagicMock()
            hass.data = {run.loader.DATA_PRELOAD_PLATFORMS: []}

            async def executor(fn, *args):
                return fn(*args)

            hass.async_add_executor_job = executor
            return hass

        async def ensure(_hass):
            order.append("config")
            return True

        def upgrade(_hass):
            order.append("upgrade")
            raise self.Stop  # far enough: what follows runs with the detector on either way

        logger = logging.getLogger("custom_components.integration_manager")
        self.addCleanup(logger.setLevel, logger.level)
        with mock.patch.dict(os.environ, {"HRI_DEBUG": "1" if debug else "0"}), \
                mock.patch.object(run, "_run_loop", lambda boot: asyncio.run(boot())), \
                mock.patch.object(run, "_quiet_loggers", return_value=[]), \
                mock.patch.object(run, "_detect_blocking", False, create=True), \
                mock.patch.object(run, "CONFIG_DIR", cfg), \
                mock.patch.object(run, "_install_boot_signal_handlers", lambda *a: None), \
                mock.patch.object(run, "_sweep_deploy_leftovers", lambda: None), \
                mock.patch.object(run, "_sync_manager_component", lambda: None), \
                mock.patch("homeassistant.block_async_io.enable", lambda: order.append("enable")), \
                mock.patch.object(run.core, "HomeAssistant", new_hass), \
                mock.patch.object(run.loader, "async_setup", lambda _hass: order.append("loader")), \
                mock.patch.object(run.conf_util, "async_ensure_config_exists", ensure), \
                mock.patch.object(run.conf_util, "process_ha_config_upgrade", upgrade), \
                mock.patch.dict(os.environ, {"HRI_DEBUGPY": ""}):
            with self.assertRaises(self.Stop):
                run._boot_with_logging()
        return order

    def test_after_hass_and_the_loader(self):
        self.assertEqual(self._boot(True), ["hass", "loader", "config", "enable", "upgrade"])

    def test_off_without_debug(self):
        self.assertEqual(self._boot(False), ["hass", "loader", "config", "upgrade"])


if __name__ == "__main__":
    unittest.main()
