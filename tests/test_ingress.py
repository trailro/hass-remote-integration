"""Home Assistant ingress for the app: the Supervisor's requests (transport peer 172.30.32.2, both hops' X-Forwarded-For,
the browser's Host of Home Assistant, the prefix stripped) reach the UI without HRI's password and host guard, before
Home Assistant's forwarded middleware answers 400; nothing else gets that pass.  The policy lets Home Assistant frame the
UI, the boot status page lets the Supervisor through, and app/config.yaml declares the panel.  The relative URLs the
prefix needs: test_relative_urls.py."""

import asyncio
import http.server
import json
import logging
import os
import pathlib
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextvars import ContextVar
from types import SimpleNamespace
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from custom_components.integration_manager import auth as auth_mod
from custom_components.integration_manager import hostguard
from tests.fakes import entrypoint_for

try:
    from custom_components.integration_manager import ingress
except ImportError:  # the tree before ingress: each test that needs it fails on its own
    ingress = None

ROOT = pathlib.Path(__file__).resolve().parent.parent

# what reaches the app from the Supervisor: headers of the browser's request to Home Assistant plus both hops' own
INGRESS_HEADERS = {
    "Host": "ha.example.com",  # the remote name of Home Assistant (Nabu Casa, a domain): not a LAN name
    "X-Forwarded-For": "203.0.113.7, 172.30.32.1",
    "X-Forwarded-Host": "ha.example.com",
    "X-Forwarded-Proto": "https",
    "X-Ingress-Path": "/api/hassio_ingress/tok3n",
    "X-Hass-Source": "core.ingress",
    "X-Remote-User-Id": "abc123",
    "X-Remote-User-Name": "alice",
    "X-Remote-User-Display-Name": "Alice",
}


def _tmp(test):
    tmp = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, tmp, True)
    return tmp


def _env(**values):
    drop = ("HRI_PASSWORD", "HRI_PASSWORD_FILE", "HRI_COOKIE_SECURE", "HRI_APP", "HRI_INGRESS_USERS")
    env = {k: v for k, v in os.environ.items() if k not in drop}
    return mock.patch.dict(os.environ, {**env, **values}, clear=True)


def _hass(tmp, app):
    async def job(fn, *args):
        return fn(*args)

    return SimpleNamespace(async_add_executor_job=job, data={}, config=SimpleNamespace(path=lambda *p: os.path.join(tmp, *p)),
                           http=SimpleNamespace(app=app))


def _ha_http():
    try:
        from homeassistant.components.http.forwarded import async_setup_forwarded
        from homeassistant.components.http.request_context import setup_request_context
        from homeassistant.components.http.security_filter import setup_security_filter
    except ImportError as err:  # pragma: no cover - outside the container's HA venv
        raise unittest.SkipTest(f"Home Assistant's http component is not importable: {err}")
    return async_setup_forwarded, setup_request_context, setup_security_filter


class IngressStackTest(unittest.TestCase):
    """A real aiohttp app with Home Assistant's own first middlewares (security filter, forwarded, request context,
    in HA's order), then HRI's as __init__.async_setup installs them, served on a socket: the transport peer is real."""

    def _run(self, requests, supervisor="127.0.0.1", **env):
        async_setup_forwarded, setup_request_context, setup_security_filter = _ha_http()
        tmp = _tmp(self)
        seen = []

        async def ok(request):
            seen.append({"xff": request.headers.get("X-Forwarded-For"), "xfh": request.headers.get("X-Forwarded-Host"),
                         "ingress": request.get("hri_ingress"), "user": request.get("hri_ingress_user")})
            return web.Response(text="ok")

        async def main():
            app = web.Application()
            setup_security_filter(app)
            async_setup_forwarded(app, False, [])
            setup_request_context(app, ContextVar("request", default=None))
            hass = _hass(tmp, app)
            with _env(HRI_PASSWORD="pw", **env):
                if ingress is not None:
                    ingress.install_ingress(hass, hostguard.CSP)
                hostguard.install_host_guard(hass, SimpleNamespace(settings=SimpleNamespace(data={})))
                await auth_mod.async_setup_auth(hass)
            app.router.add_get("/", ok)
            app.router.add_get("/config", ok)
            app.router.add_post("/api/run/stop", ok)
            out = []
            patch = mock.patch.object(ingress, "SUPERVISOR_IP", supervisor) if ingress is not None else mock.patch.dict({})
            with patch:
                async with TestClient(TestServer(app, host="127.0.0.1")) as client:
                    for method, path, headers in requests:
                        resp = await client.request(method, path, headers=headers, allow_redirects=False)
                        out.append(resp.status)
            return out, [m.__name__ for m in app.middlewares]

        out, names = asyncio.run(main())
        return out, names, seen

    def test_an_ingress_request_is_served_without_the_password(self):
        out, names, seen = self._run([("GET", "/", INGRESS_HEADERS), ("GET", "/config", INGRESS_HEADERS)], HRI_APP="1")
        self.assertEqual(out, [200, 200])
        self.assertEqual(names[:3], ["security_filter_middleware", "hri_ingress", "forwarded_middleware"])
        self.assertEqual(seen[0], {"xff": None, "xfh": None, "ingress": True, "user": "alice"})

    def test_a_state_changing_ingress_request_is_logged_with_the_user(self):
        with self.assertLogs(ingress.__name__ if ingress else "x", logging.INFO) as logs:
            out, _, _ = self._run([("POST", "/api/run/stop", INGRESS_HEADERS)], HRI_APP="1")
        self.assertEqual(out, [200])
        self.assertTrue(any("POST /api/run/stop" in line and "'alice'" in line for line in logs.output), logs.output)

    def test_without_hri_app_nothing_changes(self):
        out, names, seen = self._run([("GET", "/", INGRESS_HEADERS)])
        self.assertEqual(out, [400])  # Home Assistant's forwarded middleware, as before
        self.assertNotIn("hri_ingress", names)
        self.assertEqual(seen, [])

    def test_another_peer_gets_no_pass(self):
        """HRI_APP set, but the request does not come from the Supervisor's address (the port on the host)."""
        lan = {"Host": "10.0.0.2:8087"}
        spoofed = {**lan, "X-Ingress-Path": "/api/hassio_ingress/x", "X-Hass-Source": "core.ingress", "X-Remote-User-Name": "alice"}
        out, _, seen = self._run([
            ("GET", "/", INGRESS_HEADERS),  # X-Forwarded-For from a peer HA does not trust: HA's 400
            ("GET", "/", {"Host": "ha.example.com"}),  # a public name: the host guard's 403
            ("GET", "/", spoofed),  # the ingress headers, from a LAN client: the password is still asked
            ("GET", "/", lan),
            ("GET", "/", {**lan, "Authorization": "Bearer pw"}),
        ], supervisor="172.30.32.2", HRI_APP="1")
        self.assertEqual(out, [400, 403, 302, 302, 200])
        self.assertEqual(seen, [{"xff": None, "xfh": None, "ingress": None, "user": None}])

    def test_ingress_users(self):
        out, _, _ = self._run([
            ("GET", "/", INGRESS_HEADERS),
            ("GET", "/", {**INGRESS_HEADERS, "X-Remote-User-Name": "Bob"}),
            ("GET", "/", {**INGRESS_HEADERS, "X-Remote-User-Name": "carol"}),
            ("GET", "/", {k: v for k, v in INGRESS_HEADERS.items() if k != "X-Remote-User-Name"}),
        ], HRI_APP="1", HRI_INGRESS_USERS=" alice , bob,,")
        self.assertEqual(out, [200, 200, 403, 403])

    def test_empty_ingress_users_is_every_user(self):
        out, _, _ = self._run([("GET", "/", {**INGRESS_HEADERS, "X-Remote-User-Name": "anyone"})], HRI_APP="1", HRI_INGRESS_USERS=" , ")
        self.assertEqual(out, [200])


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(ingress, "custom_components/integration_manager/ingress.py")

    def test_fails_closed_without_the_forwarded_middleware(self):
        async def other(request, handler):
            return await handler(request)

        app = SimpleNamespace(middlewares=[other])
        with _env(HRI_APP="1"), self.assertLogs(ingress._LOGGER, logging.ERROR) as logs:
            self.assertFalse(ingress.install_ingress(SimpleNamespace(http=SimpleNamespace(app=app)), hostguard.CSP))
        self.assertEqual(app.middlewares, [other])
        self.assertIn("forwarded_middleware", "\n".join(logs.output))

    def test_a_frozen_list_is_logged_not_raised(self):
        async def forwarded_middleware(request, handler):
            return await handler(request)

        class Frozen(list):
            def insert(self, *a):
                raise RuntimeError("Cannot modify frozen list.")

        app = SimpleNamespace(middlewares=Frozen([forwarded_middleware]))
        with _env(HRI_APP="1"), self.assertLogs(ingress._LOGGER, logging.ERROR):
            self.assertFalse(ingress.install_ingress(SimpleNamespace(http=SimpleNamespace(app=app)), hostguard.CSP))

    def test_not_an_app_installs_nothing(self):
        app = SimpleNamespace(middlewares=[])
        with _env():
            self.assertFalse(ingress.install_ingress(SimpleNamespace(http=SimpleNamespace(app=app)), hostguard.CSP))
        self.assertEqual(app.middlewares, [])

    def test_the_supervisor_address_is_one_value(self):
        ep = entrypoint_for(self, _tmp(self))
        self.assertEqual(ep.SUPERVISOR_IP, ingress.SUPERVISOR_IP)
        self.assertEqual(ingress.SUPERVISOR_IP, "172.30.32.2")


class PolicyTest(unittest.TestCase):
    def test_home_assistant_may_frame_the_ui(self):
        self.assertIn("frame-ancestors 'self'", hostguard.CSP)
        self.assertNotIn("frame-ancestors 'none'", hostguard.CSP)
        self.assertIn("base-uri 'none'", hostguard.CSP)


class StatusServerTest(unittest.TestCase):
    """The page entrypoint.py serves while Home Assistant installs: the Supervisor's proxied request carries Home
    Assistant's public Host, which the DNS-rebinding rule refuses."""

    def _get(self, supervisor=None, headers=None, **env):
        ep = entrypoint_for(self, _tmp(self), **{"HRI_APP": "", "HRI_INGRESS_USERS": "", **env})  # "" is unset for both
        ep._status.update(phase="pip", version="2026.9.3", kind="install", title=None)
        patch = mock.patch.object(ep, "SUPERVISOR_IP", supervisor) if supervisor else mock.patch.dict({})
        with patch:
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ep._StatusHandler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/", headers=headers or INGRESS_HEADERS)
                try:
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        return resp.status
                except urllib.error.HTTPError as err:
                    with err:
                        return err.code
            finally:
                srv.shutdown()
                srv.server_close()

    def test_the_supervisor_is_let_through_as_the_app(self):
        self.assertEqual(self._get(supervisor="127.0.0.1", HRI_APP="1"), 503)  # the install page, with its 503

    def test_not_an_app_or_another_peer_is_refused(self):
        self.assertEqual(self._get(supervisor="127.0.0.1"), 403)
        self.assertEqual(self._get(HRI_APP="1"), 403)

    def test_ingress_users(self):
        self.assertEqual(self._get(supervisor="127.0.0.1", HRI_APP="1", HRI_INGRESS_USERS="alice"), 503)
        self.assertEqual(self._get(supervisor="127.0.0.1", HRI_APP="1", HRI_INGRESS_USERS="dave"), 403)


class AppIngressConfigTest(unittest.TestCase):
    def setUp(self):
        if not (ROOT / "app" / "config.yaml").is_file():
            self.skipTest("app/ not copied next to the tests")
        import yaml
        self.cfg = yaml.safe_load((ROOT / "app" / "config.yaml").read_text(encoding="utf-8"))
        self.tr = yaml.safe_load((ROOT / "app" / "translations" / "en.yaml").read_text(encoding="utf-8"))

    def test_the_panel(self):
        self.assertIs(self.cfg["ingress"], True)
        self.assertEqual(self.cfg["ingress_port"], 8087)
        self.assertEqual(f"{self.cfg['ingress_port']}/tcp" in self.cfg["ports"], True)  # the same server as the port
        self.assertIs(self.cfg["ingress_stream"], True)  # uploads above the Supervisor's 16 MiB buffer
        self.assertRegex(self.cfg["panel_icon"], r"^mdi:[a-z0-9-]+$")
        self.assertEqual(self.cfg["panel_title"], "HRI")
        self.assertEqual(self.cfg["ports"], {"8087/tcp": 8087})  # the direct port stays
        self.assertNotIn("webui", self.cfg)  # frenck/action-app-linter: "'webui' should be removed, Ingress is enabled"
        self.assertNotIn("panel_admin", self.cfg)  # the default (admins see the panel): the linter refuses a default

    def test_the_ingress_users_option(self):
        self.assertEqual(self.cfg["schema"]["ingress_users"], ["str?"])  # an optional list (supervisor apps/options.py)
        self.assertNotIn("ingress_users", self.cfg["options"])
        self.assertIn("ingress_users", self.tr["configuration"])
        self.assertIn("HRI_INGRESS_USERS", self.tr["configuration"]["ingress_users"]["description"])

    def test_the_list_becomes_a_comma_separated_variable(self):
        tmp = _tmp(self)
        ep = entrypoint_for(self, tmp)
        options = os.path.join(tmp, "options.json")
        for value, want in ((["alice", " bob ", ""], "alice,bob"), ([], None), (None, None)):
            with open(options, "w", encoding="utf-8") as fh:
                json.dump({} if value is None else {"ingress_users": value}, fh)
            with self.subTest(value=value), mock.patch.dict(os.environ, {"SUPERVISOR_TOKEN": "t", "HRI_INGRESS_USERS": "old"}):
                ep.apply_app_options(options)
                self.assertEqual(os.environ.get("HRI_INGRESS_USERS"), want)


if __name__ == "__main__":
    unittest.main()
