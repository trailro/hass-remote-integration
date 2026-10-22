"""Log files an integration writes in a folder of its own under the config dir, naming no path anywhere.

ramses_cc 0.61.1 writes ``/config/ramses_rf_logs/packet_log.log`` (0.60.4 wrote ``/config/packet_log.log``); its
config entry has no file path and the registry no ``log_dir``, so the page listed only the stale root file."""

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace

from aiohttp.test_utils import make_mocked_request

from custom_components.integration_manager import logfiles_page
from tests.fakes import FakeInstaller


async def _job(fn, *args):
    return fn(*args)


def _write(path, text="x\n", age_s=0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if age_s:
        t = time.time() - age_s
        os.utime(path, (t, t))


class SubfolderLogsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp(prefix="hri-logsub-")
        self.addCleanup(shutil.rmtree, self.cfg, ignore_errors=True)
        self.installer = FakeInstaller(running="ramses_cc")
        self.installer.settings = SimpleNamespace(data={})

    def names(self):
        return [f["name"] for f in logfiles_page._log_files(self.cfg, self.installer, [])]

    def test_the_live_log_in_a_subfolder_is_listed_before_the_stale_root_one(self):
        _write(os.path.join(self.cfg, "packet_log.log"), "old version\n", age_s=600)
        _write(os.path.join(self.cfg, "packet_log.log.2026-10-20"), age_s=86400)
        _write(os.path.join(self.cfg, "ramses_rf_logs", "packet_log.log"), "live line\n")
        _write(os.path.join(self.cfg, "ramses_rf_logs", "packet_log.log.2026-10-21"), age_s=7200)
        files = logfiles_page._log_files(self.cfg, self.installer, [])
        self.assertEqual([f["name"] for f in files], ["ramses_rf_logs/packet_log.log", "packet_log.log",
                                                      "ramses_rf_logs/packet_log.log.2026-10-21", "packet_log.log.2026-10-20"])
        self.assertEqual(files[0]["source"], "config subfolder")

    def test_it_is_tailed(self):
        _write(os.path.join(self.cfg, "ramses_rf_logs", "packet_log.log"), "first\nlive line\n")
        hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg), async_add_executor_job=_job,
                               config_entries=SimpleNamespace(async_entries=lambda domain=None: []))
        request = make_mocked_request("GET", "/api/log_files/tail", headers={"X-Requested-With": "fetch"})
        resp = asyncio.run(logfiles_page.LogFileTailView(hass, self.installer).get(request))
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertIn("live line", json.dumps(body["lines"]))
        self.assertTrue(body["path"].endswith("ramses_rf_logs/packet_log.log"))

    def test_home_assistant_and_manager_folders_are_not_scanned(self):
        for sub in (*logfiles_page.NOT_SCANNED, ".storage", ".cloud", "venv-2026.10.2", "venv-current"):
            _write(os.path.join(self.cfg, sub, "x.log"))
        self.assertEqual(self.names(), [])

    def test_a_linked_folder_or_file_is_not_listed(self):
        _write(os.path.join(self.cfg, "secrets.yaml"), "pw: x\n")
        _write(os.path.join(self.cfg, "real", "inner.log"))
        os.symlink(os.path.join(self.cfg, "real"), os.path.join(self.cfg, "linked"))
        os.makedirs(os.path.join(self.cfg, "logs2"))
        os.symlink(os.path.join(self.cfg, "secrets.yaml"), os.path.join(self.cfg, "logs2", "secret.log"))
        outside = tempfile.mkdtemp(prefix="hri-logsub-out-")
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        _write(os.path.join(outside, "far.log"))
        os.symlink(outside, os.path.join(self.cfg, "elsewhere"))
        self.assertEqual(self.names(), ["real/inner.log"])

    def test_a_hard_linked_file_is_not_listed(self):
        _write(os.path.join(self.cfg, "secrets.yaml"), "pw: x\n")
        os.makedirs(os.path.join(self.cfg, "mine"))
        os.link(os.path.join(self.cfg, "secrets.yaml"), os.path.join(self.cfg, "mine", "secret.log"))
        self.assertEqual(self.names(), [])

    def test_deeper_folders_are_not_scanned(self):
        _write(os.path.join(self.cfg, "a", "b", "deep.log"))
        _write(os.path.join(self.cfg, "a", "top.log"))
        self.assertEqual(self.names(), ["a/top.log"])

    def test_what_is_not_a_log_is_not_listed(self):
        _write(os.path.join(self.cfg, "mine", "notes.txt"))
        _write(os.path.join(self.cfg, "mine", "ramses.db"))
        os.makedirs(os.path.join(self.cfg, "mine", "dir.log"))
        self.assertEqual(self.names(), [])


if __name__ == "__main__":
    unittest.main()
