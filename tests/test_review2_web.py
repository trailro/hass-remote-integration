"""Review round 2, web: a HRI_PASSWORD of only whitespace turned the login off
(M-01), a query whose "=" arrived as %3D reached process.log unmasked (M-02),
the query scan was quadratic in a run with no query after it, and the password
check against Home Assistant's own routes and middlewares (U-05)."""

import asyncio
import logging
import os
import shutil
import tempfile
import time
import unittest
from contextvars import ContextVar
from types import SimpleNamespace
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import logbuffer
from custom_components.integration_manager import auth as auth_mod
from custom_components.integration_manager.auth import Auth, LoginView


def _tmp(test):
    tmp = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, tmp, True)
    return tmp


def _env(**values):
    env = {k: v for k, v in os.environ.items() if k not in ("HRI_PASSWORD", "HRI_PASSWORD_FILE", "HRI_COOKIE_SECURE")}
    return mock.patch.dict(os.environ, {**env, **values}, clear=True)


def _hass(tmp, app):
    async def job(fn, *args):
        return fn(*args)

    return SimpleNamespace(async_add_executor_job=job, data={}, config=SimpleNamespace(path=lambda *p: os.path.join(tmp, *p)),
                           http=SimpleNamespace(app=app))


# ----- M-01 ---------------------------------------------------------------------------------------

class BlankPasswordTest(unittest.TestCase):
    """HRI_PASSWORD="   " read as "no password": no middleware, the UI and API open, nothing logged.  The
    sibling HRI_PASSWORD_FILE branch fails closed on the same mistake."""

    def test_only_whitespace_keeps_the_ui_closed_and_says_why(self):
        for value in ("   ", " ", "\t", " \t ", " \r\n", "\r\n \r\n"):
            with self.subTest(value=value), _env(HRI_PASSWORD=value):
                password, reason = auth_mod._configured_password()
                self.assertTrue(Auth(password).enabled)
                self.assertIn("only whitespace", reason)
                for guess in ("", " ", "   ", value):
                    self.assertFalse(Auth(password).check_password(guess))

    def test_line_ends_alone_still_mean_no_login(self):
        """An empty `HRI_PASSWORD=` line of an .env saved with Windows line ends arrives as "\\r" (verify.sh
        sources .env): that is the operator writing "no password", pinned in test_r12_web."""
        for value in ("", "\r", "\n", "\r\n"):
            with self.subTest(value=value), _env(HRI_PASSWORD=value):
                self.assertEqual(auth_mod._configured_password(), ("", ""))

    def test_a_password_with_spaces_around_it_is_unchanged(self):
        with _env(HRI_PASSWORD=" pw "):
            self.assertEqual(auth_mod._configured_password(), (" pw ", ""))

    def test_setup_installs_the_check_logs_and_the_login_says_why(self):
        tmp = _tmp(self)
        hass = _hass(tmp, SimpleNamespace(middlewares=[]))
        with _env(HRI_PASSWORD="   "), self.assertLogs(auth_mod._LOGGER, logging.ERROR) as logs:
            auth = asyncio.run(auth_mod.async_setup_auth(hass))
        self.assertEqual(len(hass.http.app.middlewares), 1)
        self.assertIn("only whitespace", "\n".join(logs.output))

        async def payload():
            return {"password": "   "}

        request = SimpleNamespace(content_type="application/json", json=payload, headers={}, cookies={}, remote="10.0.0.9",
                                  secure=False, path="/api/login", path_qs="/api/login", query={})
        resp = asyncio.run(LoginView(auth).post(request))
        self.assertEqual(resp.status, 503)
        self.assertIn("only whitespace", resp.body.decode())


# ----- M-02 ---------------------------------------------------------------------------------------

class EncodedEqualsTest(unittest.TestCase):
    """mask_query_secrets returned early when a line held no "=", so a pair whose "=" was sent as %3D was
    written as it came; the rest of the function did not read %3D as the separator either."""

    def test_a_log_search_with_an_encoded_equals_is_masked(self):
        for line in ('"GET /api/logs?q%3Dhunter2 HTTP/1.1" 200', "GET /api/log_files/tail?id=ab&q%3dhunter2"):
            with self.subTest(line=line):
                self.assertNotIn("hunter2", logbuffer.mask_query_secrets(line))

    def test_a_credential_with_an_encoded_equals_is_masked_its_name_kept(self):
        cases = {
            "GET /api/x?access_token%3Dabc HTTP/1.1": "GET /api/x?access_token%3D*** HTTP/1.1",
            "GET /api/x?a=1&access%5Ftoken%3dabc&b=2": "GET /api/x?a=1&access%5Ftoken%3d***&b=2",
            "GET /x?authSig%3Dabc": "GET /x?authSig%3D***",
        }
        for line, masked in cases.items():
            with self.subTest(line=line):
                self.assertEqual(logbuffer.mask_query_secrets(line), masked)

    def test_plain_names_with_an_encoded_equals_are_left_alone(self):
        for line in ("GET /x?keyword%3Dabc", "GET /x?translation_key%3Dabc", "GET /x?token%3D", "GET /x?a%20b", "GET /x?%3D"):
            with self.subTest(line=line):
                self.assertEqual(logbuffer.mask_query_secrets(line), line)


class LinearScanTest(unittest.TestCase):
    """The query pattern was tried from every character of a run with no query after it: an 8 KB request path
    ending in "?" took ~270 ms of the GIL per access-log line once anything on the line held a "="."""

    def test_a_long_run_without_a_query_is_scanned_once(self):
        for line in ('"GET /' + "y" * 20000 + '? HTTP/1.1" 404 0 "-" "ua a=b"', "? " + "y" * 20000 + " a=b"):
            t0 = time.perf_counter()
            logbuffer.mask_query_secrets(line)
            self.assertLess(time.perf_counter() - t0, 0.2)

    def test_matches_are_the_same_as_before(self):
        cases = {
            "a b/c?token=1 d": "a b/c?token=*** d",
            # the closing quote goes with a masked value (logbuffer's comment on _URL_QUERY)
            '"GET /x?token=1&a=2 HTTP/1.1" "http://h/y?sig=3"': '"GET /x?token=***&a=2 HTTP/1.1" "http://h/y?sig=***',
            "??token=1": "??token=***",
            "x'/p?token=1'": "x'/p?token=***",
        }
        for line, masked in cases.items():
            with self.subTest(line=line):
                self.assertEqual(logbuffer.mask_query_secrets(line), masked)


# ----- U-05 ---------------------------------------------------------------------------------------

class HomeAssistantRoutesTest(unittest.TestCase):
    """The password check is appended to the app's middlewares after Home Assistant's (security filter,
    forwarded, request context, bans, auth, headers, cors), so it runs for every route of that app, HA's own
    included; HA's forwarded middleware answers 400 before it for X-Forwarded-For without use_x_forwarded_for."""

    def test_ha_routes_need_the_password(self):
        try:
            from homeassistant.components.http.forwarded import async_setup_forwarded
            from homeassistant.components.http.request_context import setup_request_context
            from homeassistant.components.http.security_filter import setup_security_filter
        except ImportError as err:  # pragma: no cover - outside the container's HA venv
            self.skipTest(f"Home Assistant's http component is not importable: {err}")
        tmp = _tmp(self)
        static = os.path.join(tmp, "static")
        os.makedirs(static)
        with open(os.path.join(static, "app.js"), "w", encoding="utf-8") as fh:
            fh.write("//")

        async def ok(request):
            return web.Response(text="ok")

        async def main():
            app = web.Application()
            setup_security_filter(app)
            async_setup_forwarded(app, False, [])
            setup_request_context(app, ContextVar("request", default=None))
            with _env(HRI_PASSWORD="pw"):
                await auth_mod.async_setup_auth(_hass(tmp, app))
            app.router.add_get("/api/config", ok)
            app.router.add_get("/api/websocket", ok)
            app.router.add_static("/static/", static)
            out = {}
            async with TestClient(TestServer(app)) as client:
                for path in ("/api/config", "/api/websocket", "/static/app.js"):
                    resp = await client.get(path, allow_redirects=False)
                    out[path] = resp.status
                    resp = await client.get(path, allow_redirects=False, headers={"Authorization": "Bearer pw"})
                    out[path + " bearer"] = resp.status
                resp = await client.get("/api/config", headers={"Authorization": "Bearer pw", "X-Forwarded-For": "1.2.3.4"})
                out["forwarded"] = resp.status
            return out

        out = asyncio.run(main())
        self.assertEqual(out, {"/api/config": 401, "/api/config bearer": 200, "/api/websocket": 401,
                               "/api/websocket bearer": 200, "/static/app.js": 302, "/static/app.js bearer": 200,
                               "forwarded": 400})


if __name__ == "__main__":
    unittest.main()
