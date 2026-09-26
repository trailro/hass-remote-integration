"""End-to-end run on the 0.25.0 release candidate.

E1  with HRI_DEBUG=1 Home Assistant 2026.5.0 could not boot: run.py turned on the blocking-call detector before the
    HomeAssistant object existed, and creating it imports dateutil.relativedelta on the loop; reporting that import
    looks the integration up in hass.data, which the loader had not set up yet (KeyError: 'integrations').
E2  after a downgrade with a clean start every device has a new id: the new configs went out while the old ones were
    still retained with the same unique ids, and the main HA refused them until the orphan sweep, five minutes later.
E3  _rollback_full read restore-pending.json on the event loop.
E4  the message of a damaged mqtt_rules.json did not name Reconnect.
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


class SupersededBootConfigsTest(StartWindowCase):
    """E2: the configs of the old device ids A and B are retained; the registry now announces the same unique ids under
    the devices C (demo) and D (other).  At the first publish A and B are cleared before C and D go out, without waiting
    for the orphan sweep."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.registry.entities["sensor.c"] = _entry("sensor.c")
        self.registry.entities["sensor.c"].platform = "other"
        self.states["sensor.c"] = State("sensor.c", "5", {"unit_of_measurement": "W"})
        self.pub.hass.config.components = {"demo", "other"}
        self.c, self.d = self.did, disc.device_block(self.pub.hass, None, "other", self.pub.prefix)[0]
        self.a, self.b = f"{self.pub.prefix}olda", f"{self.pub.prefix}oldb"

    def _retained(self, did, *eids, base=None):
        comps = {mp._comp_key(eid): {"platform": "sensor", "unique_id": f"{self.pub.prefix}{eid}", "state_topic": "x"} for eid in eids}
        origin = disc.origin(self.pub.prefix if base is None else base)
        return json.dumps({"device": {"identifiers": [did]}, "origin": origin, "components": comps}).encode()

    def _topic(self, did):
        return self.pub._discovery_topic(did)

    def _last(self, did):
        return [p for t, p in self.published if t == self._topic(did)]

    def _first(self, did):
        return next(i for i, (t, _p) in enumerate(self.published) if t == self._topic(did))

    async def _first_publish(self, found):
        """A new process: nothing announced yet, the broker replays what earlier processes left retained."""
        self.pub._discovery_map, self.pub._blocks, self.pub._last_hash, self.published[:] = {}, {}, {}, []
        self.pub._boot_components, self.pub._boot_removed = None, set()
        self.pub.hass.async_add_executor_job = mock.AsyncMock(return_value=found)
        await self.pub.async_republish_all()

    async def test_cleared_before_the_new_ones_are_announced(self):
        await self._first_publish({self._topic(self.a): self._retained(self.a, "sensor.a", "sensor.b"),
                                   self._topic(self.b): self._retained(self.b, "sensor.c")})
        self.assertEqual((self._last(self.a), self._last(self.b)), ([None], [None]))
        self.assertLess(max(self._first(self.a), self._first(self.b)), min(self._first(self.c), self._first(self.d)))
        self.assertTrue(self.pub._orphan_sweep_due, "no wait for the sweep: it has not run")
        self.published[:] = []
        await self.run_debounced()  # announced again a moment later, as a move seen live
        self.assertEqual(set(json.loads(self._last(self.c)[-1])["components"]), {"sensor_a", "sensor_b"})
        self.assertEqual(set(json.loads(self._last(self.d)[-1])["components"]), {"sensor_c"})
        self.assertEqual((self._last(self.a), self._last(self.b)), ([], []))  # cleared once, and not carried back

    async def test_once_the_entities_are_in_the_registry(self):
        """A clean start: at the first publish the integration has not added its entities yet, so nothing of A is
        announced and A stays; when they are added, A goes before the device they are now on."""
        moved = {eid: (self.registry.entities.pop(eid), self.states.pop(eid)) for eid in ("sensor.a", "sensor.b")}
        await self._first_publish({self._topic(self.a): self._retained(self.a, "sensor.a", "sensor.b")})
        self.assertEqual(self._last(self.a), [])
        for eid, (entry, state) in moved.items():
            self.registry.entities[eid], self.states[eid] = entry, state
        self.published[:] = []
        self.pub._publish_discovery_all()  # the registry burst of the integration adding them
        self.assertEqual(self._last(self.a), [None])
        self.assertLess(self._first(self.a), self._first(self.c))

    async def test_an_entity_still_setting_up_keeps_the_config_for_the_sweep(self):
        self.registry.entities["sensor.slow"] = _entry("sensor.slow")
        self.registry.entities["sensor.slow"].platform = "slow"  # in the registry, its integration still setting up
        await self._first_publish({self._topic(self.a): self._retained(self.a, "sensor.a", "sensor.slow"),
                                   self._topic(self.b): self._retained(self.b, "sensor.c")})
        self.assertEqual(self._last(self.a), [])
        self.assertEqual(self._last(self.b), [None])

    async def test_an_entity_not_in_the_registry_keeps_the_config_for_the_sweep(self):
        await self._first_publish({self._topic(self.a): self._retained(self.a, "sensor.a", "sensor.notyet")})
        self.assertEqual(self._last(self.a), [])

    async def test_a_live_config_is_never_cleared(self):
        await self._first_publish({self._topic(self.c): self._retained(self.c, "sensor.a", "sensor.c"),
                                   self._topic(self.a): self._retained(self.a, "sensor.b")})
        self.assertNotIn(None, self._last(self.c))
        self.assertNotIn(None, self._last(self.d))
        self.assertEqual(self._last(self.a), [None])

    async def test_someone_elses_config_is_left_alone(self):
        await self._first_publish({self._topic(self.a): self._retained(self.a, "sensor.a", "sensor.b", base="hass_other_")})
        self.assertEqual(self._last(self.a), [])


class RollbackReadsPendingOffTheLoopTest(unittest.TestCase):
    """E3: _rollback_full asks whether a restore is scheduled (restore-pending.json) in the executor, with busy held
    (rollback_full holds it), as install and start do."""

    setUp = r10.FullRollbackVersusScheduledChangesTest.setUp  # its installer, backups and executor

    def test_in_the_executor_with_busy_held(self):
        inner, through = self.hass.async_add_executor_job, []

        async def executor(fn, *args):
            through.append(fn)
            return await inner(fn, *args)

        self.hass.async_add_executor_job = executor
        real, busy = backupkit.pending, []

        def pending(cfg):
            busy.append(self.installer.busy)
            return real(cfg)

        with mock.patch.object(backupkit, "pending", pending):
            res = asyncio.run(self.installer.rollback_full("demo"))
        self.assertTrue(res["ok"], res)
        self.assertEqual(busy, [True])
        self.assertEqual(through.count(pending), 1, "read on the event loop")


class DamagedRulesNameReconnectTest(unittest.TestCase):
    """E4: the text of a damaged mqtt_rules.json (rules_error, and connect_error built from it) names the three ways
    to read the file again that docs/mqtt.md and docs/files.md give: Reconnect, saving the settings, a restart."""

    def test_reconnect_is_named(self):
        path = os.path.join(tempfile.mkdtemp(), "mqtt_rules.json")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with self.assertLogs("custom_components.integration_manager.mqtt_rules", "ERROR"):
            problem = MqttRules(path).problem
        self.assertIn("press Reconnect on the MQTT page, save the MQTT settings, or restart", problem)


if __name__ == "__main__":
    unittest.main()
