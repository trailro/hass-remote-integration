"""The app on the host network (an HRI Manager instance with `host_network: true` and `ingress_port: 0`).

- The entrypoint reads GET /addons/self/info once, while it has the token, and exports what it says: the port the
  Supervisor gives the app (HRI_PORT, for the status page, Home Assistant and the image's HEALTHCHECK through
  PORT_FILE), the network (HRI_HOST_NETWORK) and a manager instance's name from its slug (HRI_INSTANCE).
- Without a password, the port answers nothing but ingress there: it is on every interface of the host, the LAN too
  (auth.py's guard, and the status page served while Home Assistant installs).  With one, the password guard as ever.
- Home Assistant's zeroconf does not announce this headless Home Assistant on the LAN.
- The session cookie takes the port there: the host name is the host's, the port the app's own."""

import asyncio
import inspect
import http.client
import http.server
import json
import logging
import os
import pathlib
import re
import sys
import threading
import types
import unittest
from contextvars import ContextVar
from types import SimpleNamespace
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from custom_components.integration_manager import auth as auth_mod
from custom_components.integration_manager import hostguard, ingress
from tests.fakes import entrypoint_for
from tests.test_ingress import INGRESS_HEADERS, _env, _ha_http, _hass, _tmp

ROOT = pathlib.Path(__file__).resolve().parent.parent
LAN = {"Host": "10.0.0.2:62345"}
NOT_HOST = {"HRI_HOST_NETWORK": ""}  # "" is not "1": the container the tests run in sets none of these


class AppInfoTest(unittest.TestCase):
    VARS = ("HRI_PORT", "HRI_HOST_NETWORK", "HRI_INSTANCE", "HRI_APP_WATCHDOG")

    def setUp(self):
        self.tmp = _tmp(self)
        self.ep = entrypoint_for(self, self.tmp, HRI_PORT="8087")
        self.lines = []
        patch = mock.patch.object(self.ep, "log", self.lines.append)
        patch.start()
        self.addCleanup(patch.stop)

    def _apply(self, info, owns=False, **env):
        """What apply_app_info leaves in an environment of HRI_PORT=8087 and ``env`` (nothing else of VARS set)."""
        with mock.patch.dict(os.environ, {"HRI_PORT": "8087", **env}), \
                mock.patch.object(self.ep, "owns_address", lambda address: owns):
            for var in self.VARS:
                if var not in env and var != "HRI_PORT":
                    os.environ.pop(var, None)
            self.ep.apply_app_info(info)
            return {v: os.environ.get(v) for v in self.VARS}

    def test_the_marker_the_manager_greps_for(self):
        self.assertIs(self.ep.APP_DYNAMIC_PORT, True)
        text = (ROOT / "entrypoint.py").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"^APP_DYNAMIC_PORT = True$", text, re.M), ["APP_DYNAMIC_PORT = True"])

    def test_the_supervisors_port_host_network_and_instance(self):
        got = self._apply({"ingress_port": 62345, "host_network": True, "slug": "local_hri_garage", "watchdog": True})
        self.assertEqual(got, {"HRI_PORT": "62345", "HRI_HOST_NETWORK": "1", "HRI_INSTANCE": "garage", "HRI_APP_WATCHDOG": "1"})

    def test_the_stock_app_changes_nothing(self):
        got = self._apply({"ingress_port": 8087, "host_network": False, "slug": "5c53de3b_hass_remote_integration"})
        self.assertEqual(got, {"HRI_PORT": "8087", "HRI_HOST_NETWORK": None, "HRI_INSTANCE": None, "HRI_APP_WATCHDOG": None})

    def test_a_port_that_is_not_one_keeps_hri_port(self):
        for port in (0, 65536, -1, "62345", True, 62345.0, None):
            with self.subTest(port=port):
                self.assertEqual(self._apply({"ingress_port": port, "host_network": False})["HRI_PORT"], "8087")

    def test_the_network_is_never_guessed_open(self):
        """host_network unreadable: the host's hassio bridge address decides, never a default of False."""
        for info in (None, {}, {"host_network": "true"}, {"host_network": 1}):
            for owns in (True, False):
                with self.subTest(info=info, owns=owns):
                    got = self._apply(info, owns, HRI_HOST_NETWORK="stale")
                    self.assertEqual(got["HRI_HOST_NETWORK"], "1" if owns else None)
        self.assertIsNone(self._apply({"host_network": False}, owns=True, HRI_HOST_NETWORK="1")["HRI_HOST_NETWORK"])

    def test_the_address_test(self):
        self.assertTrue(self.ep.owns_address("127.0.0.1"))
        self.assertFalse(self.ep.owns_address("192.0.2.123"))  # TEST-NET-1: nobody's
        self.assertEqual(self.ep.HASSIO_GATEWAY, "172.30.32.1")

    def test_only_a_manager_instance_slug_gives_a_name(self):
        for slug in ("local_hri_Garage", "local_hri_", "local_hri_1abc", "local_hri_a-b", "local_hri_x\n",
                     "local_hri_" + "a" * 21, "hri_garage", "local_hri_garage/..", "xlocal_hri_garage",
                     "5c53de3b_hass_remote_integration", None, 7, ["local_hri_garage"]):
            with self.subTest(slug=slug):
                self.assertIsNone(self._apply({"slug": slug})["HRI_INSTANCE"])
        self.assertEqual(self._apply({"slug": "local_hri_a" + "b_9" * 6})["HRI_INSTANCE"], "a" + "b_9" * 6)

    def test_a_set_instance_is_never_overridden(self):
        for value in ("kitchen", ""):
            with self.subTest(value=value):
                self.assertEqual(self._apply({"slug": "local_hri_garage"}, HRI_INSTANCE=value)["HRI_INSTANCE"], value)

    def _main(self, env, info):
        options = os.path.join(self.tmp, "options.json")
        with open(options, "w", encoding="utf-8") as fh:
            json.dump({}, fh)
        seen = {}

        def prepare():
            seen.update(port=self.ep.PORT, env={v: os.environ.get(v) for v in ("HRI_PORT", "HRI_HOST_NETWORK", "HRI_INSTANCE")})
            raise SystemExit(7)

        with mock.patch.dict(os.environ, env), \
                mock.patch.object(self.ep, "APP_OPTIONS_FILE", options), \
                mock.patch.object(self.ep, "enable_app_watchdog", lambda token: None), \
                mock.patch.object(self.ep, "read_app_info", mock.Mock(return_value=info)) as read, \
                mock.patch.object(self.ep, "owns_address", lambda address: False), \
                mock.patch.object(self.ep, "_prepare", prepare), \
                mock.patch.object(self.ep, "start_status_server", lambda: None), \
                mock.patch.object(self.ep, "restrict_umask", lambda: 0):
            if "SUPERVISOR_TOKEN" not in env:
                os.environ.pop("SUPERVISOR_TOKEN", None)
                os.environ.pop("HASSIO_TOKEN", None)
            with self.assertRaises(SystemExit):
                self.ep.main()
        return seen, read

    def test_main_listens_on_the_supervisors_port_and_writes_it_for_the_healthcheck(self):
        info = {"ingress_port": 62345, "host_network": True, "slug": "local_hri_garage", "watchdog": True}
        seen, read = self._main({"SUPERVISOR_TOKEN": "t0ken"}, info)
        read.assert_called_once_with("t0ken")
        self.assertEqual(seen, {"port": 62345, "env": {"HRI_PORT": "62345", "HRI_HOST_NETWORK": "1", "HRI_INSTANCE": "garage"}})
        with open(self.ep.PORT_FILE, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "62345\n")
        self.assertTrue(any("host network without a password" in line for line in self.lines), self.lines)
        self.assertFalse([line for line in self.lines if "t0ken" in line])

    def test_the_restarted_entrypoint_keeps_what_it_inherits(self):
        """A restart in place has no token: it asks nothing and keeps the port, the network and the name."""
        inherited = {"HRI_APP": "1", "HRI_PORT": "62345", "HRI_HOST_NETWORK": "1", "HRI_INSTANCE": "garage"}
        seen, read = self._main(inherited, None)
        read.assert_not_called()
        self.assertEqual(seen, {"port": 62345, "env": {"HRI_PORT": "62345", "HRI_HOST_NETWORK": "1", "HRI_INSTANCE": "garage"}})
        self.assertFalse(os.path.exists(self.ep.PORT_FILE), "the first start's file stays as it is")

    def test_docker_writes_no_port_file(self):
        seen, read = self._main({"HRI_PORT": "8090"}, None)
        read.assert_not_called()
        self.assertEqual(seen["port"], 8090)
        self.assertFalse(os.path.exists(self.ep.PORT_FILE))

    def test_an_unwritable_port_file_is_logged_not_fatal(self):
        self.ep.PORT_FILE = os.path.join(self.tmp, "missing", "hri-port")
        self.ep.write_port_file(62345)
        self.assertTrue(any("not written" in line for line in self.lines), self.lines)


class StatusPageTest(unittest.TestCase):
    """The page served while Home Assistant installs, on the same port."""

    def _get(self, path="/", supervisor=None, headers=None, **env):
        env = {"HRI_APP": "", "HRI_INGRESS_USERS": "", "HRI_PASSWORD": "", "HRI_PASSWORD_FILE": "", **NOT_HOST, **env}
        ep = entrypoint_for(self, _tmp(self), **env)
        ep._status.update(phase="pip", version="2026.9.3", kind="install", title=None)
        patch = mock.patch.object(ep, "SUPERVISOR_IP", supervisor) if supervisor else mock.patch.dict({})
        with patch:
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ep._StatusHandler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
                try:
                    conn.request("GET", path, headers=headers or LAN)
                    resp = conn.getresponse()
                    return resp.status, resp.read().decode("utf-8", "replace")
                finally:
                    conn.close()
            finally:
                srv.shutdown()
                srv.server_close()

    def test_without_a_password_the_lan_is_refused(self):
        for path in ("/", "/api/status", "/api/diag/health"):
            with self.subTest(path=path):
                status, body = self._get(path, HRI_APP="1", HRI_HOST_NETWORK="1")
                self.assertEqual(status, 403)
                self.assertIn("Set the app", body)

    def test_the_healthcheck_path_still_answers(self):
        self.assertEqual(self._get("/api/alive", HRI_APP="1", HRI_HOST_NETWORK="1"), (200, '{"alive": true}'))

    def test_ingress_is_served(self):
        self.assertEqual(self._get(supervisor="127.0.0.1", headers=INGRESS_HEADERS, HRI_APP="1", HRI_HOST_NETWORK="1")[0], 503)

    def test_with_a_password_or_off_the_host_network_nothing_changes(self):
        self.assertEqual(self._get(HRI_APP="1", HRI_HOST_NETWORK="1", HRI_PASSWORD="pw")[0], 503)
        self.assertEqual(self._get(HRI_APP="1")[0], 503)
        self.assertEqual(self._get(HRI_HOST_NETWORK="1")[0], 503, "a Docker install: HRI_APP is the app's own marker")


class HomeAssistantPortTest(unittest.TestCase):
    """HRI's middlewares as __init__.async_setup installs them, after Home Assistant's first ones, on a real socket."""

    def _run(self, requests, supervisor=False, **env):
        async_setup_forwarded, setup_request_context, setup_security_filter = _ha_http()
        tmp = _tmp(self)

        async def ok(request):
            return web.Response(text="ok")

        async def main():
            app = web.Application()
            setup_security_filter(app)
            async_setup_forwarded(app, False, [])
            setup_request_context(app, ContextVar("request", default=None))
            hass = _hass(tmp, app)
            with _env(**{**NOT_HOST, **env}):
                ingress.install_ingress(hass, hostguard.CSP)
                hostguard.install_host_guard(hass, SimpleNamespace(settings=SimpleNamespace(data={})))
                await auth_mod.async_setup_auth(hass)
            app.router.add_get("/", ok)
            app.router.add_post("/api/webhook/x", ok)
            out = []
            with mock.patch.object(ingress, "SUPERVISOR_IP", "127.0.0.1" if supervisor else "172.30.32.2"):
                async with TestClient(TestServer(app, host="127.0.0.1")) as client:
                    for method, path, headers in requests:
                        resp = await client.request(method, path, headers=headers, allow_redirects=False)
                        out.append((resp.status, await resp.text(), resp.headers.get("Content-Security-Policy")))
            return out

        return asyncio.run(main())

    def test_without_a_password_the_lan_gets_403(self):
        with self.assertLogs(auth_mod._LOGGER, logging.WARNING) as logs:
            out = self._run([("GET", "/", LAN), ("GET", "/api/alive", LAN), ("POST", "/api/webhook/x", LAN),
                             ("GET", "/login", LAN)], HRI_APP="1", HRI_HOST_NETWORK="1")
        self.assertEqual([status for status, _, _ in out], [403] * 4)
        self.assertEqual(out[0][1], auth_mod.LAN_REFUSED)
        self.assertEqual(out[0][2], hostguard.CSP)
        self.assertTrue(any("host network without a password" in line for line in logs.output), logs.output)

    def test_ingress_is_served(self):
        out = self._run([("GET", "/", INGRESS_HEADERS)], supervisor=True, HRI_APP="1", HRI_HOST_NETWORK="1")
        self.assertEqual(out[0][:2], (200, "ok"))

    def test_with_a_password_the_password_guard_applies(self):
        out = self._run([("GET", "/", LAN), ("GET", "/", {**LAN, "Authorization": "Bearer pw"})],
                        HRI_APP="1", HRI_HOST_NETWORK="1", HRI_PASSWORD="pw")
        self.assertEqual([status for status, _, _ in out], [302, 200])

    def test_off_the_host_network_or_as_docker_nothing_changes(self):
        self.assertEqual(self._run([("GET", "/", LAN)], HRI_APP="1")[0][:2], (200, "ok"))
        self.assertEqual(self._run([("GET", "/", LAN)], HRI_HOST_NETWORK="1")[0][:2], (200, "ok"))

    def test_a_frozen_app_refuses_to_run_open(self):
        class Frozen(list):
            def append(self, *a):
                raise RuntimeError("frozen")

        hass = _hass(_tmp(self), SimpleNamespace(middlewares=Frozen()))
        with _env(HRI_APP="1", HRI_HOST_NETWORK="1"), self.assertLogs(auth_mod._LOGGER, logging.ERROR), \
                self.assertRaises(RuntimeError):
            asyncio.run(auth_mod.async_setup_auth(hass))


class CookieNameTest(unittest.TestCase):
    def _name(self, **env):
        with _env(**env), mock.patch("socket.gethostname", return_value="homeassistant"):
            return auth_mod._cookie_name()

    def test_on_the_host_network_the_port_names_it(self):
        """The host name is the host's there, the same for every app; the Supervisor's port is the app's alone."""
        self.assertEqual(self._name(HRI_APP="1", HRI_HOST_NETWORK="1", HRI_PORT="62345"), "hri_session_62345")
        self.assertEqual(self._name(HRI_APP="1", HRI_HOST_NETWORK="", HRI_PORT="8087"), "hri_session_homeassistant")


if __name__ == "__main__":
    unittest.main()
