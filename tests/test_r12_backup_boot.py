"""Review round 12 (m7): the boot's status server answers on the manager port until the exec."""

import json
import os
import shutil
import socket
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

from tests.fakes import entrypoint_for
from tests.test_r4_lifecycle import make_venv

A, B = "2026.8.3", "2026.9.2"


class Stop(BaseException):
    pass


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(port, path="/api/status"):
    """(status, body) of the manager port, or None when nothing listens."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"Host": "localhost"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode()
    except (urllib.error.URLError, ConnectionError):
        return None


class BootBase(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        self.port = _free_port()
        self.ep = entrypoint_for(self, self.cfg, HRI_PORT=str(self.port))
        os.makedirs(self.ep.STATE_DIR, exist_ok=True)
        self.execs = []

        def execv(path, argv):
            self.execs.append((path, _get(self.port)))
            raise Stop()

        for p in (mock.patch.object(self.ep.os, "execv", side_effect=execv), mock.patch.object(self.ep, "restrict_umask"),
                  mock.patch.object(self.ep, "fits_this_python", return_value=True), mock.patch.object(self.ep, "latest_stable", return_value=None),
                  mock.patch.object(self.ep, "ensure_extra_requirements")):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: getattr(self.ep, "_stop_boot_server", lambda: None)())  # never a server left behind by a failing test

    def write(self, state):
        with open(self.ep.HA_FILE, "w", encoding="utf-8") as fh:
            json.dump(state, fh)

    def state(self):
        with open(self.ep.HA_FILE, encoding="utf-8") as fh:
            return json.load(fh)

    def boot(self):
        with self.assertRaises(Stop):
            self.ep.main()
        return os.path.basename(os.path.dirname(os.path.dirname(self.execs[-1][0])))[5:]


class BootStatusServerTest(BootBase):
    """m7: the 503 status page answered only while pip installed Home Assistant; PyPI lookups, the requirements
    install, a restore and pruning ran with the manager port closed."""

    def test_the_port_answers_503_through_every_slow_step_and_is_free_at_the_exec(self):
        seen = {}

        def probe(step):
            got = _get(self.port)
            seen[step] = (got[0], json.loads(got[1])["phase"]) if got else None

        def latest():
            probe("latest_stable")
            return B

        def fits(version):
            probe("fits_this_python")
            return True

        def install(version):
            probe("install")
            make_venv(self.cfg, version)
            return True

        def changes(state, wanted, current):
            probe("apply_config_changes")
            return wanted

        with mock.patch.object(self.ep, "latest_stable", side_effect=latest), mock.patch.object(self.ep, "fits_this_python", side_effect=fits), \
                mock.patch.object(self.ep, "install", side_effect=install), \
                mock.patch.object(self.ep, "ensure_extra_requirements", side_effect=lambda v: probe("ensure_extra_requirements")), \
                mock.patch.object(self.ep, "apply_config_changes", side_effect=changes), \
                mock.patch.object(self.ep, "prune", side_effect=lambda keep: probe("prune")):
            self.assertEqual(self.boot(), B)
        self.assertEqual(set(seen), {"latest_stable", "fits_this_python", "install", "ensure_extra_requirements", "apply_config_changes", "prune"})
        for step, got in seen.items():
            self.assertIsNotNone(got, f"nothing listened during {step}")
            self.assertEqual(got[0], 503, step)
        self.assertIn("PyPI", seen["latest_stable"][1])
        self.assertIn("requirements", seen["ensure_extra_requirements"][1])
        self.assertIn("restore", seen["apply_config_changes"][1])
        self.assertIn("venv", seen["prune"][1])
        self.assertIsNone(self.execs[-1][1], "the status server still listened when run.py was exec'd")

    def test_a_failing_step_does_not_leave_the_server_running(self):
        make_venv(self.cfg, A)
        self.write({"current": A, "desired": A})
        with mock.patch.object(self.ep, "prune", side_effect=Stop), self.assertRaises(Stop):
            self.ep.main()
        self.assertIsNone(_get(self.port))

    def test_a_failed_restore_hold_shows_on_the_boot_server(self):
        make_venv(self.cfg, A)
        self.write({"current": A, "desired": A})
        pages = []

        def changes(state, wanted, current):
            result = {"ok": False, "recovery_source": "pre.zip"}
            with mock.patch.object(self.ep.backupkit, "pending", return_value=True), \
                    mock.patch.object(self.ep.time, "sleep", side_effect=lambda s: pages.append(_get(self.port))):
                self.ep.hold_after_failed_rollback(result, lambda: {"ok": True})
            return wanted

        with mock.patch.object(self.ep, "apply_config_changes", side_effect=changes), mock.patch.object(self.ep, "prune"):
            self.boot()
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0][0], 503)
        self.assertTrue(json.loads(pages[0][1])["restore_failed"])


if __name__ == "__main__":
    unittest.main()
