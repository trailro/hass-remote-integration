"""External review (installer): direct-URL requirements, and a requirement install that never returns."""

import asyncio
import json
import os
import shutil
import stat
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import backupkit
from custom_components.integration_manager import installer as inst_mod
from custom_components.integration_manager.installer import Installer


def _tmp(test):
    d = tempfile.mkdtemp(prefix="hri-fable-inst-")
    test.addCleanup(shutil.rmtree, d, ignore_errors=True)
    return d


class UrlRequirementTest(unittest.TestCase):
    def test_ha_never_counts_a_url_requirement_installed(self):
        """Why a URL requirement is not just a policy question: HA hands it to uv at every boot and start."""
        self.assertFalse(inst_mod.pkg_util.is_installed("packaging @ https://example.invalid/packaging.whl"))
        self.assertTrue(inst_mod.pkg_util.is_installed("packaging>=1"))


def _hanging_python(test, pidfile):
    """A stand-in for sys.executable: "python -m uv pip install ..." that starts a child and never returns."""
    d = _tmp(test)
    path = os.path.join(d, "python")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"#!/bin/sh\nsleep 30 &\necho $! > {pidfile}\nsleep 30\n")
    os.chmod(path, stat.S_IRWXU)
    return path


def _alive(pid):
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            return fh.read().split(")")[-1].split()[0] != "Z"
    except FileNotFoundError:
        return False


class InstallTimeoutTest(unittest.TestCase):
    def setUp(self):
        self.pidfile = os.path.join(_tmp(self), "child.pid")
        self.python = _hanging_python(self, self.pidfile)

    def _hung(self):
        """HA's real install_package, with uv replaced by a process that hangs."""
        return mock.patch.multiple(inst_mod.pkg_util.sys, executable=self.python)

    def test_install_requirements_stops_a_hung_uv(self):
        inst = object.__new__(Installer)
        inst.constraints = None
        with self._hung(), mock.patch.object(inst_mod, "PIP_INSTALL_TIMEOUT_S", 1), \
                mock.patch.object(inst_mod.pkg_util, "is_installed", return_value=False), \
                self.assertLogs("homeassistant.util.package", "ERROR") as logs:
            t0 = time.monotonic()
            failed = inst._install_requirements(["slowpkg==1.0"])
            took = time.monotonic() - t0
        self.assertEqual(failed, ["slowpkg==1.0"])
        self.assertLess(took, 10)
        self.assertIn("did not finish within 1s", "\n".join(logs.output))
        with open(self.pidfile, encoding="utf-8") as fh:
            child = int(fh.read())
        for _ in range(50):
            if not _alive(child):
                break
            time.sleep(0.1)
        self.assertFalse(_alive(child), "what uv started is killed with it (process group)")

    def test_other_callers_keep_ha_install(self):
        """Only _install_requirements carries a deadline: HA's own installs still run HA's _install."""
        inst_mod._bind_install()
        self.assertIs(inst_mod.pkg_util._install, inst_mod._bounded_install)
        seen = []
        with mock.patch.object(inst_mod._bounded_install, "ha_install", lambda args, env: seen.append(args)):
            self.assertIsNone(inst_mod.pkg_util._install(["x"], {}))
        self.assertEqual(seen, [["x"]])

    def test_rebind_wraps_ha_install_not_the_wrapper(self):
        ha = inst_mod._bounded_install.ha_install if inst_mod._bind_install() else None
        old = mock.Mock(ha_install=ha)
        with mock.patch.object(inst_mod.pkg_util, "_install", old):
            self.assertTrue(inst_mod._bind_install())
            self.assertIs(inst_mod._bounded_install.ha_install, ha)
            self.assertIs(inst_mod.pkg_util._install, inst_mod._bounded_install)

    def test_start_errors_and_releases_busy(self):
        cfg = _tmp(self)
        os.makedirs(os.path.join(cfg, "integration_manager"))
        with open(os.path.join(cfg, "integration_manager", "state.json"), "w", encoding="utf-8") as fh:
            json.dump({"domain": "hub", "installed": {"hub": {"versions": {"v1": {}, "v2": {}}, "running_tag": "v1"}}}, fh)

        async def executor(fn, *args):
            return fn(*args)

        hass = SimpleNamespace(config=SimpleNamespace(config_dir=cfg, components=set()), async_add_executor_job=executor)
        inst = Installer(hass)
        for tag in ("v1", "v2"):
            os.makedirs(inst._version_dir("hub", tag))

        async def nothing(*args, **kwargs):
            return []

        with self._hung(), mock.patch.object(inst_mod, "PIP_INSTALL_TIMEOUT_S", 1), \
                mock.patch.object(inst_mod.pkg_util, "is_installed", return_value=False), \
                mock.patch.object(Installer, "async_backup", mock.AsyncMock(return_value={"name": "pre.zip"})), \
                mock.patch.object(backupkit, "prune", lambda *a, **k: None), \
                mock.patch.object(Installer, "_ensure_deployed", lambda self, d, t: False), \
                mock.patch.object(Installer, "_requirements_for", mock.AsyncMock(return_value=["slowpkg==1.0"])), \
                mock.patch.object(Installer, "_disable_entries", nothing), \
                mock.patch.object(Installer, "_enable_entries", nothing), \
                self.assertLogs("homeassistant.util.package", "ERROR"):
            res = asyncio.run(inst.start("hub", "v2"))
        self.assertFalse(res["ok"])
        self.assertIn("pip failed for: slowpkg==1.0", res["error"])
        self.assertFalse(inst.busy)
        self.assertEqual(inst.state.installed["hub"]["running_tag"], "v1")


def _installer(test):
    cfg = _tmp(test)
    os.makedirs(os.path.join(cfg, "integration_manager"))

    async def executor(fn, *args):
        return fn(*args)

    return Installer(SimpleNamespace(config=SimpleNamespace(config_dir=cfg, components=set()), async_add_executor_job=executor))


class RegistryRepoTest(unittest.TestCase):
    def test_add_to_registry_checks_the_repo_itself(self):
        """The repo becomes a GitHub API path: the method holds the views' rule, not just "one slash"."""
        inst = _installer(self)
        for repo in ("owner/na me", "owner/name?per_page=1", "owner/name#x", "../name", "owner/.."):
            with self.subTest(repo=repo), self.assertRaises(ValueError):
                inst.add_to_registry("demo", repo)
        self.assertNotIn("demo", inst.registry())
        self.assertEqual(inst.add_to_registry("demo", "owner/name.py")["repo"], "owner/name.py")
        self.assertTrue(inst.add_to_registry("dev", "", local=True)["local"])


class TagLengthTest(unittest.TestCase):
    def test_a_tag_that_cannot_be_a_directory_is_refused_before_the_download(self):
        worst = "a" + "/" * 100  # 101 characters, 301 once "/" is spelled %2F
        self.assertTrue(inst_mod._TAG_RE.match(worst))
        self.assertFalse(inst_mod.tag_ok(worst))
        inst = _installer(self)
        inst.spec = lambda dom: {"repo": "owner/repo"}
        with mock.patch.object(inst_mod, "async_get_clientsession") as session:
            res = asyncio.run(inst.install(worst, domain="demo"))
        self.assertFalse(res["ok"])
        self.assertIn("invalid tag", res["error"])
        session.assert_not_called()

    def test_every_accepted_tag_fits_its_staging_directory(self):
        for tag in ("a" * 101, "a" + "/b" * 50, "a" + "/" * 72, "release/2026.9.0", "v1.2.3+build@x"):
            with self.subTest(tag=tag):
                if inst_mod.tag_ok(tag):
                    name = ".staging-" + os.path.basename(_installer(self)._version_dir("demo", tag))
                    self.assertLessEqual(len(name.encode()), 255)


if __name__ == "__main__":
    unittest.main()
