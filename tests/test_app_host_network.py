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


if __name__ == "__main__":
    unittest.main()
