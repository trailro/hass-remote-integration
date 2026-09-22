"""Review round 3, installer side.

M9   the pins of a Home Assistant release reached pip's argv without the bad_requirement filter every other pip
     path has: an option or a URL in requires_dist became an argument of its own.
M13  the Log files tail held a line with no newline in it whole, copying the partial line on every 64 KiB
     block (quadratic, up to 32 MiB), and sent it whole to the page it polls for.
M3   error texts from aiohttp / GitHub / YAML went to the page unscrubbed: raise_for_status names the URL it
     answered for, and a private repo's zipball is redirected to codeload with ?token= in it.
C5   BuildCheckView kept every passed check id for the life of the process.
"""

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import aiohttp
from aiohttp.client_reqrep import RequestInfo
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from custom_components.integration_manager import build_views, logfiles_page, preflight
from custom_components.integration_manager import installer as inst_mod
from custom_components.integration_manager.manage_views import ReleasePreviewView, YamlView
from tests.test_polish_boot_check import BuilderRegistryCase
from tests.test_r3_install import ReinstallKeepsTheCheckedFilesTest

TOKEN = "GHSAT0AAAAAAsynth3ticTOKEN"


def _response_error():
    url = URL(f"https://codeload.github.com/owner/repo/legacy.zip/refs/tags/1.0?token={TOKEN}")
    info = RequestInfo(url, "GET", CIMultiDictProxy(CIMultiDict()), url)
    return aiohttp.ClientResponseError(info, (), status=502, message="Bad Gateway")


def _proc(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# ----- M9 -----------------------------------------------------------------------------------------

class HaPinsFilterTest(unittest.TestCase):
    BAD = ["--index-url=https://evil.example/simple", "evil @ https://evil.example/evil-1.0-py3-none-any.whl"]

    def run_check(self, requires_dist):
        report_json = json.dumps({"install": [{"metadata": {"requires_dist": requires_dist}}]})
        calls = []

        def pip(_python, reqs):
            calls.append(list(reqs))
            return _proc(stdout=report_json if reqs == ["homeassistant==2026.10.1"] else "{}")

        with mock.patch.object(preflight, "_pip_no_deps", side_effect=pip):
            return preflight._ha_wheel_check("python", "2026.10.1"), calls

    def test_an_option_or_a_url_pin_never_reaches_pip_and_is_a_blocker(self):
        report, calls = self.run_check(["aiohttp==3.14.0", *self.BAD, 'x==1; sys_platform == "win32"'])
        argv = [a for call in calls for a in call]
        for bad in self.BAD:
            self.assertNotIn(bad, argv, "before the fix the pin was an argument of its own")
        self.assertEqual(calls[1:], [["aiohttp==3.14.0"]])
        self.assertTrue(report["checked"])
        self.assertFalse(report["ok"], "an unchecked pin must not read as a checked one")
        self.assertEqual(len(report["blockers"]), 2)
        self.assertIn("is an option", report["blockers"][0])
        self.assertIn("installs from a URL", report["blockers"][1])
        self.assertEqual(report["requirements"], 1)
        self.assertIn("1 conditional requirement(s) not checked", " ".join(report["notes"]))

    def test_only_refused_pins_runs_no_empty_pip(self):
        report, calls = self.run_check(self.BAD)
        self.assertEqual(calls, [["homeassistant==2026.10.1"]])
        self.assertFalse(report["ok"])
        self.assertEqual(report["warnings"], [])

    def test_ordinary_pins_unchanged(self):
        report, calls = self.run_check(["aiohttp==3.14.0", "attrs==25.1.0"])
        self.assertTrue(report["ok"])
        self.assertEqual(report["blockers"], [])
        self.assertEqual(calls[1:], [["aiohttp==3.14.0", "attrs==25.1.0"]])


# ----- M13 ----------------------------------------------------------------------------------------

class LongLineTailTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hri-tail-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write(self, data):
        path = os.path.join(self.dir, "x.log")
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_a_line_with_no_newline_is_capped_to_its_start(self):
        head = b"2026-09-22 12:00:00 ERROR [x] "
        size = 3 * logfiles_page.MAX_LINE_CHARS
        path = self.write(head + b"A" * (size - len(head)))
        rows, scanned = logfiles_page._tail(path, 50, "")
        self.assertEqual(scanned, 1)
        self.assertEqual(len(rows), 1)
        cap = logfiles_page.MAX_LINE_CHARS
        self.assertLess(len(rows[0]), cap + 100, "before the fix the whole 3 MiB line was the row")
        self.assertTrue(rows[0].startswith(head.decode()), "the start of the line (time, level, logger) is kept")
        self.assertTrue(rows[0].endswith(f" [... {size - cap} more bytes of this line not shown]"))

    def test_lines_around_a_long_one_are_whole(self):
        cap = 1000
        body = b"B" * 5000
        path = self.write(b"first line\n" + b"long " + body + b"\nlast line\n")
        with mock.patch.object(logfiles_page, "MAX_LINE_CHARS", cap):
            rows, _ = logfiles_page._tail(path, 50, "")
        self.assertEqual(rows[0], "first line")
        self.assertEqual(rows[2], "last line")
        self.assertEqual(rows[1], "long " + "B" * (cap - 5) + f" [... {5005 - cap} more bytes of this line not shown]")

    def test_the_note_is_added_after_the_search(self):
        path = self.write(b"x " * 3000)
        with mock.patch.object(logfiles_page, "MAX_LINE_CHARS", 1000):
            self.assertEqual(logfiles_page._tail(path, 50, "not shown")[0], [])
            self.assertEqual(len(logfiles_page._tail(path, 50, "x x")[0]), 1)

    def test_long_key_material_stays_masked(self):
        path = self.write(b"ok line\n" + b"QUJD" * (logfiles_page.MAX_LINE_CHARS // 2) + b"\n")
        rows, _ = logfiles_page._tail_masked(path, 50, "")
        self.assertEqual(rows[0], "ok line")
        self.assertTrue(rows[1].startswith("*** [... "), rows[1][:40])
        self.assertNotIn("QUJD", rows[1])


# ----- M3 -----------------------------------------------------------------------------------------

def _request(query=None, body=None):
    return SimpleNamespace(headers={"X-Requested-With": "fetch"}, query=query or {}, content_type="application/json",
                           json=mock.AsyncMock(return_value=body or {}))


class ScrubbedErrorsTest(unittest.TestCase):
    def test_release_preview(self):
        view = ReleasePreviewView(SimpleNamespace(preview=mock.AsyncMock(side_effect=_response_error())))
        res = json.loads(asyncio.run(view.get(_request(query={"domain": "demo", "tag": "1.0"}))).body)
        self.assertFalse(res["ok"])
        self.assertIn("codeload.github.com", res["error"])
        self.assertNotIn(TOKEN, res["error"])

    def test_yaml_save(self):
        async def job(fn, *args):
            return fn(*args)

        hass = SimpleNamespace(async_add_executor_job=job)
        installer = SimpleNamespace(yaml_write=mock.Mock(side_effect=ValueError(f"cannot fetch https://x.example/?token={TOKEN}")))
        res = json.loads(asyncio.run(YamlView(hass, installer).post(_request(body={"text": "a: 1"}), "demo")).body)
        self.assertFalse(res["ok"])
        self.assertNotIn(TOKEN, res["error"])

    def test_build_check_and_preflight(self):
        case = BuilderRegistryCase("setUp")
        case.setUp()
        self.addCleanup(shutil.rmtree, case.dir, ignore_errors=True)
        with mock.patch.object(preflight, "run", mock.AsyncMock(side_effect=_response_error())), \
                mock.patch.object(build_views, "_commit_of", mock.AsyncMock(return_value="c" * 40)):
            res = json.loads(asyncio.run(case.check.post(SimpleNamespace(
                headers={}, query={}, content_type="application/json",
                json=mock.AsyncMock(return_value={"domain": "demo", "repo": "owner/repo", "ref": "1.0"})))).body)
            self.assertFalse(res["ok"])
            self.assertNotIn(TOKEN, res["error"])
            pf = object.__new__(build_views.PreflightView)
            pf.hass, pf.installer, pf._lock = None, case.inst, asyncio.Lock()
            res = json.loads(asyncio.run(pf.post(SimpleNamespace(
                headers={}, query={}, content_type="application/json",
                json=mock.AsyncMock(return_value={"domain": "demo", "tag": "1.0"})))).body)
        self.assertFalse(res["ok"])
        self.assertIn("ClientResponseError", res["error"])
        self.assertNotIn(TOKEN, res["error"])


class _FailingResponse:
    status, headers = 502, {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        raise _response_error()


class InstallLastErrorTest(unittest.TestCase):
    _installer = ReinstallKeepsTheCheckedFilesTest._installer
    _install = ReinstallKeepsTheCheckedFilesTest._install

    def test_a_failed_download_names_the_url_without_its_token(self):
        inst, _ = self._installer(b"")
        events = []
        session = SimpleNamespace(get=lambda *a, **kw: _FailingResponse())
        with mock.patch.object(inst_mod.events, "emit", side_effect=lambda *a, **kw: events.append(a)):
            res = self._install(inst, session)
        self.assertFalse(res["ok"])
        self.assertIn("codeload.github.com", inst.state.last_error)
        self.assertNotIn(TOKEN, inst.state.last_error)
        self.assertNotIn(TOKEN, json.dumps(res))
        self.assertNotIn(TOKEN, json.dumps(events))


# ----- C5 -----------------------------------------------------------------------------------------

class CheckTokensExpireTest(BuilderRegistryCase):
    def test_a_passed_check_drops_the_expired_ones(self):
        old = time.monotonic() - build_views.CHECK_TTL_S - 1
        self.check._checks = {"stale": old, "fresh": time.monotonic()}
        res = self.run_check({"domain": "demo", "repo": "owner/repo", "ref": "1.0"})
        self.assertTrue(res["ok"])
        self.assertNotIn("stale", self.check._checks, "before the fix every id stayed for the life of the process")
        self.assertIn("fresh", self.check._checks)
        self.assertIn(res["check_id"], self.check._checks)


if __name__ == "__main__":
    unittest.main()
