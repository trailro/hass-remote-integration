"""Test campaign findings: a restored .storage/http that crash-loops the container, a preflight blocker that
hides the missing compiler, the install page answering 200 on every path, and an install budget on the wall
clock instead of on progress."""

import asyncio
import importlib
import inspect
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from types import SimpleNamespace
from unittest import mock

import backupkit
import run
from custom_components.integration_manager import preflight
from tests.fakes import entrypoint_for
from tests.test_r3_install import _hass, _preflight_installer

# what pip really writes when it resolves scipy without a compiler (35 lines; the compiler is on line 15)
SCIPY_STDERR = """  error: subprocess-exited-with-error

  × Preparing metadata (pyproject.toml) did not run successfully.
  │ exit code: 1
  ╰─> [26 lines of output]
      + meson setup /tmp/pip-install-x/scipy /tmp/pip-install-x/scipy/.mesonpy-a
      The Meson build system
      Version: 1.9.1
      Source dir: /tmp/pip-install-x/scipy
      Build dir: /tmp/pip-install-x/scipy/.mesonpy-a
      Build type: native build
      Project name: scipy
      Project version: 1.11.4
      ../meson.build:1:0: ERROR: Unknown compiler(s): [['cc'], ['gcc'], ['clang'], ['nvc'], ['pgcc'], ['icc'], ['icx']]
      The following exception(s) were encountered:
      Running `cc --version` gave "[Errno 2] No such file or directory: 'cc'"
      Running `gcc --version` gave "[Errno 2] No such file or directory: 'gcc'"
      Running `clang --version` gave "[Errno 2] No such file or directory: 'clang'"
      Running `nvc --version` gave "[Errno 2] No such file or directory: 'nvc'"
      Running `pgcc --version` gave "[Errno 2] No such file or directory: 'pgcc'"
      Running `icc --version` gave "[Errno 2] No such file or directory: 'icc'"
      Running `icx --version` gave "[Errno 2] No such file or directory: 'icx'"

      A full log can be found at /tmp/pip-install-x/scipy/.mesonpy-a/meson-logs/meson-log.txt
      [end of output]

  note: This error originates from a subprocess, and is likely not a problem with pip.
error: metadata-generation-failed

× Encountered error while generating package metadata.
╰─> scipy

note: This is an issue with the package mentioned above, not pip.
hint: See above for details.
"""


def _tmp(test):
    d = tempfile.mkdtemp(prefix="hri-camp-")
    test.addCleanup(shutil.rmtree, d, ignore_errors=True)
    return d


def _http_store(port, key="stable"):
    data = {"stable": None, "pending": None, "yaml_migration_done": True}
    data[key] = {"server_port": port, "ip_ban_enabled": True}
    return json.dumps({"version": 2, "minor_version": 2, "key": "http", "data": data})


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class HttpPortIsNotBackedUpTest(unittest.TestCase):
    """The pinned port must not travel to another container inside a backup."""

    def test_storage_http_is_left_out(self):
        cfg = _tmp(self)
        _write(os.path.join(cfg, backupkit.MARKER), "{}")
        _write(os.path.join(cfg, ".storage", "http"), _http_store(8087))
        _write(os.path.join(cfg, ".storage", "http.auth"), "{}")
        _write(os.path.join(cfg, ".storage", "core.config_entries"), "{}")
        rec = backupkit.create(cfg)
        with zipfile.ZipFile(os.path.join(cfg, backupkit.BACKUP_DIR, rec["name"])) as zf:
            names = zf.namelist()
        self.assertNotIn(".storage/http", names)
        self.assertIn(".storage/http.auth", names)  # only the port pin goes, not the rest of .storage
        self.assertIn(".storage/core.config_entries", names)


class ForeignHttpPortHealsTest(unittest.TestCase):
    """A backup made on another HRI_PORT used to end every boot at the port check: a crash loop with no UI."""

    def store(self, cfg):
        return os.path.join(cfg, ".storage", "http")

    def test_a_foreign_pin_is_removed(self):
        cfg = _tmp(self)
        _write(self.store(cfg), _http_store(8123))
        self.assertEqual(run.drop_foreign_http_port(cfg, 8087), 8123)
        self.assertFalse(os.path.exists(self.store(cfg)))

    def test_a_pending_pin_counts_too(self):
        cfg = _tmp(self)
        _write(self.store(cfg), _http_store(9999, key="pending"))
        self.assertEqual(run.drop_foreign_http_port(cfg, 8087), 9999)

    def test_our_own_store_is_kept(self):
        cfg = _tmp(self)
        _write(self.store(cfg), _http_store(8087))
        self.assertIsNone(run.drop_foreign_http_port(cfg, 8087))
        self.assertTrue(os.path.exists(self.store(cfg)))

    def test_no_store_and_a_broken_one_are_no_problem(self):
        cfg = _tmp(self)
        self.assertIsNone(run.drop_foreign_http_port(cfg, 8087))
        _write(self.store(cfg), "{not json")
        self.assertIsNone(run.drop_foreign_http_port(cfg, 8087))

    def test_the_boot_clears_the_store_before_http_is_set_up(self):
        src = inspect.getsource(run._boot)
        self.assertLess(src.index("drop_foreign_http_port"), src.index('"http", "integration_manager"'),
                        "the store is cleared too late to help this boot")


class PipReasonSurvivesLongOutputTest(unittest.TestCase):
    """scipy's decisive line sits 17 lines above pip's closing summary: truncating first hid it."""

    def _pip_fails(self):
        proc = SimpleNamespace(returncode=1, stdout="", stderr=SCIPY_STDERR)
        return mock.patch.object(preflight, "_run_pip", return_value=proc)

    def test_the_report_keeps_the_whole_output_for_the_reason(self):
        with self._pip_fails():
            res = preflight._pip_dry_run("python", ["scipy"], None)
        self.assertFalse(res["ok"])
        self.assertLessEqual(len(res["stderr"].splitlines()), preflight.STDERR_TAIL_LINES)  # the UI shows this verbatim
        self.assertIn("Unknown compiler(s)", preflight._pip_reason(res.get("stderr_full") or res["stderr"]))

    def test_the_blocker_names_the_compiler(self):
        inst = _preflight_installer(self, {"__init__.py": "x = 1\n"})
        _write(os.path.join(inst._version_dir("demo", "2.0"), "manifest.json"),
               json.dumps({"domain": "demo", "version": "2.0", "requirements": ["scipy"]}))
        preflight._REPORTS.clear()
        with self._pip_fails():
            res = asyncio.run(preflight.gate(_hass(), inst, "demo", "2.0"))
        blockers = "; ".join(res["report"]["blockers"])
        self.assertIn("requirements cannot be resolved", blockers)
        self.assertIn("gcc", blockers)
        self.assertNotIn("metadata-generation-failed", blockers)


class StatusPageIsNotHealthyTest(unittest.TestCase):
    """While Home Assistant installs, every path answered 200 with the progress page."""

    def setUp(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.cfg = _tmp(self)
        os.makedirs(os.path.join(self.cfg, "integration_manager"))
        self.ep = entrypoint_for(self, self.cfg, HRI_PORT=str(port))
        self.ep._status.update(phase="pip install homeassistant==2026.9.2", version="2026.9.2")
        self.srv = self.ep.start_status_server()
        self.addCleanup(self.ep.stop_status_server, self.srv)
        self.url = f"http://127.0.0.1:{port}"

    def get(self, path):
        try:
            with urllib.request.urlopen(self.url + path, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read().decode()
        except urllib.error.HTTPError as err:
            with err:
                return err.code, dict(err.headers), err.read().decode()

    def test_api_paths_answer_503_with_json(self):
        for path in ("/api/status", "/api/diag/health"):
            code, headers, body = self.get(path)
            self.assertEqual(code, 503, path)
            self.assertEqual(headers["Content-Type"], "application/json")
            self.assertEqual(headers["Retry-After"], str(self.ep.STATUS_RETRY_AFTER_S))
            self.assertEqual(json.loads(body)["version"], "2026.9.2")

    def test_the_page_still_renders_but_is_not_a_200(self):
        code, headers, body = self.get("/")
        self.assertEqual(code, 503)
        self.assertEqual(headers["Retry-After"], str(self.ep.STATUS_RETRY_AFTER_S))
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("http-equiv=refresh", body)
        self.assertIn("pip install homeassistant==2026.9.2", body)


class PipBudgetFollowsProgressTest(unittest.TestCase):
    """A slow but progressing install used to be killed by the wall clock and lose the whole venv."""

    def setUp(self):
        self.ep = importlib.import_module("entrypoint")
        self.out = open(os.path.join(_tmp(self), "pip.log"), "w", encoding="utf-8")
        self.addCleanup(self.out.close)

    def test_an_install_that_keeps_writing_is_not_killed(self):
        t0 = time.monotonic()
        self.ep._run_pip(["sh", "-c", "for i in 1 2 3 4 5 6 7 8; do echo Collecting package-$i; sleep 0.2; done"],
                         self.out, idle_timeout=0.6)
        self.assertGreater(time.monotonic() - t0, 0.6)  # longer than the budget, and it still finished

    def test_a_silent_install_still_ends(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            self.ep._run_pip(["sh", "-c", "sleep 30"], self.out, idle_timeout=0.5)

    def test_pip_is_not_quiet(self):
        """-q printed nothing at all for a whole install: no progress for the page, none for the budget."""
        src = inspect.getsource(self.ep.install) + inspect.getsource(self.ep.ensure_extra_requirements)
        self.assertFalse('"-q"' in src, "pip -q writes nothing at all: no progress for the page and none for the budget")


if __name__ == "__main__":
    unittest.main()
