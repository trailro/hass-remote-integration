"""The app on the host network (an HRI Manager instance with `host_network: true` and `ingress_port: 0`).

- The entrypoint reads GET /addons/self/info once, while it has the token, and exports what it says: the port the
  Supervisor gives the app (HRI_PORT, for the status page, Home Assistant and the image's HEALTHCHECK through
  PORT_FILE), the network (HRI_HOST_NETWORK) and a manager instance's name from its slug (HRI_INSTANCE).
- Without a password, the port answers nothing but ingress there: it is on every interface of the host, the LAN too
  (auth.py's guard, and the status page served while Home Assistant installs).  With one, the password guard as ever.
- Home Assistant's zeroconf and ssdp do not announce this headless Home Assistant on the LAN.
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
    VARS = ("HRI_PORT", "HRI_HOST_NETWORK", "HRI_INSTANCE", "HRI_APP_WATCHDOG", "HRI_INSTANCE_UNKNOWN")

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
        self.assertEqual(got, {"HRI_PORT": "62345", "HRI_HOST_NETWORK": "1", "HRI_INSTANCE": "garage", "HRI_APP_WATCHDOG": "1",
                               "HRI_INSTANCE_UNKNOWN": None})

    def test_the_stock_app_changes_nothing(self):
        got = self._apply({"ingress_port": 8087, "host_network": False, "slug": "5c53de3b_hass_remote_integration"})
        self.assertEqual(got, {"HRI_PORT": "8087", "HRI_HOST_NETWORK": None, "HRI_INSTANCE": None, "HRI_APP_WATCHDOG": None,
                               "HRI_INSTANCE_UNKNOWN": None})

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

    def _main(self, env, info, owns=False, order=None):
        options = os.path.join(self.tmp, "options.json")
        with open(options, "w", encoding="utf-8") as fh:
            json.dump({}, fh)
        seen = {}
        order = [] if order is None else order

        def prepare():
            seen.update(port=self.ep.PORT, env={v: os.environ.get(v) for v in ("HRI_PORT", "HRI_HOST_NETWORK", "HRI_INSTANCE")})
            if os.environ.get("HRI_INSTANCE_UNKNOWN"):
                seen["unknown"] = os.environ["HRI_INSTANCE_UNKNOWN"]
            raise SystemExit(7)

        def read(token):
            order.append("read")
            return info

        with mock.patch.dict(os.environ, env), \
                mock.patch.object(self.ep, "APP_OPTIONS_FILE", options), \
                mock.patch.object(self.ep, "enable_app_watchdog", lambda token: order.append("watchdog")), \
                mock.patch.object(self.ep, "read_app_info", mock.Mock(side_effect=read)) as read_mock, \
                mock.patch.object(self.ep, "owns_address", lambda address: owns), \
                mock.patch.object(self.ep, "_prepare", prepare), \
                mock.patch.object(self.ep, "start_status_server", lambda: order.append("listen")), \
                mock.patch.object(self.ep, "restrict_umask", lambda: 0):
            for var in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN", "HRI_INSTANCE", "HRI_INSTANCE_UNKNOWN"):
                if var not in env:
                    os.environ.pop(var, None)
            with self.assertRaises(SystemExit) as ctx:
                self.ep.main()
        seen["exit"] = ctx.exception.code
        return seen, read_mock

    def test_main_listens_on_the_supervisors_port_and_writes_it_for_the_healthcheck(self):
        info = {"ingress_port": 62345, "host_network": True, "slug": "local_hri_garage", "watchdog": True}
        seen, read = self._main({"SUPERVISOR_TOKEN": "t0ken"}, info)
        read.assert_called_once_with("t0ken")
        self.assertEqual(seen, {"port": 62345, "env": {"HRI_PORT": "62345", "HRI_HOST_NETWORK": "1", "HRI_INSTANCE": "garage"},
                                "exit": 7})
        with open(self.ep.PORT_FILE, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "62345\n")
        self.assertTrue(any("host network without a password" in line for line in self.lines), self.lines)
        self.assertFalse([line for line in self.lines if "t0ken" in line])

    def test_the_restarted_entrypoint_keeps_what_it_inherits(self):
        """A restart in place has no token: it asks nothing and keeps the port, the network and the name."""
        inherited = {"HRI_APP": "1", "HRI_PORT": "62345", "HRI_HOST_NETWORK": "1", "HRI_INSTANCE": "garage"}
        seen, read = self._main(inherited, None)
        read.assert_not_called()
        self.assertEqual(seen, {"port": 62345, "env": {"HRI_PORT": "62345", "HRI_HOST_NETWORK": "1", "HRI_INSTANCE": "garage"},
                                "exit": 7})
        self.assertFalse(os.path.exists(self.ep.PORT_FILE), "the first start's file stays as it is")

    def test_docker_writes_no_port_file(self):
        seen, read = self._main({"HRI_PORT": "8090"}, None)
        read.assert_not_called()
        self.assertEqual(seen["port"], 8090)
        self.assertFalse(os.path.exists(self.ep.PORT_FILE))

    def test_the_info_is_asked_again_before_it_counts_as_unreadable(self):
        answer = mock.MagicMock()
        answer.__enter__.return_value.read.return_value = json.dumps({"data": {"ingress_port": 62345}}).encode()
        with mock.patch.object(self.ep.urllib.request, "urlopen", side_effect=[TimeoutError(), OSError(), answer]) as urlopen, \
                mock.patch.object(self.ep.time, "sleep") as sleep:
            self.assertEqual(self.ep.read_app_info("t0ken"), {"ingress_port": 62345})
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], list(self.ep.APP_INFO_RETRY_DELAYS[:2]))
        self.assertLessEqual(sum(self.ep.APP_INFO_RETRY_DELAYS), 10, "short: the Supervisor waits for the app to start")
        self.assertEqual(self.lines, [])

    def test_unreadable_info_on_the_host_network_exits_before_listening(self):
        """The image's 8087 is a port on the LAN there, and not the one the Supervisor proxies ingress to: nothing
        listens, and the exit is after the Watchdog was turned on, so the Supervisor starts a fresh container."""
        order = []
        seen, read = self._main({"SUPERVISOR_TOKEN": "t0ken"}, None, owns=True, order=order)
        self.assertEqual(seen, {"exit": 1})
        self.assertEqual(order, ["watchdog", "read"], "nothing listens")
        self.assertFalse(os.path.exists(self.ep.PORT_FILE))
        self.assertTrue(any("host network" in line and "not listening" in line and "exiting" in line
                            for line in self.lines), self.lines)
        self.assertFalse([line for line in self.lines if "t0ken" in line])

    def test_unreadable_info_off_the_host_network_runs_with_the_instance_unknown(self):
        order = []
        seen, _ = self._main({"SUPERVISOR_TOKEN": "t0ken"}, None, owns=False, order=order)
        self.assertEqual(seen, {"port": 8087, "env": {"HRI_PORT": "8087", "HRI_HOST_NETWORK": None, "HRI_INSTANCE": None},
                                "unknown": "1", "exit": 7})
        self.assertEqual(order, ["watchdog", "read", "listen"])
        seen, _ = self._main({"SUPERVISOR_TOKEN": "t0ken", "HRI_INSTANCE": "kitchen"}, None)
        self.assertEqual((seen["env"]["HRI_INSTANCE"], seen.get("unknown")), ("kitchen", None), "set explicitly: known")

    def test_readable_info_clears_the_instance_unknown(self):
        for info in ({"slug": "local_hri_garage"}, {"slug": "5c53de3b_hass_remote_integration"}, {}):
            with self.subTest(info=info):
                self.assertIsNone(self._apply(info, HRI_INSTANCE_UNKNOWN="1")["HRI_INSTANCE_UNKNOWN"])

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

            insert = append

        hass = _hass(_tmp(self), SimpleNamespace(middlewares=Frozen()))
        with _env(HRI_APP="1", HRI_HOST_NETWORK="1"), self.assertLogs(auth_mod._LOGGER, logging.ERROR), \
                self.assertRaises(RuntimeError):
            asyncio.run(auth_mod.async_setup_auth(hass))


class RealHttpChainTest(unittest.TestCase):
    """Home Assistant's own HomeAssistantHTTP, initialised as its http component does with the defaults HRI runs with
    (no http section: HTTP_SCHEMA({})), its auth manager real too, and HRI's middlewares installed as
    __init__.async_setup does, on a real socket: a HomeAssistantView, the websocket upgrade and a request carrying
    X-Forwarded-For from the LAN get the guard's 403, ingress is served."""

    def setUp(self):
        try:
            from homeassistant import core, loader
            from homeassistant.auth import auth_manager_from_config
            from homeassistant.components import http as ha_http
            from homeassistant.components.websocket_api.http import WebsocketAPIView
            from homeassistant.helpers import device_registry as dr
        except ImportError as err:  # pragma: no cover - outside the container's HA venv
            self.skipTest(f"Home Assistant's http component is not importable: {err}")
        self.core, self.loader, self.dr = core, loader, dr
        self.auth_manager_from_config, self.ha_http, self.websocket_view = auth_manager_from_config, ha_http, WebsocketAPIView

    def _run(self, requests, supervisor=False, **env):
        from homeassistant.components.http import HomeAssistantView

        class Api(HomeAssistantView):
            url, name, requires_auth = "/api/hri_probe", "api:hri_probe", True

            async def get(self, request):
                return web.Response(text="api")

        class Open(HomeAssistantView):
            url, name, requires_auth = "/hri_open", "hri_open", False

            async def get(self, request):
                return web.Response(text="open")

        async def main():
            hass = self.core.HomeAssistant(_tmp(self))
            self.loader.async_setup(hass)
            self.dr.async_setup(hass)
            await self.dr.async_load(hass, load_empty=True)
            hass.auth = await self.auth_manager_from_config(hass, [], [])
            conf = self.ha_http.HTTP_SCHEMA({})
            server = self.ha_http.HomeAssistantHTTP(hass, ssl_certificate=None, ssl_peer_certificate=None, ssl_key=None,
                                                    server_host=["127.0.0.1"], server_port=0, trusted_proxies=[],
                                                    ssl_profile=conf["ssl_profile"])
            await server.async_initialize(cors_origins=conf["cors_allowed_origins"],
                                          use_x_forwarded_for=conf.get("use_x_forwarded_for", False),
                                          login_threshold=conf["login_attempts_threshold"],
                                          is_ban_enabled=conf["ip_ban_enabled"], use_x_frame_options=conf["use_x_frame_options"])
            hass.http = server
            try:
                with _env(**{**NOT_HOST, **env}):
                    self.assertTrue(ingress.install_ingress(hass, hostguard.CSP))
                    hostguard.install_host_guard(hass, SimpleNamespace(settings=SimpleNamespace(data={})))
                    await auth_mod.async_setup_auth(hass)
                for view in (Api, Open, self.websocket_view):
                    server.register_view(view)
                out = []
                with mock.patch.object(ingress, "SUPERVISOR_IP", "127.0.0.1" if supervisor else "172.30.32.2"):
                    async with TestClient(TestServer(server.app, host="127.0.0.1")) as client:
                        for path, headers in requests:
                            resp = await client.get(path, headers=headers, allow_redirects=False)
                            out.append((resp.status, await resp.text()))
                return out
            finally:
                await hass.async_stop(force=True)

        return asyncio.run(main())

    WS = {**LAN, "Connection": "Upgrade", "Upgrade": "websocket", "Sec-WebSocket-Version": "13",
          "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="}
    REQUESTS = [("/api/hri_probe", {**LAN, "X-Forwarded-For": "203.0.113.9"}), ("/api/hri_probe", LAN),
                ("/hri_open", LAN), ("/api/websocket", WS), ("/api/alive", LAN)]

    def test_without_a_password_the_lan_gets_403(self):
        with self.assertLogs(auth_mod._LOGGER, logging.WARNING):
            out = self._run(self.REQUESTS, HRI_APP="1", HRI_HOST_NETWORK="1")
        self.assertEqual(out, [(403, auth_mod.LAN_REFUSED)] * len(self.REQUESTS))

    def test_ingress_in_the_supervisors_spelling_is_served(self):
        with self.assertLogs(auth_mod._LOGGER, logging.WARNING):
            out = self._run([("/hri_open", INGRESS_HEADERS)], supervisor=True, HRI_APP="1", HRI_HOST_NETWORK="1")
        self.assertEqual(out, [(200, "open")])
        respelled = {("x-remote-user-name" if k == "X-Remote-User-Name" else k): v for k, v in INGRESS_HEADERS.items()}
        with self.assertLogs(auth_mod._LOGGER, logging.WARNING):
            self.assertEqual(self._run([("/hri_open", respelled)], supervisor=True, HRI_APP="1", HRI_HOST_NETWORK="1")[0][0], 403)

    def test_off_the_host_network_home_assistant_answers(self):
        """The same chain without the guard: what the 403s above replace."""
        out = self._run(self.REQUESTS[:3], HRI_APP="1")
        self.assertEqual([status for status, _ in out], [400, 401, 200])


class CookieNameTest(unittest.TestCase):
    def _name(self, **env):
        with _env(**env), mock.patch("socket.gethostname", return_value="homeassistant"):
            return auth_mod._cookie_name()

    def test_on_the_host_network_the_port_names_it(self):
        """The host name is the host's there, the same for every app; the Supervisor's port is the app's alone."""
        self.assertEqual(self._name(HRI_APP="1", HRI_HOST_NETWORK="1", HRI_PORT="62345"), "hri_session_62345")
        self.assertEqual(self._name(HRI_APP="1", HRI_HOST_NETWORK="", HRI_PORT="8087"), "hri_session_homeassistant")


class ZeroconfTest(unittest.TestCase):
    """zeroconf's async_setup announces Home Assistant by calling a function of its module once Home Assistant starts."""

    def setUp(self):
        try:
            import run
            from homeassistant.components import zeroconf
        except ImportError as err:  # pragma: no cover - outside the container's HA venv
            self.skipTest(f"Home Assistant's zeroconf is not importable: {err}")
        self.run, self.zeroconf = run, zeroconf
        original = getattr(zeroconf, run.ZEROCONF_ANNOUNCE)
        self.addCleanup(setattr, zeroconf, run.ZEROCONF_ANNOUNCE, original)
        self.original = original

    def test_the_announcement_is_looked_up_by_name_when_it_runs(self):
        """What the replacement relies on, in this Home Assistant (CI runs it on the floor version too): the start
        callback loads the name from the module's globals at call time, not a reference bound at import."""
        start = next(c for c in self.zeroconf.async_setup.__code__.co_consts
                     if inspect.iscode(c) and c.co_name == "_async_zeroconf_hass_start")
        self.assertIn(self.run.ZEROCONF_ANNOUNCE, start.co_names)
        self.assertNotIn(self.run.ZEROCONF_ANNOUNCE, start.co_freevars)
        self.assertIn("_home-assistant._tcp", self.zeroconf.ZEROCONF_TYPE)

    def test_on_the_host_network_nothing_is_announced(self):
        with mock.patch.dict(os.environ, {"HRI_HOST_NETWORK": "1"}):
            self.assertTrue(self.run._suppress_zeroconf_announcement())
        replaced = getattr(self.zeroconf, self.run.ZEROCONF_ANNOUNCE)
        self.assertIsNot(replaced, self.original)
        aio_zc = mock.AsyncMock()
        with self.assertLogs(self.run._LOGGER, logging.INFO):
            asyncio.run(replaced(aio_zc, object()))
        aio_zc.async_register_service.assert_not_called()
        self.assertIn("_suppress_zeroconf_announcement", inspect.getsource(self.run._boot))

    def test_otherwise_it_is_left_alone(self):
        for value in ("", "0"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"HRI_HOST_NETWORK": value}):
                self.assertFalse(self.run._suppress_zeroconf_announcement())
                self.assertIs(getattr(self.zeroconf, self.run.ZEROCONF_ANNOUNCE), self.original)

    def test_a_home_assistant_without_it_says_so(self):
        import homeassistant.components as components

        bare = types.ModuleType("homeassistant.components.zeroconf")
        with mock.patch.dict(os.environ, {"HRI_HOST_NETWORK": "1"}), \
                mock.patch.dict(sys.modules, {"homeassistant.components.zeroconf": bare}), \
                mock.patch.object(components, "zeroconf", bare, create=True), \
                self.assertLogs(self.run._LOGGER, logging.ERROR) as logs:
            self.assertFalse(self.run._suppress_zeroconf_announcement())
        self.assertTrue(any("announces itself" in line for line in logs.output), logs.output)


class _AnyName(types.ModuleType):
    """A stand-in for async-upnp-client, which Home Assistant installs only for an integration that needs ssdp: every
    name is a class that keeps its keyword arguments."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        cls = type(name, (), {"__init__": lambda self, *args, **kwargs: self.__dict__.update(kwargs)})
        setattr(self, name, cls)
        return cls


class SsdpTest(unittest.TestCase):
    """ssdp's Server starts the UPnP servers that announce Home Assistant, from one method its async_start registers
    for Home Assistant's start; the Scanner that discovers devices is another object.  The module needs
    async-upnp-client (a stand-in where it is not installed) and is imported by the hook the first time, as Home
    Assistant's loader imports it."""

    def setUp(self):
        try:
            import run
            import homeassistant.components as components
        except ImportError as err:  # pragma: no cover - outside the container's HA venv
            self.skipTest(f"Home Assistant is not importable: {err}")
        self.run = run
        self.ssdp_dir = pathlib.Path(components.__path__[0]) / "ssdp"
        # the package's __init__ imports the scanner and the rest of async-upnp-client: only server.py runs here
        package = types.ModuleType("homeassistant.components.ssdp")
        package.__path__ = [str(self.ssdp_dir)]
        modules = {"homeassistant.components.ssdp": package}
        try:
            import async_upnp_client  # noqa: F401
        except ImportError:
            for name in ("async_upnp_client", "async_upnp_client.const", "async_upnp_client.server",
                         "async_upnp_client.ssdp"):
                modules[name] = _AnyName(name)
        patch = mock.patch.dict(sys.modules, modules)
        patch.start()
        self.addCleanup(patch.stop)
        for name in (run.SSDP_SERVER_MODULE, "homeassistant.components.ssdp.common"):
            sys.modules.pop(name, None)
        self.addCleanup(self._unhook)

    def _unhook(self):
        sys.meta_path[:] = [f for f in sys.meta_path if not isinstance(f, self.run._SsdpServerHook)]

    def _import(self):
        import importlib

        return importlib.import_module(self.run.SSDP_SERVER_MODULE)

    def test_the_servers_are_started_by_the_method_async_start_looks_up_when_it_runs(self):
        """What the replacement relies on, in this Home Assistant (CI runs it on the floor version too)."""
        server = self._import()
        announce = getattr(server.Server, self.run.SSDP_ANNOUNCE)
        self.assertIn(self.run.SSDP_ANNOUNCE, server.Server.async_start.__code__.co_names)
        self.assertEqual(server.HassUpnpServiceDevice.DEVICE_DEFINITION.device_type,
                         "urn:home-assistant.io:device:HomeAssistant:1")

        def names(code):
            return set(code.co_names).union(*(names(c) for c in code.co_consts if inspect.iscode(c)))

        self.assertTrue({"UpnpServer", "HassUpnpServiceDevice", "async_start"} <= names(announce.__code__))
        functions = [f for f in (*vars(server).values(), *vars(server.Server).values()) if inspect.isfunction(f)]
        device_users = {f.__name__ for f in functions if "HassUpnpServiceDevice" in names(f.__code__)}
        self.assertEqual(device_users, {self.run.SSDP_ANNOUNCE}, "the announced device is used nowhere else")
        init = compile((self.ssdp_dir / "__init__.py").read_text(encoding="utf-8"), "ssdp/__init__.py", "exec")
        setup = next(c for c in init.co_consts if inspect.iscode(c) and c.co_name == "async_setup")
        self.assertTrue({"Scanner", "Server", "async_start"} <= names(setup), "the scanner starts on its own")

    def _started(self, server_module):
        """Server.async_start as the component calls it, then the listener it registers for Home Assistant's start."""
        from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

        hass = SimpleNamespace(bus=mock.Mock())
        server = server_module.Server(hass)
        with mock.patch.object(server_module, "get_url", side_effect=AssertionError("announced")), \
                mock.patch.object(server_module, "async_build_source_set", side_effect=AssertionError("announced")):
            asyncio.run(server.async_start())
            listener = next(c.args[1] for c in hass.bus.async_listen_once.call_args_list
                            if c.args[0] == EVENT_HOMEASSISTANT_STARTED)
            with self.assertLogs(self.run._LOGGER, logging.INFO) as logs:
                asyncio.run(listener(object()))
        asyncio.run(server.async_stop())
        self.assertEqual(server._upnp_servers, [])
        return logs.output

    def test_on_the_host_network_nothing_is_announced(self):
        with mock.patch.dict(os.environ, {"HRI_HOST_NETWORK": "1"}):
            self.assertTrue(self.run._suppress_ssdp_announcement())
            self.assertTrue(self.run._suppress_ssdp_announcement())  # a second boot step hooks once
        self.assertEqual(sum(isinstance(f, self.run._SsdpServerHook) for f in sys.meta_path), 1)
        server = self._import()
        self.assertIsInstance(server.__loader__, self.run._SsdpServerLoader)
        self.assertIn("class Server", inspect.getsource(server), "the module's source is still readable")
        logs = self._started(server)
        self.assertTrue(any("not announced" in line for line in logs), logs)
        self.assertIn("_suppress_ssdp_announcement", inspect.getsource(self.run._boot))

    def test_a_module_already_imported_is_replaced_at_once(self):
        server = self._import()
        original = getattr(server.Server, self.run.SSDP_ANNOUNCE)
        with mock.patch.dict(os.environ, {"HRI_HOST_NETWORK": "1"}):
            self.assertTrue(self.run._suppress_ssdp_announcement())
        self.assertIsNot(getattr(server.Server, self.run.SSDP_ANNOUNCE), original)
        self.assertFalse(any(isinstance(f, self.run._SsdpServerHook) for f in sys.meta_path))

    def test_otherwise_it_is_left_alone(self):
        for value in ("", "0"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"HRI_HOST_NETWORK": value}):
                self.assertFalse(self.run._suppress_ssdp_announcement())
        self.assertFalse(any(isinstance(f, self.run._SsdpServerHook) for f in sys.meta_path))
        server = self._import()
        self.assertNotIsInstance(server.__loader__, self.run._SsdpServerLoader)
        self.assertEqual(getattr(server.Server, self.run.SSDP_ANNOUNCE).__qualname__, f"Server.{self.run.SSDP_ANNOUNCE}")

    def test_the_hook_touches_no_other_module(self):
        hook = self.run._SsdpServerHook()
        for name in ("homeassistant.components.ssdp", "homeassistant.components.ssdp.scanner",
                     "homeassistant.components.ssdp.common", "homeassistant.components.zeroconf"):
            with self.subTest(name=name):
                self.assertIsNone(hook.find_spec(name, None))

    def test_a_home_assistant_without_it_says_so(self):
        bare = types.ModuleType(self.run.SSDP_SERVER_MODULE)
        bare.Server = type("Server", (), {})
        with mock.patch.dict(os.environ, {"HRI_HOST_NETWORK": "1"}), \
                mock.patch.dict(sys.modules, {self.run.SSDP_SERVER_MODULE: bare}), \
                self.assertLogs(self.run._LOGGER, logging.ERROR) as logs:
            self.assertFalse(self.run._suppress_ssdp_announcement())
        self.assertTrue(any("announces itself" in line for line in logs.output), logs.output)


if __name__ == "__main__":
    unittest.main()
