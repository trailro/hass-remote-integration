"""Review round 5, auth: the boot status page's /api/ answer names the version and the phase (apt packages
included) without a login even with a password set; a password that is not UTF-8 (file or environment) crashed the
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


if __name__ == "__main__":
    unittest.main()
