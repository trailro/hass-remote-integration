"""Review round 12 (m9): Home Assistant versions are checked against PyPI or refused, yanked releases are skipped."""

import asyncio
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import aiohttp

from tests.fakes import entrypoint_for
from tests.test_r4_lifecycle import make_venv

A, B = "2026.8.3", "2026.9.2"


class _Response:
    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    async def read(self):
        return json.dumps(self.payload).encode()


class _Session:
    def __init__(self, payload=None, error=None):
        self.payload, self.error = payload, error

    def get(self, url, timeout=None):
        if self.error:
            raise self.error
        return _Response(self.payload)


def _release(requires=">=3.0", yanked=False):
    return {"requires_python": requires, "yanked": yanked, "upload_time": "2026-09-01T00:00:00"}


class HaVersionCheckTest(unittest.IsolatedAsyncioTestCase):
    """m9: with PyPI unreachable any version was accepted (no Python check), and a release yanked whole was offered."""

    async def asyncSetUp(self):
        from custom_components.integration_manager import ha_updater

        self.ha_updater = ha_updater
        self.cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        os.makedirs(os.path.join(self.cfg, "integration_manager"))
        loop = asyncio.get_running_loop()
        self.hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg, path=lambda *p: os.path.join(self.cfg, *p)),
                                    async_add_executor_job=lambda f, *a: loop.run_in_executor(None, f, *a))
        env = mock.patch.dict(os.environ, {"HA_VERSION_DEFAULT": A})
        env.start()
        self.addCleanup(env.stop)

    def updater(self, session):
        patch = mock.patch.object(self.ha_updater, "async_get_clientsession", return_value=session)
        patch.start()
        self.addCleanup(patch.stop)
        return self.ha_updater.HaUpdater(self.hass)

    async def test_an_unknown_release_list_refuses_the_version(self):
        up = self.updater(_Session(error=aiohttp.ClientConnectionError("offline")))
        with self.assertRaises(ValueError) as ctx:
            await up.validate(B)
        self.assertIn("cannot check", str(ctx.exception))
        self.assertIn("try again", str(ctx.exception))

    async def test_offline_a_venv_installed_for_this_python_is_still_accepted(self):
        make_venv(self.cfg, B)
        up = self.updater(_Session(error=aiohttp.ClientConnectionError("offline")))
        await up.validate(B)
        other = os.path.join(self.cfg, "venv-2026.9.3")
        make_venv(self.cfg, "2026.9.3")
        shutil.rmtree(os.path.join(other, "lib", f"python{sys.version_info[0]}.{sys.version_info[1]}"))  # another Python's venv
        with self.assertRaises(ValueError):
            await up.validate("2026.9.3")

    async def test_a_release_yanked_whole_is_not_offered_or_accepted(self):
        payload = {"info": {"requires_python": ">=3.0"},
                   "releases": {B: [_release()], "2026.9.3": [_release(yanked=True), _release(yanked=True)],
                                "2026.9.4": [_release(yanked=True), _release()]}}
        up = self.updater(_Session(payload))
        info = await up.available(force=True)
        self.assertEqual(info["latest_stable"], "2026.9.4")  # one file left is a release
        self.assertNotIn("2026.9.3", info["recent"])
        with self.assertRaises(ValueError):
            await up.validate("2026.9.3")
        await up.validate(B)

    async def test_the_python_check_still_refuses_with_a_list(self):
        up = self.updater(_Session({"info": {}, "releases": {B: [_release(">=9.0")]}}))
        with self.assertRaises(ValueError) as ctx:
            await up.validate(B)
        self.assertIn("needs Python", str(ctx.exception))


class LatestStableYankedTest(unittest.TestCase):
    """m9: a fresh volume installed the newest release even when every file of it was yanked."""

    def test_a_fully_yanked_release_is_skipped(self):
        ep = entrypoint_for(self, tempfile.mkdtemp(), HA_VERSION_DEFAULT=A)
        data = {"releases": {B: [{"requires_python": ">=3.0", "yanked": False}],
                             "2026.9.3": [{"requires_python": ">=3.0", "yanked": True}, {"requires_python": ">=3.0", "yanked": True}]}}
        with mock.patch.object(ep.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(data).encode())):
            self.assertEqual(ep.latest_stable(), B)


if __name__ == "__main__":
    unittest.main()
