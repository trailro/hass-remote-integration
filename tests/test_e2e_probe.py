"""What an end-to-end run against a probe integration turned up: the published
service catalog advertised no response at all, requirements_ok was false for an
integration that needs nothing, and the image carried no libturbojpeg for HA's
camera component."""

import asyncio
import json
import os
import pathlib
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from homeassistant.core import SupportsResponse

from custom_components.integration_manager import services_catalog as sc
from custom_components.integration_manager.installer import Installer, State

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Services:
    """The two ServiceRegistry members the catalog uses."""

    def __init__(self, supported):
        self._supported = supported

    def async_services(self):
        out = {}
        for domain, service in self._supported:
            out.setdefault(domain, {})[service] = None
        return out

    def supports_response(self, domain, service):
        return self._supported[(domain, service)]


def _rows(supported, services_yaml=None):
    tmp = tempfile.mkdtemp(prefix="hri-unit-")
    try:
        if services_yaml is not None:
            with open(os.path.join(tmp, "services.yaml"), "w", encoding="utf-8") as fh:
                fh.write(services_yaml)

        async def executor(fn, *args):
            return fn(*args)

        hass = SimpleNamespace(services=_Services(supported), async_add_executor_job=executor)
        integration = SimpleNamespace(is_built_in=False, file_path=pathlib.Path(tmp))
        with mock.patch.object(sc.loader, "async_get_integration", mock.AsyncMock(return_value=integration)):
            rows = asyncio.run(sc.service_rows(hass))
        return {(r["domain"], s["name"]): s["response"] for r in rows for s in r["services"]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class ServiceCatalogResponseTest(unittest.TestCase):
    """H1: only knx declares response: in services.yaml, so reading the yaml
    alone published every response-capable service as null."""

    def test_registration_decides_when_the_yaml_says_nothing(self):
        responses = _rows({("probe", "echo"): SupportsResponse.ONLY,
                           ("probe", "search"): SupportsResponse.OPTIONAL,
                           ("probe", "press"): SupportsResponse.NONE})
        self.assertEqual(responses[("probe", "echo")], "required")
        self.assertEqual(responses[("probe", "search")], "optional")
        self.assertIsNone(responses[("probe", "press")])

    def test_an_explicit_yaml_declaration_wins(self):
        yaml = "echo:\n  response:\n    optional: true\nsearch:\n  response: {}\n"
        responses = _rows({("probe", "echo"): SupportsResponse.ONLY,
                           ("probe", "search"): SupportsResponse.OPTIONAL}, yaml)
        self.assertEqual(responses[("probe", "echo")], "optional")
        self.assertEqual(responses[("probe", "search")], "required")


class RequirementsOkTest(unittest.TestCase):
    """H2: "requirements": [] in the manifest is a healthy integration, not a
    missing dependency."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hri-unit-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        os.makedirs(os.path.join(self.dir, "integration_manager"))
        os.makedirs(os.path.join(self.dir, "custom_components", "probe"))

    def info(self, requirements):
        with open(os.path.join(self.dir, "custom_components", "probe", "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"domain": "probe", "name": "Probe", "version": "1.0.0", "requirements": requirements}, fh)
        hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.dir, components=set()),
                               config_entries=SimpleNamespace(async_entries=lambda domain: []))
        inst = Installer(hass)
        inst.state = State(domain="probe", installed={"probe": {"running_tag": "1.0.0", "versions": {"1.0.0": {}}}})
        return inst._domain_info("probe")

    def test_no_requirements_is_satisfied(self):
        info = self.info([])
        self.assertEqual(info["requirements"], {})
        self.assertIs(info["requirements_ok"], True)

    def test_a_requirement_that_is_not_installed_is_still_a_miss(self):
        info = self.info(["hri-nothing-provides-this==1.0"])
        self.assertIsNone(info["requirements"]["hri-nothing-provides-this==1.0"])
        self.assertIs(info["requirements_ok"], False)


class TurboJpegTest(unittest.TestCase):
    """H3: PyTurboJPEG is a ctypes binding; without the C library HA's camera
    component logs an ERROR with a traceback at every boot."""

    def test_the_image_installs_the_turbojpeg_library(self):
        path = os.path.join(ROOT, "Dockerfile")
        if not os.path.isfile(path):
            self.skipTest("the Dockerfile is not copied next to the tests")
        with open(path, encoding="utf-8") as fh:
            dockerfile = fh.read()
        self.assertIn("libturbojpeg0", dockerfile)
        self.assertEqual(dockerfile.count("apt-get install"), 1)  # one apt layer
        self.assertIn("--no-install-recommends", dockerfile)
        self.assertIn("rm -rf /var/lib/apt/lists/*", dockerfile)


if __name__ == "__main__":
    unittest.main()
