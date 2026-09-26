"""Review round 5, auth: the boot status page (its /api/ answer and its HTML) named the version and the phase (apt
packages included) without a login even with a password set; a password that is not UTF-8 (file or environment) crashed the
setup instead of closing the UI with a reason; a password ending in a space cannot be sent as Bearer (documented);
the direct port under a host name outside the allowed list (documented); HRI_COOKIE_SECURE was on only for exactly
"1"; a logout was written without fsync and an unparsable record read as generation 0; and two apps on one host
shared the session cookie name."""

import asyncio
import http.server
import json
import logging
import os
import pathlib
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest import mock

from aiohttp import web

from custom_components.integration_manager import auth as auth_mod
from custom_components.integration_manager.auth import Auth
from tests.fakes import entrypoint_for

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _tmp(test):
    tmp = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, tmp, True)
    return tmp


def _env(**values):
    drop = ("HRI_PASSWORD", "HRI_PASSWORD_FILE", "HRI_COOKIE_SECURE", "HRI_APP", "HRI_INGRESS_USERS")
    env = {k: v for k, v in os.environ.items() if k not in drop}
    return mock.patch.dict(os.environ, {**env, **values}, clear=True)


def _hass(tmp):
    async def job(fn, *args):
        return fn(*args)

    return SimpleNamespace(async_add_executor_job=job, data={}, config=SimpleNamespace(path=lambda *p: os.path.join(tmp, *p)),
                           http=SimpleNamespace(app=SimpleNamespace(middlewares=[])))


def _request(secure=False):
    return SimpleNamespace(headers={}, query={}, cookies={}, path="/", path_qs="/", secure=secure, remote="10.0.0.9")


# ----- S1-5 ---------------------------------------------------------------------------------------

class StatusApiWithPasswordTest(unittest.TestCase):
    """/api/ on the boot status page: with a password, a caller that is not the app's ingress learns only that the
    manager is not up (the healthcheck needs no more), not the version, the phase or a failed restore."""

    def _get(self, path="/api/status", supervisor=None, held=False, **env):
        ep = entrypoint_for(self, _tmp(self), **{"HRI_APP": "", "HRI_INGRESS_USERS": "", "HRI_PASSWORD": "",
                                                "HRI_PASSWORD_FILE": "", **env})
        if held:
            ep._status.update(phase="restore failed", version=None, kind="restore_hold", title="held", backup="b.tar")
        else:
            ep._status.update(phase="apt-get install ffmpeg jq", version="2026.9.3", kind="install", title=None)
        patch = mock.patch.object(ep, "SUPERVISOR_IP", supervisor) if supervisor else mock.patch.dict({})
        with patch:
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ep._StatusHandler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}{path}", headers={"Host": "localhost"})
                try:
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        return resp.status, resp.read()
                except urllib.error.HTTPError as err:
                    with err:
                        return err.code, err.read()
            finally:
                srv.shutdown()
                srv.server_close()

    def test_a_password_hides_the_details(self):
        for env in ({"HRI_PASSWORD": "pw"}, {"HRI_PASSWORD_FILE": "/run/secrets/hri"}):
            with self.subTest(env=env):
                status, body = self._get(**env)
                self.assertEqual(status, 503)
                data = json.loads(body)
                self.assertEqual(set(data), {"installing", "error"})
                self.assertTrue(data["installing"])
                for leak in (b"ffmpeg", b"2026.9.3", b"apt-get"):
                    self.assertNotIn(leak, body)

    def test_a_held_restore_is_not_described_either(self):
        status, body = self._get(held=True, HRI_PASSWORD="pw")
        self.assertEqual(status, 503)
        data = json.loads(body)
        self.assertEqual(set(data), {"installing", "error"})
        self.assertFalse(data["installing"])
        self.assertNotIn(b"b.tar", body)

    def test_without_a_password_nothing_changes(self):
        status, body = self._get()
        data = json.loads(body)
        self.assertEqual((status, data["version"], data["phase"], data["restore_failed"]),
                         (503, "2026.9.3", "apt-get install ffmpeg jq", False))

    def test_the_apps_ingress_still_sees_the_details(self):
        status, body = self._get(supervisor="127.0.0.1", HRI_APP="1", HRI_PASSWORD="pw")
        self.assertEqual((status, json.loads(body)["version"]), (503, "2026.9.3"))


class StatusPageWithPasswordTest(unittest.TestCase):
    """The HTML page of the same server, same rule: with a password, a caller that is not the app's ingress sees that
    Home Assistant is being installed or is not started, not the version, the phase (apt package names) or the backup
    a failed restore needs."""

    _get = StatusApiWithPasswordTest._get

    def test_a_password_hides_the_details(self):
        for env in ({"HRI_PASSWORD": "pw"}, {"HRI_PASSWORD_FILE": "/run/secrets/hri"}):
            with self.subTest(env=env):
                status, body = self._get("/", **env)
                self.assertEqual(status, 503)
                self.assertIn(b"being installed or prepared", body)
                for leak in (b"ffmpeg", b"2026.9.3", b"apt-get"):
                    self.assertNotIn(leak, body)

    def test_a_title_with_the_version_is_hidden_too(self):
        ep = entrypoint_for(self, _tmp(self), HRI_APP="", HRI_INGRESS_USERS="", HRI_PASSWORD="pw", HRI_PASSWORD_FILE="")
        with mock.patch.dict(ep._status, {"title": "Starting Home Assistant 2026.9.3 …", "phase": "removing venv-2026.8.1"}):
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ep._StatusHandler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/", headers={"Host": "localhost"})
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req, timeout=5)
                with ctx.exception as err:
                    body = err.read()
            finally:
                srv.shutdown()
                srv.server_close()
        self.assertNotIn(b"2026.9.3", body)
        self.assertNotIn(b"2026.8.1", body)

    def test_a_held_restore_is_not_described_either(self):
        status, body = self._get("/", held=True, HRI_PASSWORD="pw")
        self.assertEqual(status, 503)
        self.assertIn(b"not started", body)
        self.assertNotIn(b"b.tar", body)
        self.assertNotIn(b"restore", body)

    def test_without_a_password_nothing_changes(self):
        status, body = self._get("/")
        self.assertEqual(status, 503)
        self.assertIn(b"2026.9.3", body)
        self.assertIn(b"apt-get install ffmpeg jq", body)

    def test_the_apps_ingress_still_sees_the_details(self):
        status, body = self._get("/", supervisor="127.0.0.1", HRI_APP="1", HRI_PASSWORD="pw")
        self.assertEqual(status, 503)
        self.assertIn(b"apt-get install ffmpeg jq", body)
        self.assertIn(b"2026.9.3", body)


# ----- S4-2 ---------------------------------------------------------------------------------------

class UndecodablePasswordTest(unittest.TestCase):
    """A password file that is not UTF-8, or HRI_PASSWORD with bytes that are not (os.environ holds them as lone
    surrogates), crashed async_setup: now the UI stays closed and the login page says why."""

    def _file(self, content: bytes):
        path = os.path.join(_tmp(self), "hri_password")
        with open(path, "wb") as fh:
            fh.write(content)
        return path

    def _setup(self, **env):
        with _env(**env):
            return asyncio.run(auth_mod.async_setup_auth(_hass(_tmp(self))))

    def test_a_file_that_is_not_utf8(self):
        with _env(HRI_PASSWORD_FILE=self._file(b"s3\xffcr3t\n")):
            password, reason = auth_mod._configured_password()
        self.assertTrue(password)
        self.assertIn("UTF-8", reason)
        self.assertNotIn("0xff", reason)  # nothing of the password's bytes in the log or on the page
        with self.assertLogs(auth_mod._LOGGER, logging.ERROR):
            auth = self._setup(HRI_PASSWORD_FILE=self._file(b"s3\xffcr3t\n"))
        self.assertTrue(auth.enabled)
        self.assertIn("UTF-8", auth.unusable)

    def test_an_environment_value_that_is_not_utf8(self):
        with _env(HRI_PASSWORD="s3\udcffcr3t"):  # b"s3\xffcr3t" in the process environment
            password, reason = auth_mod._configured_password()
        self.assertTrue(password)
        self.assertIn("UTF-8", reason)
        with self.assertLogs(auth_mod._LOGGER, logging.ERROR):
            auth = self._setup(HRI_PASSWORD="s3\udcffcr3t")
        self.assertTrue(auth.enabled)
        self.assertIn("UTF-8", auth.unusable)
        self.assertFalse(auth.check_password("s3\udcffcr3t"))

    def test_the_status_page_counts_both_as_a_password(self):
        ep = entrypoint_for(self, _tmp(self), HRI_PASSWORD="s3\udcffcr3t", HRI_PASSWORD_FILE="")
        self.assertTrue(ep.password_configured())
        ep = entrypoint_for(self, _tmp(self), HRI_PASSWORD="", HRI_PASSWORD_FILE=self._file(b"\xff"))
        self.assertTrue(ep.password_configured())


# ----- S4-3 / S4-4 --------------------------------------------------------------------------------

class DocsTest(unittest.TestCase):
    def _read(self, name):
        path = ROOT / name
        if not path.is_file():
            self.skipTest(f"{name} not copied next to the tests")
        return path.read_text(encoding="utf-8")

    def test_a_trailing_space_and_bearer(self):
        row = next(line for line in self._read("README.md").splitlines() if line.startswith("| `HRI_PASSWORD` |"))
        self.assertIn("Bearer", row)
        self.assertIn("Bearer", next(p for p in self._read("docs/security.md").split("\n\n") if "ends with a space" in p))

    def test_the_apps_port_under_another_host_name(self):
        access = self._read("docs/app.md").split("## Access", 1)[1].split("\n## ", 1)[0]
        self.assertIn("allowed host names", access)
        self.assertIn("403", access)


# ----- S4-5 ---------------------------------------------------------------------------------------

class CookieSecureTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(auth_mod, "_cookie_secure_warned", False, create=True)
        patch.start()
        self.addCleanup(patch.stop)

    def _secure(self, value):
        resp = web.Response()
        with _env(HRI_COOKIE_SECURE=value):
            auth_mod._set_session_cookie(resp, _request(), "v", 60)
        return bool(resp.cookies[auth_mod.COOKIE]["secure"])

    def test_the_usual_spellings_of_on(self):
        for value in ("1", "1 ", " 1\n", "true", "True", "YES", "on", "On "):
            with self.subTest(value=value):
                self.assertTrue(self._secure(value))

    def test_off_and_unset(self):
        for value in ("", "0", "false", "no", "off", " "):
            with self.subTest(value=value), self.assertNoLogs(auth_mod._LOGGER, logging.WARNING):
                self.assertFalse(self._secure(value))

    def test_an_unknown_value_is_warned_about_once(self):
        with self.assertLogs(auth_mod._LOGGER, logging.WARNING) as logs:
            self.assertFalse(self._secure("maybe"))
            self.assertFalse(self._secure("maybe"))
        self.assertEqual(len([r for r in logs.records if "HRI_COOKIE_SECURE" in r.getMessage()]), 1)

    def test_a_tls_request_is_secure_whatever_the_value(self):
        resp = web.Response()
        with _env(HRI_COOKIE_SECURE="0"):
            auth_mod._set_session_cookie(resp, _request(secure=True), "v", 60)
        self.assertTrue(resp.cookies[auth_mod.COOKIE]["secure"])


# ----- S4-6 ---------------------------------------------------------------------------------------

class RevokedRecordTest(unittest.TestCase):
    def _auth(self):
        return Auth("pw", b"k" * 32, os.path.join(_tmp(self), "integration_manager", "auth_revoked"))

    def test_a_logout_is_synced_to_the_disk(self):
        auth = self._auth()
        os.makedirs(os.path.dirname(auth.revoked_path))
        real = os.fsync
        synced = []
        with mock.patch("os.fsync", side_effect=lambda fd: (synced.append(fd), real(fd))):
            auth.revoke_all()
        self.assertGreaterEqual(len(synced), 2)  # the file, then its directory (the rename)
        again = Auth("pw", b"k" * 32, auth.revoked_path)
        again.load_revoked()
        self.assertEqual(again.generation, auth.generation)
        with open(auth.revoked_path, encoding="utf-8") as fh:
            self.assertEqual(int(fh.read().strip()), auth.generation)  # what an older image reads

    def test_an_unparsable_record_fails_closed(self):
        for content in ("garbage", "", "12\x00"):
            with self.subTest(content=content):
                auth = self._auth()
                os.makedirs(os.path.dirname(auth.revoked_path))
                old = auth.new_session()  # issued at generation 0, before the logout the file recorded
                with open(auth.revoked_path, "w", encoding="utf-8") as fh:
                    fh.write(content)
                before = int(time.time())
                with self.assertLogs(auth_mod._LOGGER, logging.WARNING):
                    auth.load_revoked()
                self.assertGreaterEqual(auth.generation, before)
                self.assertFalse(auth.valid_session(old))
                self.assertTrue(auth.valid_session(auth.new_session()))

    def test_no_record_is_generation_zero(self):
        auth = self._auth()
        auth.load_revoked()
        self.assertEqual(auth.generation, 0)


# ----- X-2 ----------------------------------------------------------------------------------------

class CookieNameTest(unittest.TestCase):
    def _name(self, hostname="a0d7b954-hass-remote-integration", **env):
        with _env(**env), mock.patch("socket.gethostname", return_value=hostname):
            return auth_mod._cookie_name()

    def test_docker_keeps_the_port(self):
        self.assertEqual(self._name(HRI_PORT="8088"), "hri_session_8088")
        self.assertEqual(self._name(HRI_PORT=" "), "hri_session_8087")

    def test_the_app_takes_its_host_name(self):
        a = self._name(HRI_APP="1", HRI_PORT="8087")
        b = self._name("local-hass-remote-integration", HRI_APP="1", HRI_PORT="8087")
        self.assertEqual(a, "hri_session_a0d7b954-hass-remote-integration")
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, auth_mod.LEGACY_COOKIE)

    def test_the_host_name_is_a_cookie_token(self):
        self.assertEqual(self._name("Weird.Host name;=x.", HRI_APP="1"), "hri_session_weird_host_name__x")
        self.assertEqual(self._name("", HRI_APP="1", HRI_PORT="8087"), "hri_session_8087")


if __name__ == "__main__":
    unittest.main()
