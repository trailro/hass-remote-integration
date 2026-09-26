"""End-to-end run on the 0.25.0 release candidate.

E1  with HRI_DEBUG=1 Home Assistant 2026.5.0 could not boot: run.py turned on the blocking-call detector before the
    HomeAssistant object existed, and creating it imports dateutil.relativedelta on the loop; reporting that import
    looks the integration up in hass.data, which the loader had not set up yet (KeyError: 'integrations').
E2  after a downgrade with a clean start every device has a new id: the new configs went out while the old ones were
    still retained with the same unique ids, and the main HA refused them until the orphan sweep, five minutes later.
E3  _rollback_full read restore-pending.json on the event loop.
E4  the message of a damaged mqtt_rules.json did not name Reconnect.
E5  a start the never-older guard refused exited 1: a restart every few seconds, the reason only in the log.
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


REASON = ("Home Assistant 2026.8.3 was not started: the configuration on this volume was last written by Home Assistant "
          "2026.9.2, which an older version cannot read")


class RefusedStartHoldsTest(unittest.TestCase):
    """E5: a start refused by the never-older guard exited 1, and Docker's restart policy (or the app's Watchdog)
    restarted it every few seconds with the reason only in the log.  It now waits with the status page up."""

    def setUp(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        env = {k: v for k, v in os.environ.items() if k not in ("HRI_PASSWORD", "HRI_PASSWORD_FILE")}
        patcher = mock.patch.dict(os.environ, {**env, "HRI_PORT": str(self.port)}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ep = entrypoint_for(self, self.cfg)
        os.makedirs(self.ep.STATE_DIR)

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as err:
            with err:
                return err.code, err.read().decode()

    def during_hold(self):
        seen = {}

        def sleep(_s):
            seen.update(alive=self.get("/api/alive"), page=self.get("/"), api=self.get("/api/status"))
            raise SystemExit(128 + signal.SIGTERM)  # what _on_sigterm raises

        with mock.patch.object(self.ep.time, "sleep", sleep), self.assertRaises(SystemExit):
            self.ep.hold_refused_boot(REASON)
        with self.assertRaises(OSError):  # its own status server is gone with the wait
            socket.create_connection(("127.0.0.1", self.port), timeout=2).close()
        return seen

    def test_the_page_says_why_and_that_a_restart_is_needed(self):
        seen = self.during_hold()
        self.assertEqual(seen["alive"][0], 200, "a watchdog would restart it")
        code, body = seen["page"]
        self.assertEqual(code, 503)
        self.assertIn("its start was refused", body)
        self.assertIn("last written by Home Assistant 2026.9.2", body)
        self.assertIn("restart the container (or the app)", body)
        status = json.loads(seen["api"][1])
        self.assertFalse(status["installing"])
        self.assertIn("2026.9.2", status["error"])

    def test_with_a_password_only_that_the_start_was_refused(self):
        with mock.patch.dict(os.environ, {"HRI_PASSWORD": "pw"}):
            seen = self.during_hold()
        self.assertEqual(seen["alive"][0], 200)
        _code, body = seen["page"]
        self.assertIn("its start was refused", body)
        self.assertNotIn("2026.", body)
        status = json.loads(seen["api"][1])
        self.assertEqual(set(status), {"installing", "error"})
        self.assertIn("refused", status["error"])
        self.assertNotIn("2026.", status["error"])

    def test_the_boot_server_is_used_when_there_is_one(self):
        with mock.patch.object(self.ep, "_boot_server", object()), \
                mock.patch.object(self.ep, "start_status_server") as start, \
                mock.patch.object(self.ep.time, "sleep", side_effect=SystemExit(143)), self.assertRaises(SystemExit):
            self.ep.hold_refused_boot(REASON)
        start.assert_not_called()


class RefusedStartUntilSigtermTest(unittest.TestCase):
    """E5, the whole entrypoint in a process: the refused start keeps running with /api/alive at 200, ha.json keeps
    last_error, and SIGTERM ends it at once with the code of a stop."""

    def test_waits_then_stops_on_sigterm(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cfg, True)
        for rel, text in ((".HA_VERSION", "2026.9.2\n"), (os.path.join("integration_manager", "ha.json"),
                                                            json.dumps({"desired": "2026.9.2", "current": "2026.9.2", "proven": "2026.9.2"}))):
            os.makedirs(os.path.dirname(os.path.join(cfg, rel)), exist_ok=True)
            with open(os.path.join(cfg, rel), "w", encoding="utf-8") as fh:
                fh.write(text)
        os.makedirs(os.path.join(cfg, ".storage"))
        _venv(cfg, "2026.8.3")  # the newest venv here, older than the configuration: 2026.9.2 does not install
        child = textwrap.dedent(f"""
            import ast, sys
            from unittest import mock
            sys.path.insert(0, {ROOT!r})
            import entrypoint as ep

            patches = [mock.patch.object(ep, "latest_stable", return_value=None), mock.patch.object(ep, "fits_this_python", return_value=True),
                       mock.patch.object(ep, "ensure_apt_packages", lambda state: None),
                       mock.patch.object(ep, "ensure_extra_requirements", lambda version: None),
                       mock.patch.object(ep, "install", return_value=False), mock.patch.object(ep, "apply_app_options", return_value=None)]
            for p in patches:
                p.start()
            tree = ast.parse(open(ep.__file__).read())
            block = next(n for n in tree.body if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test))
            exec(compile(ast.Module(body=block.body, type_ignores=[]), ep.__file__, "exec"), ep.__dict__)
        """)
        env = {k: v for k, v in os.environ.items() if k not in ("HRI_PASSWORD", "HRI_PASSWORD_FILE", "HRI_APP", "SUPERVISOR_TOKEN")}
        env.update(HRI_CONFIG=cfg, HRI_PORT=str(port), HA_VERSION_LATEST="0", HA_VERSION_DEFAULT="2026.8.3", HA_VERSION_MIN="")
        proc = subprocess.Popen([sys.executable, "-c", child], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline, page = time.monotonic() + 30, None
            while time.monotonic() < deadline and proc.poll() is None:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/alive", timeout=2) as resp:
                        alive = resp.status
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2):
                        pass
                except urllib.error.HTTPError as err:
                    with err:
                        body = err.read().decode()
                    if "its start was refused" in body:
                        page = body
                        break
                except OSError:
                    pass
                time.sleep(0.1)
            self.assertIsNone(proc.poll(), "the refused start exited: a restart policy would loop it")
            self.assertEqual(alive, 200)
            self.assertIn("last written by Home Assistant 2026.9.2", page)
            time.sleep(1)
            self.assertIsNone(proc.poll(), "still waiting")
            with open(os.path.join(cfg, "integration_manager", "ha.json"), encoding="utf-8") as fh:
                self.assertIn("last written by Home Assistant 2026.9.2", json.load(fh)["last_error"])
            proc.send_signal(signal.SIGTERM)
            out, _err = proc.communicate(timeout=15)
            self.assertEqual(proc.returncode, 128 + signal.SIGTERM, out.decode()[-2000:])
        finally:
            proc.kill()
            proc.wait()
            proc.stdout.close()
            proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
