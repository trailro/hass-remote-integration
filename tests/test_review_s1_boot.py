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


class InPlaceRestartTest(unittest.TestCase):
    """S1-1 (a): run.py starts the image's entrypoint again instead of exiting, only for a restart asked for from the
    manager, only in the app, never after a stop signal."""

    def setUp(self):
        for name, value in (("_restart_asked", None), ("_boot_signalled", False)):
            patch = mock.patch.object(run, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_what_runs_again_is_the_images_cmd(self):
        with open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8") as fh:
            cmd = re.search(r"^CMD (\[.*\])$", fh.read(), re.M).group(1)
        self.assertEqual(json.loads(cmd), list(run.ENTRYPOINT_ARGV))

    def test_only_an_asked_restart_in_the_app_with_no_stop_signal(self):
        from homeassistant.helpers.signal import KEY_HA_STOP

        hass = SimpleNamespace(data={})
        with mock.patch.dict(os.environ, {"HRI_APP": "1"}):
            self.assertFalse(run._restart_in_place(), "nothing asked for a restart: a plain stop")
            run._ask_restart(hass)
            self.assertTrue(run._restart_in_place())
            hass.data[KEY_HA_STOP] = object()  # Home Assistant's signal handler ran: the Supervisor stops the app
            self.assertFalse(run._restart_in_place())
            hass.data.clear()
            with mock.patch.object(run, "_boot_signalled", True):
                self.assertFalse(run._restart_in_place())
        with mock.patch.dict(os.environ):
            os.environ.pop("HRI_APP", None)
            self.assertFalse(run._restart_in_place(), "Docker: the restart policy starts the container again")

    def _exit(self, app):
        calls = []
        with mock.patch.dict(os.environ, {"HRI_APP": "1"} if app else {}), \
                mock.patch.object(run.os, "closerange", lambda *a: calls.append(("closerange",) + a)), \
                mock.patch.object(run.os, "chdir", lambda d: calls.append(("chdir", d))), \
                mock.patch.object(run.os, "execvp", lambda f, argv: calls.append(("execvp", f, argv))), \
                mock.patch.object(run.os, "_exit", lambda rc: calls.append(("_exit", rc))), \
                mock.patch.object(run.logbuffer, "stop_queue", return_value=False):
            if not app:
                os.environ.pop("HRI_APP", None)
            run._ask_restart(SimpleNamespace(data={}))
            run._exit(0)
        return calls

    def test_the_app_execs_the_entrypoint(self):
        calls = self._exit(app=True)
        self.assertEqual([c[0] for c in calls], ["closerange", "chdir", "execvp", "_exit"])  # _exit only if the exec fails
        self.assertEqual(calls[1], ("chdir", "/app"))
        self.assertEqual(calls[2], ("execvp", "python", ["python", "/app/entrypoint.py"]))

    def test_docker_still_exits(self):
        self.assertEqual(self._exit(app=False), [("_exit", 0)])

    def test_the_new_process_gets_the_environment_and_no_descriptor(self):
        """A real exec: the listening socket and the files of the old process are gone, one a C library left
        inheritable too; HRI_APP and the options stay, and it starts in the entrypoint's folder."""
        tmp = _tmp(self)
        probe = os.path.join(tmp, "probe.py")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write(
                "import json, os, sys\n"
                "def is_open(fd):\n"
                "    try:\n"
                "        os.fstat(fd)\n"
                "        return True\n"
                "    except OSError:\n"
                "        return False\n"
                "print(json.dumps({'open': [fd for fd in map(int, sys.argv[1:]) if is_open(fd)], 'cwd': os.getcwd(),"
                " 'env': {k: os.environ.get(k) for k in ('HRI_APP', 'HRI_PASSWORD', 'SUPERVISOR_TOKEN')}}))\n")
        child = (
            "import os, socket, sys; from types import SimpleNamespace; sys.path.insert(0, sys.argv[1]); import run\n"
            "srv = socket.socket(); srv.bind(('127.0.0.1', 0)); srv.listen()\n"
            "kept = open(sys.argv[2], 'rb')\n"
            "leaky = os.open(sys.argv[2], os.O_RDONLY); os.set_inheritable(leaky, True)\n"
            "run.ENTRYPOINT_ARGV = (sys.executable, sys.argv[2], str(srv.fileno()), str(kept.fileno()), str(leaky))\n"
            "run._ask_restart(SimpleNamespace(data={}))\n"
            "run._exit(0)\n"
        )
        env = {**os.environ, "HRI_APP": "1", "HRI_PASSWORD": "pw-from-the-options", "HRI_CONFIG": tmp}
        env.pop("SUPERVISOR_TOKEN", None)
        proc = subprocess.run([sys.executable, "-c", child, ROOT, probe], env=env, capture_output=True, text=True,
                              timeout=120, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertNotIn("restart in place failed", proc.stderr)
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(out["open"], [], "a descriptor of the old process reached the new one")
        self.assertEqual(out["cwd"], os.path.realpath(tmp))
        self.assertEqual(out["env"], {"HRI_APP": "1", "HRI_PASSWORD": "pw-from-the-options", "SUPERVISOR_TOKEN": None})


class RestartAsksForInPlaceTest(unittest.IsolatedAsyncioTestCase):
    """S1-1 (a): installer.restart - the one path every restart HRI asks for takes (UI, API, MQTT, the health
    watchdog, the restart after a version change) - tells run.py, before the stop starts."""

    def setUp(self):
        _events(self)

    async def test_restart_says_it_is_a_restart(self):
        order = []
        hass = _hass(data={"hri_restart_in_place": lambda: order.append("asked")})
        stop = hass.async_stop

        async def async_stop():
            order.append("stop")
            await stop()

        hass.async_stop = async_stop
        ins = _installer(self, hass)
        self.assertTrue((await asyncio.wait_for(ins.restart(), 5))["ok"])
        await asyncio.sleep(0)
        self.assertEqual(order, ["asked", "stop"])

    async def test_run_py_publishes_the_hook(self):
        with open(os.path.join(ROOT, "run.py"), encoding="utf-8") as fh:
            self.assertIn('hass.data["hri_restart_in_place"] = lambda: _ask_restart(hass)', fh.read())


class AppWatchdogTest(unittest.TestCase):
    """S1-1 (b): the entrypoint turns the app's Watchdog on once per volume, with the app's own token, before it
    drops the token; a failure is logged and never stops the boot."""

    def setUp(self):
        self.tmp = _tmp(self)
        self.ep = entrypoint_for(self, self.tmp)
        os.makedirs(self.ep.STATE_DIR)
        self.lines = []
        patch = mock.patch.object(self.ep, "log", self.lines.append)
        patch.start()
        self.addCleanup(patch.stop)

    def test_first_start_turns_it_on_and_records_it(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b'{"result": "ok", "data": {}}'
        with mock.patch.object(self.ep.urllib.request, "urlopen", return_value=resp) as urlopen:
            self.ep.enable_app_watchdog("t0ken-secret")
            self.ep.enable_app_watchdog("t0ken-secret")  # once per volume: the marker stops the second call
        urlopen.assert_called_once()
        request = urlopen.call_args.args[0]
        self.assertEqual((request.full_url, request.get_method()), ("http://supervisor/addons/self/options", "POST"))
        self.assertEqual(json.loads(request.data), {"watchdog": True})  # nothing else: the options stay as they are
        self.assertEqual(request.get_header("Authorization"), "Bearer t0ken-secret")
        self.assertLessEqual(urlopen.call_args.kwargs["timeout"], 10)
        self.assertTrue(os.path.isfile(self.ep.APP_WATCHDOG_MARKER))
        self.assertFalse([line for line in self.lines if "t0ken" in line])

    def test_turned_off_later_stays_off(self):
        with open(self.ep.APP_WATCHDOG_MARKER, "w", encoding="utf-8") as fh:
            fh.write("2026-09-26T10:00:00\n")
        with mock.patch.object(self.ep.urllib.request, "urlopen") as urlopen:
            self.ep.enable_app_watchdog("t0ken-secret")
        urlopen.assert_not_called()

    def test_a_failure_is_logged_and_tried_at_the_next_start(self):
        refused = urllib.error.HTTPError("http://supervisor/addons/self/options", 403, "Forbidden", {}, None)
        self.addCleanup(refused.close)
        for err in (urllib.error.URLError("Name or service not known"), TimeoutError("timed out"), refused):
            with self.subTest(err=type(err).__name__):
                self.lines.clear()
                with mock.patch.object(self.ep.urllib.request, "urlopen", side_effect=err):
                    self.ep.enable_app_watchdog("t0ken-secret")  # never raises
                self.assertFalse(os.path.exists(self.ep.APP_WATCHDOG_MARKER))
                self.assertTrue(any("Watchdog could not be turned on" in line for line in self.lines), self.lines)
                self.assertFalse([line for line in self.lines if "t0ken" in line])

    def test_main_uses_the_token_before_it_is_gone(self):
        options = os.path.join(self.tmp, "options.json")
        with open(options, "w", encoding="utf-8") as fh:
            json.dump({"password": "pw"}, fh)
        seen = []

        def enable(token):
            seen.append((token, os.environ.get("SUPERVISOR_TOKEN")))

        with mock.patch.dict(os.environ, {"SUPERVISOR_TOKEN": "t0ken-secret"}), \
                mock.patch.object(self.ep, "APP_OPTIONS_FILE", options), \
                mock.patch.object(self.ep, "enable_app_watchdog", enable), \
                mock.patch.object(self.ep, "read_app_watchdog", lambda token: None), \
                mock.patch.object(self.ep, "_prepare", mock.Mock(side_effect=SystemExit(7))), \
                mock.patch.object(self.ep, "start_status_server", lambda: None), \
                mock.patch.object(self.ep, "restrict_umask", lambda: 0):
            with self.assertRaises(SystemExit):
                self.ep.main()
        self.assertEqual(seen, [("t0ken-secret", None)], "called with the token, which the environment no longer holds")

    def test_the_restarted_entrypoint_is_still_the_app_and_asks_nothing(self):
        """The in-place restart runs the entrypoint again in the environment the first run cleaned: no token, the
        options' variables and HRI_APP kept; apply_app_options returns None and main goes on."""
        options = os.path.join(self.tmp, "options.json")
        with open(options, "w", encoding="utf-8") as fh:
            json.dump({"password": ""}, fh)
        env = {"HRI_APP": "1", "HRI_PASSWORD": "pw"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(self.ep, "APP_OPTIONS_FILE", options), \
                mock.patch.object(self.ep, "enable_app_watchdog") as enable, \
                mock.patch.object(self.ep, "_prepare", mock.Mock(side_effect=SystemExit(7))) as prepare, \
                mock.patch.object(self.ep, "start_status_server", lambda: None), \
                mock.patch.object(self.ep, "restrict_umask", lambda: 0):
            os.environ.pop("SUPERVISOR_TOKEN", None)
            os.environ.pop("HASSIO_TOKEN", None)
            with self.assertRaises(SystemExit) as ctx:
                self.ep.main()
            self.assertEqual((os.environ["HRI_APP"], os.environ["HRI_PASSWORD"]), ("1", "pw"))
        self.assertEqual(ctx.exception.code, 7, "reached the boot, not an exit of its own")
        prepare.assert_called_once()
        enable.assert_not_called()


class AppWatchdogRestartModeTest(unittest.TestCase):
    """With the app's Watchdog on, a restart HRI asks for ends the process and the Supervisor starts a fresh container:
    an exec in place gets no HEALTHCHECK start period, so a slow install after it could mark the container unhealthy
    and have the Supervisor restart it mid-install.  The entrypoint reads the toggle at boot, while it has the token,
    and passes it on as HRI_APP_WATCHDOG; unknown restarts in place, as with the toggle off."""

    def setUp(self):
        self.tmp = _tmp(self)
        self.ep = entrypoint_for(self, self.tmp)
        os.makedirs(self.ep.STATE_DIR)
        self.lines = []
        for target, name, value in ((self.ep, "log", self.lines.append), (run, "_restart_asked", None),
                                    (run, "_boot_signalled", False)):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def _answer(body):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = body
        return resp

    def test_it_reads_the_toggle_with_the_apps_token(self):
        for watchdog in (True, False):
            with self.subTest(watchdog=watchdog):
                body = json.dumps({"result": "ok", "data": {"watchdog": watchdog, "slug": "x"}}).encode()
                with mock.patch.object(self.ep.urllib.request, "urlopen", return_value=self._answer(body)) as urlopen:
                    self.assertIs(self.ep.read_app_watchdog("t0ken-secret"), watchdog)
                request = urlopen.call_args.args[0]
                self.assertEqual((request.full_url, request.get_method()), ("http://supervisor/addons/self/info", "GET"))
                self.assertEqual(request.get_header("Authorization"), "Bearer t0ken-secret")
                self.assertLessEqual(urlopen.call_args.kwargs["timeout"], 10)

    def test_unknown_is_none_and_logged_without_the_token(self):
        refused = urllib.error.HTTPError("http://supervisor/addons/self/info", 403, "Forbidden", {}, None)
        self.addCleanup(refused.close)
        answers = [urllib.error.URLError("Name or service not known"), TimeoutError("timed out"), refused,
                   self._answer(b"not json"), self._answer(b'{"result": "ok", "data": {}}'),
                   self._answer(b'{"result": "ok", "data": {"watchdog": "yes"}}')]
        for answer in answers:
            with self.subTest(answer=answer):
                self.lines.clear()
                kwargs = {"side_effect": answer} if isinstance(answer, BaseException) else {"return_value": answer}
                with mock.patch.object(self.ep.urllib.request, "urlopen", **kwargs):
                    self.assertIsNone(self.ep.read_app_watchdog("t0ken-secret"))  # never raises
                self.assertTrue(any("Watchdog setting could not be read" in line for line in self.lines), self.lines)
                self.assertFalse([line for line in self.lines if "t0ken" in line])
        with mock.patch.object(self.ep.urllib.request, "urlopen") as urlopen:
            self.assertIsNone(self.ep.read_app_watchdog(""))
        urlopen.assert_not_called()

    def _main(self, env, read):
        options = os.path.join(self.tmp, "options.json")
        with open(options, "w", encoding="utf-8") as fh:
            json.dump({"password": "pw"}, fh)
        order, seen = [], {}

        def prepare():
            seen["var"] = os.environ.get("HRI_APP_WATCHDOG")
            raise SystemExit(7)

        with mock.patch.dict(os.environ, env), \
                mock.patch.object(self.ep, "APP_OPTIONS_FILE", options), \
                mock.patch.object(self.ep, "enable_app_watchdog", lambda token: order.append(("enable", token))), \
                mock.patch.object(self.ep, "read_app_watchdog", lambda token: order.append(("read", token)) or read), \
                mock.patch.object(self.ep, "_prepare", prepare), \
                mock.patch.object(self.ep, "start_status_server", lambda: None), \
                mock.patch.object(self.ep, "restrict_umask", lambda: 0):
            if "SUPERVISOR_TOKEN" not in env:
                os.environ.pop("SUPERVISOR_TOKEN", None)
                os.environ.pop("HASSIO_TOKEN", None)
            with self.assertRaises(SystemExit):
                self.ep.main()
        return order, seen["var"]

    def test_main_reads_it_after_turning_it_on_and_exports_it(self):
        for read, var in ((True, "1"), (False, "0"), (None, None)):
            with self.subTest(read=read):
                order, seen = self._main({"SUPERVISOR_TOKEN": "t0ken-secret", "HRI_APP_WATCHDOG": "stale"}, read)
                self.assertEqual(order, [("enable", "t0ken-secret"), ("read", "t0ken-secret")])
                self.assertEqual(seen, var)

    def test_the_restarted_entrypoint_keeps_what_it_inherits(self):
        for inherited in ("0", "1"):
            with self.subTest(inherited=inherited):
                order, seen = self._main({"HRI_APP": "1", "HRI_APP_WATCHDOG": inherited}, True)
                self.assertEqual(order, [], "no token: nothing is asked")
                self.assertEqual(seen, inherited)

    def test_run_py_restarts_in_place_unless_the_watchdog_is_known_on(self):
        run._ask_restart(SimpleNamespace(data={}))
        for var, in_place in (("1", False), ("0", True), (None, True)):
            with self.subTest(var=var), mock.patch.dict(os.environ, {"HRI_APP": "1"}):
                os.environ.pop("HRI_APP_WATCHDOG", None)
                if var is not None:
                    os.environ["HRI_APP_WATCHDOG"] = var
                self.assertIs(run._restart_in_place(), in_place)

    def test_with_the_watchdog_on_the_process_ends(self):
        calls = []
        with mock.patch.dict(os.environ, {"HRI_APP": "1", "HRI_APP_WATCHDOG": "1"}), \
                mock.patch.object(run.os, "execvp", lambda f, argv: calls.append("execvp")), \
                mock.patch.object(run.os, "_exit", lambda rc: calls.append(("_exit", rc))), \
                mock.patch.object(run.logbuffer, "stop_queue", return_value=False):
            run._ask_restart(SimpleNamespace(data={}))
            run._exit(0)
        self.assertEqual(calls, [("_exit", 0)], "the Supervisor's Watchdog starts a fresh container")


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
