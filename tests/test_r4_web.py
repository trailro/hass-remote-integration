"""Round 4 web review: secrets masked on the log surfaces (HRI-06), the image build label (HRI-21), bounded log
queries and logger names, log files limited to regular *.log files, CSP on raised errors, redirects raised,
one memory probe at a time, HA's onboarding API blocked, and the compose asset uploaded by its own job."""

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import yaml
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

import logbuffer
from custom_components.integration_manager import auth as auth_mod
from custom_components.integration_manager import hostguard, logfiles_page, logs_page, memdiag
from tests.fakes import FakeInstaller

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IM_DIR = os.path.dirname(auth_mod.__file__)


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


async def _job(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


def _tmp(test):
    tmp = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, tmp, True)
    return tmp


def _get(url):
    return make_mocked_request("GET", url, headers={"X-Requested-With": "fetch", "Host": "10.0.0.2:8087"})


def _post_json(body):
    async def payload():
        return body

    return SimpleNamespace(content_type="application/json", json=payload)


class LogSecretsTest(unittest.TestCase):
    """HRI-06: the log tails and the log records go through the diagnostics scrubber."""

    def test_log_file_tail_masks_passwords(self):
        cfg = _tmp(self)
        with open(os.path.join(cfg, "app.log"), "w", encoding="utf-8") as fh:
            fh.write("2026-09-15 INFO login password=hunter2 ok\n2026-09-15 INFO plain line\n")
        installer = FakeInstaller()
        installer.settings = SimpleNamespace(data={})
        hass = SimpleNamespace(config=SimpleNamespace(config_dir=cfg), async_add_executor_job=_job)
        view = logfiles_page.LogFileTailView(hass, installer)
        resp = asyncio.run(view.get(_get("/api/log_files/tail?file=app.log")))
        body = resp.body.decode()
        self.assertEqual(resp.status, 200, body)
        self.assertNotIn("hunter2", body)
        self.assertIn("password=***", body)
        self.assertIn("plain line", body)

    def test_log_file_tail_masks_formatted_cells(self):
        cfg = _tmp(self)
        with open(os.path.join(cfg, "app.log"), "w", encoding="utf-8") as fh:
            fh.write("INFO token=abcdef123\n")
        installer = FakeInstaller()
        installer.settings = SimpleNamespace(data={"log_format": {"pattern": r"(?P<level>\w+) (?P<msg>.*)"}})
        hass = SimpleNamespace(config=SimpleNamespace(config_dir=cfg), async_add_executor_job=_job)
        resp = asyncio.run(logfiles_page.LogFileTailView(hass, installer).get(_get("/api/log_files/tail")))
        self.assertNotIn("abcdef123", resp.body.decode())

    def _handler(self):
        tmp = _tmp(self)
        handler = logbuffer.FileLogHandler(os.path.join(tmp, "process.log"))
        self.addCleanup(handler.close)
        try:
            raise RuntimeError("refresh failed with access_token=SYNTHTOKEN99")
        except RuntimeError:
            exc_info = sys.exc_info()
        handler.emit(logging.LogRecord("custom_components.demo", logging.ERROR, __file__, 1,
                                       "auth header Authorization: Bearer SYNTHBEARER12345 access_token=SYNTHTOKEN42", None, exc_info))
        return handler

    def test_log_records_mask_message_and_exception(self):
        handler = self._handler()
        view = logs_page.LogsApiView(SimpleNamespace(async_add_executor_job=_job))
        with mock.patch.object(logs_page.logbuffer, "find", return_value=handler):
            resp = asyncio.run(view.get(_get("/api/logs")))
        body = resp.body.decode()
        self.assertEqual(resp.status, 200, body)
        for secret in ("SYNTHTOKEN42", "SYNTHTOKEN99", "SYNTHBEARER12345"):
            self.assertNotIn(secret, body)
        recs = json.loads(body)["records"]
        self.assertEqual(len(recs), 1)
        self.assertIn("access_token=***", recs[0]["message"])
        self.assertIn("access_token=***", recs[0]["exc"])


class LogQueryBoundsTest(unittest.TestCase):
    def test_limit_outside_its_range_is_refused(self):
        """Refused rather than clamped since the b4cd1a1 review (docs/api.md: 1-2000, otherwise 400)."""
        seen = []
        handler = SimpleNamespace(capacity=0, path="p", query=lambda **kw: (seen.append(kw["limit"]) or ([], False)))
        view = logs_page.LogsApiView(SimpleNamespace(async_add_executor_job=_job))
        with mock.patch.object(logs_page.logbuffer, "find", return_value=handler):
            statuses = [asyncio.run(view.get(_get(f"/api/logs?limit={limit}"))).status for limit in ("0", "-5", "99999", "7")]
        self.assertEqual(statuses, [400, 400, 400, 200])
        self.assertEqual(seen, [7])

    def test_level_only_for_known_or_sane_logger_names(self):
        view = logs_page.LogLevelView()
        self.addCleanup(logging.getLogger("homeassistant").setLevel, logging.getLogger("homeassistant").level)
        resp = asyncio.run(view.post(_post_json({"logger": "homeassistant", "level": "INFO"})))
        self.assertEqual(resp.status, 200)
        for bad in ("not a logger", "x" * 300, "a..b", "../etc", ".lead"):
            with self.subTest(name=bad):
                resp = asyncio.run(view.post(_post_json({"logger": bad, "level": "DEBUG"})))
                self.assertEqual(resp.status, 400)
                self.assertNotIn(bad, logging.Logger.manager.loggerDict)

    def test_new_logger_names_are_capped(self):
        view = logs_page.LogLevelView()
        with mock.patch.object(logs_page, "MAX_NEW_LOGGERS", 2), mock.patch.object(logs_page, "_NEW_LOGGERS", set()):
            ok = [asyncio.run(view.post(_post_json({"logger": f"r4probe_lib{i}", "level": "DEBUG"}))).status for i in range(3)]
            again = asyncio.run(view.post(_post_json({"logger": "r4probe_lib0", "level": None}))).status
        self.assertEqual(ok, [200, 200, 400])
        self.assertEqual(again, 200)  # one already created may be set again
        self.assertNotIn("r4probe_lib2", logging.Logger.manager.loggerDict)
        for i in range(2):
            logging.getLogger(f"r4probe_lib{i}").setLevel(logging.NOTSET)


class LogFileDiscoveryTest(unittest.TestCase):
    def test_log_dir_lists_only_regular_log_files(self):
        cfg = _tmp(self)
        logs = os.path.join(cfg, "logs")
        os.makedirs(logs)
        for name in ("a.log", "a.log.1", "notes.txt", "state.json"):
            open(os.path.join(logs, name), "w").close()
        with open(os.path.join(cfg, "secrets.yaml"), "w", encoding="utf-8") as fh:
            fh.write("pw: x\n")
        os.symlink(os.path.join(cfg, "secrets.yaml"), os.path.join(logs, "link.log"))
        os.symlink(os.path.join(cfg, "secrets.yaml"), os.path.join(cfg, "root.log"))
        os.makedirs(os.path.join(logs, "dir.log"))
        installer = FakeInstaller(running="demo", spec={"log_dir": "logs"})
        names = sorted(f["name"] for f in logfiles_page._log_files(cfg, installer, [os.path.join(cfg, "root.log")]))
        self.assertEqual(names, ["logs/a.log", "logs/a.log.1"])


class HostGuardTest(unittest.TestCase):
    def _guard(self):
        app = SimpleNamespace(middlewares=[])
        hostguard.install_host_guard(SimpleNamespace(http=SimpleNamespace(app=app)), SimpleNamespace(settings=SimpleNamespace(data={})))
        return app.middlewares[0]

    def test_csp_on_raised_http_errors(self):
        guard = self._guard()

        async def handler(request):
            raise web.HTTPNotFound(text="no")

        with self.assertRaises(web.HTTPNotFound) as ctx:
            asyncio.run(guard(_get("/nothing"), handler))
        self.assertIn("frame-ancestors 'none'", ctx.exception.headers.get("Content-Security-Policy", ""))

    def test_onboarding_api_is_blocked(self):
        """U1: an integration depending on frontend/panel_custom loads HA's onboarding, whose user creation is
        open while no user exists and reads text/plain bodies: a page on another origin could create an owner."""
        guard = self._guard()
        called = []

        async def handler(request):
            called.append(request.path)
            return web.Response(text="ok")

        for path in ("/api/onboarding", "/api/onboarding/users", "/api/onboarding/core_config"):
            resp = asyncio.run(guard(make_mocked_request("POST", path, headers={"Host": "10.0.0.2:8087", "Content-Type": "text/plain"}), handler))
            self.assertEqual(resp.status, 403, path)
        resp = asyncio.run(guard(_get("/api/onboardingx"), handler))
        self.assertEqual(resp.status, 200)
        self.assertEqual(called, ["/api/onboardingx"])


class RedirectRaisedTest(unittest.TestCase):
    def test_login_redirects_are_raised_not_returned(self):
        tmp = _tmp(self)

        async def job(fn, *args):
            return fn(*args)

        hass = SimpleNamespace(async_add_executor_job=job, data={}, config=SimpleNamespace(path=lambda *p: os.path.join(tmp, *p)),
                               http=SimpleNamespace(app=SimpleNamespace(middlewares=[])))
        env = {k: v for k, v in os.environ.items() if k not in ("HRI_PASSWORD_FILE", "HRI_COOKIE_SECURE")}
        with mock.patch.dict(os.environ, {**env, "HRI_PASSWORD": "pw"}, clear=True):
            asyncio.run(auth_mod.async_setup_auth(hass))
        guard = hass.http.app.middlewares[0]
        req = SimpleNamespace(headers={}, query={}, cookies={}, path="/logs", path_qs="/logs", secure=False, remote="10.0.0.9")
        with self.assertRaises(web.HTTPFound) as ctx:
            asyncio.run(guard(req, None))
        self.assertTrue(ctx.exception.location.startswith("/login?next="))
        with self.assertRaises(web.HTTPFound):
            asyncio.run(auth_mod.LoginPageView(auth_mod.Auth("")).get(req))


class MemoryProbeTest(unittest.TestCase):
    def test_one_probe_at_a_time(self):
        async def main():
            gate = asyncio.Event()
            loop = asyncio.get_running_loop()

            def slow(name):
                asyncio.run_coroutine_threadsafe(gate.wait(), loop).result(5)
                return {"type": name}

            view = memdiag.MemoryDiagView(SimpleNamespace(async_add_executor_job=_job))
            with mock.patch.object(memdiag, "referrers", slow):
                first = asyncio.ensure_future(view.get(_get("/api/diag/memory?refs=dict")))
                await asyncio.sleep(0.05)
                second = await view.get(_get("/api/diag/memory?refs=dict"))
                gate.set()
                done = await first
                third = await asyncio.wait_for(view.get(_get("/api/diag/memory?refs=list")), 5)
            return second.status, done.status, third.status

        self.assertEqual(asyncio.run(main()), (429, 200, 200))


class WorkflowTest(unittest.TestCase):
    def test_image_build_label_and_permissions(self):
        if not os.path.isfile(os.path.join(ROOT, ".github", "workflows", "image.yml")):
            self.skipTest("workflows not copied next to the tests")
        wf = _read(".github", "workflows", "image.yml")
        self.assertNotIn("HRI_BUILD=${{ github.sha }}", wf)
        self.assertIn("git rev-parse HEAD", wf)
        self.assertIn("HRI_BUILD=${{ steps.commit.outputs.sha }}", wf)
        top = wf.split("\njobs:", 1)[0]
        self.assertNotIn("contents: write", top)
        # only the release upload and the commit of the app's version write, each in its own job
        jobs = yaml.safe_load(wf)["jobs"]
        writers = sorted(name for name, job in jobs.items() if (job.get("permissions") or {}).get("contents") == "write")
        self.assertEqual(writers, ["app-version", "compose"])
        self.assertEqual(wf.count("contents: write"), 2)
        # a stable release (published + released) is built once, on released; a pre-release on published;
        # a manual run builds unless it only updates the Docker Hub overview
        self.assertIn("if: (github.event_name != 'release' && !inputs.readme_only) || github.event.action == 'released' || github.event.release.prerelease", wf)
        # one build pushes to both registries, and Docker Hub needs its token before anything is built
        self.assertIn("ghcr.io/${{ github.repository }}", wf)
        self.assertIn("DOCKERHUB_IMAGE: docker.io/trailro26/hass-remote-integration", wf)
        self.assertIn("${{ env.DOCKERHUB_IMAGE }}", wf)
        image_job = wf.split("\n  image:", 1)[1].split("\n  compose:", 1)[0]
        self.assertLess(image_job.index("Docker Hub token present"), image_job.index("docker/build-push-action"))
        # the overview follows only the newest stable release, after a successful build or on its own
        overview = wf.split("\n  dockerhub-overview:", 1)[1]
        self.assertIn("needs.image.result == 'success' || (needs.image.result == 'skipped' && inputs.readme_only)", overview)
        self.assertIn("steps.newest.outputs.enable == 'true'", overview)
        for line in wf.splitlines():
            if line.strip().startswith(("- uses:", "uses:")):
                self.assertRegex(line, r"@[0-9a-f]{40}\b", line)


class SystemPageTest(unittest.TestCase):
    def test_entry_id_is_stringified(self):
        js = _read("custom_components", "integration_manager", "static", "system.js")
        self.assertTrue("String(e.entry_id).slice(0,8)" in js and "e.entry_id.slice" not in js)


if __name__ == "__main__":
    unittest.main()
