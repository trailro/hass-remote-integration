"""Campaign findings on the auth and log surfaces: an empty password file that
turned the login off (M1), private keys printed unmasked on the Log files page
(M3), hard links past the symlink guard (m5), the host-guard 403 without a
Content-Security-Policy (m6), the root logger silenced through the API (m7) and
the ungated log-file listing (m12)."""

import asyncio
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from aiohttp.test_utils import make_mocked_request

# the modules, not the names: the same file then loads against a tree without the fix,
# so each test reports its own defect instead of one import error for the lot
from custom_components.integration_manager import auth as auth_mod
from custom_components.integration_manager import diagnostics, hostguard, logfiles_page, logs_page
from custom_components.integration_manager.auth import Auth, LoginView
from tests.fakes import FakeInstaller

PEM_LOG = (
    "2026-09-16 10:00:11 INFO [probe] before",
    "2026-09-16 10:00:12 INFO [probe] -----BEGIN PRIVATE KEY-----",
    "MIIFSYNTHETICKEYMATERIALAAAABBBBCCCCDDDD",
    "-----END PRIVATE KEY-----",
    "2026-09-16 10:00:13 INFO [probe] after",
)
KEY_BODY = "MIIFSYNTHETICKEYMATERIALAAAABBBBCCCCDDDD"


async def _job(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


def _tmp(test):
    tmp = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, tmp, True)
    return tmp


def _get(url, fetch=True):
    headers = {"Host": "10.0.0.2:8087"}
    if fetch:
        headers["X-Requested-With"] = "fetch"
    return make_mocked_request("GET", url, headers=headers)


def _post_json(body):
    async def payload():
        return body

    return SimpleNamespace(content_type="application/json", json=payload, headers={}, cookies={}, remote="10.0.0.9",
                           secure=False, path="/api/login", path_qs="/api/login", query={})


# ----- M1 -----------------------------------------------------------------------------------------

class EmptyPasswordFileTest(unittest.TestCase):
    """An empty HRI_PASSWORD_FILE used to read as "no password configured", which
    opened the whole admin surface on the LAN without a line in the log."""

    def _file(self, content):
        path = os.path.join(_tmp(self), "hri_password")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path

    def test_zero_byte_file_does_not_turn_the_login_off(self):
        with mock.patch.dict(os.environ, {"HRI_PASSWORD_FILE": self._file("")}, clear=False):
            password, reason = auth_mod._configured_password()
        self.assertTrue(Auth(password).enabled)
        self.assertIn("is empty", reason)

    def test_whitespace_only_file_does_not_turn_the_login_off(self):
        with mock.patch.dict(os.environ, {"HRI_PASSWORD_FILE": self._file("\n  \t\n")}, clear=False):
            password, reason = auth_mod._configured_password()
        self.assertTrue(Auth(password).enabled)
        self.assertIn("is empty", reason)

    def test_the_password_nobody_knows_is_not_guessable(self):
        with mock.patch.dict(os.environ, {"HRI_PASSWORD_FILE": self._file("")}, clear=False):
            auth = Auth(auth_mod._configured_password()[0])
        for guess in ("", " ", "\n", "password"):
            self.assertFalse(auth.check_password(guess), guess)

    def test_an_unreadable_file_still_fails_closed(self):
        with mock.patch.dict(os.environ, {"HRI_PASSWORD_FILE": os.path.join(_tmp(self), "gone")}, clear=False):
            password, reason = auth_mod._configured_password()
        self.assertTrue(Auth(password).enabled)
        self.assertIn("not readable", reason)

    def test_a_real_password_file_is_unchanged(self):
        with mock.patch.dict(os.environ, {"HRI_PASSWORD_FILE": self._file("s3cr3t\n")}, clear=False):
            self.assertEqual(auth_mod._configured_password(), ("s3cr3t", ""))

    def test_no_password_at_all_still_means_no_login(self):
        env = {k: v for k, v in os.environ.items() if k not in ("HRI_PASSWORD_FILE", "HRI_PASSWORD")}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(auth_mod._configured_password(), ("", ""))

    def test_the_login_page_says_why_instead_of_wrong_password(self):
        auth = Auth("unguessable", b"k" * 32, unusable="HRI_PASSWORD_FILE /run/secrets/hri_password is empty")
        resp = asyncio.run(LoginView(auth).post(_post_json({"password": "anything"})))
        self.assertEqual(resp.status, 503)
        self.assertIn("is empty", resp.body.decode())


# ----- M3 -----------------------------------------------------------------------------------------

class MultiLineScrubTest(unittest.TestCase):
    """The page scrubbed line by line, so the PEM pattern could never match: the
    BEGIN marker came back masked and the key body was printed on the next line."""

    def test_scrub_lines_masks_a_block_that_spans_lines(self):
        out = diagnostics.scrub_lines(list(PEM_LOG))
        self.assertNotIn(KEY_BODY, "\n".join(out))
        self.assertIn("before", out[0])
        self.assertIn("after", out[4])

    def test_scrub_lines_keeps_one_element_per_element(self):
        self.assertEqual(len(diagnostics.scrub_lines(list(PEM_LOG))), len(PEM_LOG))

    def test_scrub_lines_keeps_the_newlines_of_a_multi_line_element(self):
        records = ["first\n" + "\n".join(PEM_LOG[1:4]), "second"]
        out = diagnostics.scrub_lines(records)
        self.assertEqual([t.count("\n") for t in out], [3, 0])
        self.assertNotIn(KEY_BODY, "\n".join(out))

    def test_scrub_lines_still_applies_the_single_line_rules(self):
        out = diagnostics.scrub_lines(["INFO password=hunter2", "INFO plain"])
        self.assertEqual(out, ["INFO password=***", "INFO plain"])

    def test_scrub_of_one_whole_text_is_unchanged(self):
        self.assertEqual(diagnostics.scrub("a -----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE----- b"),
                         "a -----BEGIN CERTIFICATE-----***-----END CERTIFICATE----- b")

    def test_the_tail_of_a_log_file_masks_the_key(self):
        cfg = _tmp(self)
        with open(os.path.join(cfg, "app.log"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(PEM_LOG) + "\n")
        installer = FakeInstaller()
        installer.settings = SimpleNamespace(data={})
        hass = SimpleNamespace(config=SimpleNamespace(config_dir=cfg), async_add_executor_job=_job)
        resp = asyncio.run(logfiles_page.LogFileTailView(hass, installer).get(_get("/api/log_files/tail?file=app.log")))
        body = resp.body.decode()
        self.assertEqual(resp.status, 200, body)
        self.assertNotIn(KEY_BODY, body)
        self.assertIn("after", body)

    def test_the_log_records_mask_a_key_split_over_records(self):
        """The diagnostics zip joins the records before scrubbing; the Logs page
        scrubbed each record on its own, so a key logged line by line came through."""
        recs = [{"message": text, "exc": None} for text in PEM_LOG]
        handler = SimpleNamespace(query=lambda **kw: (recs, False))
        out, _, _ = logs_page._query_masked(handler)
        self.assertNotIn(KEY_BODY, "\n".join(r["message"] for r in out))
        self.assertIn("after", out[4]["message"])

    def test_the_log_records_mask_a_key_inside_one_traceback(self):
        recs = [{"message": "boom", "exc": "Traceback\n" + "\n".join(PEM_LOG[1:4])}]
        handler = SimpleNamespace(query=lambda **kw: (recs, False))
        out, _, _ = logs_page._query_masked(handler)
        self.assertNotIn(KEY_BODY, out[0]["exc"])
        self.assertEqual(out[0]["message"], "boom")


# ----- m5 -----------------------------------------------------------------------------------------

class HardLinkTest(unittest.TestCase):
    """The guard rejected symlinks only, so a hard link named <x>.log made any
    file under the config dir (secrets.yaml) listed, tailable and packed in the zip."""

    def test_a_hard_link_to_secrets_is_not_a_log_file(self):
        cfg = _tmp(self)
        with open(os.path.join(cfg, "secrets.yaml"), "w", encoding="utf-8") as fh:
            fh.write("mqtt_password: s3cr3t\n")
        os.link(os.path.join(cfg, "secrets.yaml"), os.path.join(cfg, "rotated.log"))
        with open(os.path.join(cfg, "app.log"), "w", encoding="utf-8") as fh:
            fh.write("INFO plain\n")
        names = [f["name"] for f in logfiles_page._log_files(cfg, FakeInstaller(), [])]
        self.assertEqual(names, ["app.log"])

    def test_rotated_files_of_an_integration_stay_listed(self):
        cfg = _tmp(self)
        logs = os.path.join(cfg, "logs")
        os.makedirs(logs)
        for name in ("a.log", "a.log.1", "a.log.2026-09-15"):
            with open(os.path.join(logs, name), "w", encoding="utf-8") as fh:
                fh.write("INFO line\n")
        installer = FakeInstaller(running="demo", spec={"log_dir": "logs"})
        names = sorted(f["name"] for f in logfiles_page._log_files(cfg, installer, []))
        self.assertEqual(names, ["logs/a.log", "logs/a.log.1", "logs/a.log.2026-09-15"])


# ----- m6 -----------------------------------------------------------------------------------------

class HostGuard403Test(unittest.TestCase):
    def _guard(self):
        app = SimpleNamespace(middlewares=[])
        hostguard.install_host_guard(SimpleNamespace(http=SimpleNamespace(app=app)), SimpleNamespace(settings=SimpleNamespace(data={})))
        return app.middlewares[0]

    async def _never(self, request):
        raise AssertionError("the handler must not run")

    def test_the_refused_host_answer_carries_the_policy(self):
        request = make_mocked_request("GET", "/", headers={"Host": "attacker.example.com"})
        resp = asyncio.run(self._guard()(request, self._never))
        self.assertEqual(resp.status, 403)
        self.assertIn("frame-ancestors 'none'", resp.headers.get("Content-Security-Policy", ""))

    def test_the_onboarding_answer_carries_the_policy(self):
        resp = asyncio.run(self._guard()(_get("/api/onboarding/users"), self._never))
        self.assertEqual(resp.status, 403)
        self.assertIn("frame-ancestors 'none'", resp.headers.get("Content-Security-Policy", ""))


# ----- m7 -----------------------------------------------------------------------------------------

class RootLoggerTest(unittest.TestCase):
    """getLogger("root") is the root logger, so CRITICAL there silenced the whole
    process log; nothing listed it, and clearing it left NOTSET, not the INFO boot set."""

    def test_the_root_logger_is_refused(self):
        resp = asyncio.run(logs_page.LogLevelView().post(_post_json({"logger": "root", "level": "CRITICAL"})))
        self.assertEqual(resp.status, 400)
        self.assertIn("root logger", resp.body.decode())

    def test_clearing_the_root_logger_is_refused_too(self):
        resp = asyncio.run(logs_page.LogLevelView().post(_post_json({"logger": "root", "level": None})))
        self.assertEqual(resp.status, 400)

    def test_a_noisy_integration_logger_can_still_be_raised(self):
        name = "custom_components.hri_probe_m7"
        self.addCleanup(logs_page.logging.getLogger(name).setLevel, logs_page.logging.NOTSET)
        self.addCleanup(logs_page._NEW_LOGGERS.discard, name)
        resp = asyncio.run(logs_page.LogLevelView().post(_post_json({"logger": name, "level": "ERROR"})))
        self.assertEqual(resp.status, 200, resp.body.decode())
        self.assertEqual(logs_page.logging.getLogger(name).level, logs_page.logging.ERROR)
        self.assertEqual(logs_page.logging.getLogger().level, logs_page.logging.WARNING)


# ----- m12 ----------------------------------------------------------------------------------------

class LogFileListingGateTest(unittest.TestCase):
    """The tail was gated, the listing was not, so any page could make the server
    walk the config directory."""

    def _view(self):
        cfg = _tmp(self)
        with open(os.path.join(cfg, "app.log"), "w", encoding="utf-8") as fh:
            fh.write("INFO line\n")
        hass = SimpleNamespace(config=SimpleNamespace(config_dir=cfg), async_add_executor_job=_job)
        return logfiles_page.LogFilesView(hass, FakeInstaller())

    def test_a_listing_without_the_header_is_refused(self):
        resp = asyncio.run(self._view().get(_get("/api/log_files", fetch=False)))
        self.assertEqual(resp.status, 400)
        self.assertIn("X-Requested-With", resp.body.decode())

    def test_the_ui_still_gets_the_listing(self):
        resp = asyncio.run(self._view().get(_get("/api/log_files")))
        self.assertEqual(resp.status, 200)
        self.assertIn("app.log", resp.body.decode())


if __name__ == "__main__":
    unittest.main()
