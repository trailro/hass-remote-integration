"""External review (installer): direct-URL requirements, and a requirement install that never returns."""

import asyncio
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import types
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
    URLS = ("pkg @ https://host/x.whl", "pkg @ git+https://github.com/a/b", "pkg @ file:///config/x.tar.gz",
            'pkg[x] @ https://h/x.whl ; python_version>"3"')

    def test_url_requirements_are_refused(self):
        for req in self.URLS:
            with self.subTest(req=req):
                self.assertIn("URL", inst_mod.bad_requirement(req) or "")
        for req in ("requests>=2.0", "pkg[extra]==1.0; python_version>'3.8'", "ramses-rf==0.60.4"):
            self.assertIsNone(inst_mod.bad_requirement(req))

    def test_ha_never_counts_a_url_requirement_installed(self):
        """Why a URL requirement is not just a policy question: HA hands it to uv at every boot and start."""
        self.assertFalse(inst_mod.pkg_util.is_installed("packaging @ https://example.invalid/packaging.whl"))
        self.assertTrue(inst_mod.pkg_util.is_installed("packaging>=1"))

    def test_url_requirement_never_reaches_uv(self):
        inst = object.__new__(Installer)
        inst.constraints = ""
        with mock.patch.object(inst_mod.pkg_util, "install_package") as pip, \
                self.assertLogs("custom_components.integration_manager.installer", "ERROR"):
            self.assertEqual(inst._install_requirements(["pkg @ https://host/x.whl"]), ["pkg @ https://host/x.whl"])
        pip.assert_not_called()


# The two shapes of homeassistant/util/package.py that run uv, trimmed to what reaches Popen (Apache-2.0,
# Home Assistant).  2026.5.0-2026.7.x: Popen inline in install_package.  2026.8.0+: Popen in _install, and a
# retry without a failing extra index.  The suite also runs against the HA that is installed (the CI floor job
# is 2026.5.0), so both shapes are covered wherever it runs.
SHAPE_2026_5 = """
import logging, os, sys
from subprocess import PIPE, Popen

_LOGGER = logging.getLogger("hri_fixture.package_2026_5")


def is_installed(requirement_str):
    return False


def install_package(package, upgrade=True, target=None, constraints=None, timeout=None):
    env = os.environ.copy()
    args = [sys.executable, "-m", "uv", "pip", "install", "--quiet", package, "--index-strategy", "unsafe-first-match"]
    if timeout:
        env["HTTP_TIMEOUT"] = str(timeout)
    if constraints is not None:
        args += ["--constraint", constraints]
    with Popen(
        args,
        stdin=PIPE,
        stdout=PIPE,
        stderr=PIPE,
        env=env,
        close_fds=False,  # required for posix_spawn
    ) as process:
        _, stderr = process.communicate()
        if process.returncode != 0:
            _LOGGER.error(
                "Unable to install package %s: %s",
                package,
                stderr.decode("utf-8").lstrip().strip(),
            )
            return False

    return True
"""

SHAPE_2026_8 = """
import logging, os, sys
from subprocess import PIPE, Popen
from urllib.parse import urlparse

_LOGGER = logging.getLogger("hri_fixture.package_2026_8")


def is_installed(requirement_str):
    return False


def _install(args, env):
    with Popen(
        args,
        stdin=PIPE,
        stdout=PIPE,
        stderr=PIPE,
        env=env,
        close_fds=False,  # required for posix_spawn
    ) as process:
        _, stderr = process.communicate()
        if process.returncode != 0:
            return stderr.decode("utf-8").lstrip().strip()
    return None


def install_package(package, upgrade=True, target=None, constraints=None, timeout=None):
    env = os.environ.copy()
    args = [sys.executable, "-m", "uv", "pip", "install", "--quiet", package, "--index-strategy", "unsafe-first-match"]
    if timeout:
        env["HTTP_TIMEOUT"] = str(timeout)
    if constraints is not None:
        args += ["--constraint", constraints]
    if (stderr := _install(args, env)) is None:
        return True
    extra_urls = env.get("UV_EXTRA_INDEX_URL", "").split()
    failing = {url: host for url in extra_urls if (host := urlparse(url).hostname) and host in stderr}
    if failing:
        _LOGGER.warning("Unable to install package %s using extra index host %s: %s; retrying without it",
                        package, ", ".join(failing.values()), stderr)
        retry_env = env.copy()
        if remaining := [url for url in extra_urls if url not in failing]:
            retry_env["UV_EXTRA_INDEX_URL"] = " ".join(remaining)
        else:
            del retry_env["UV_EXTRA_INDEX_URL"]
        if (stderr := _install(args, retry_env)) is None:
            return True
    _LOGGER.error("Unable to install package %s: %s", package, stderr)
    return False
"""


def _shape(source, name):
    module = types.ModuleType(name)
    exec(compile(source, name, "exec"), module.__dict__)  # noqa: S102 - the fixture above
    return module


def _hanging_python(test, pidfile):
    """A stand-in for sys.executable: "python -m uv pip install ..." that starts a child and never returns.
    With an extra index set, it fails at once naming that index's host (what the 2026.8+ retry looks for)."""
    d = _tmp(test)
    path = os.path.join(d, "python")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('#!/bin/sh\nif [ -n "$UV_EXTRA_INDEX_URL" ]; then echo "error: wheels.example.invalid: 503" >&2; exit 2; fi\n'
                 f"sleep 30 &\necho $! > {pidfile}\nsleep 30\n")
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
        """install_package as HA has it, with uv replaced by a process that hangs."""
        return mock.patch.object(sys, "executable", self.python)

    def _child_is_gone(self):
        with open(self.pidfile, encoding="utf-8") as fh:
            child = int(fh.read())
        for _ in range(50):
            if not _alive(child):
                return True
            time.sleep(0.1)
        return False

    def _stops(self, logger, env=None):
        inst = object.__new__(Installer)
        inst.constraints = None
        with self._hung(), mock.patch.object(inst_mod, "PIP_INSTALL_TIMEOUT_S", 1), \
                mock.patch.object(inst_mod.pkg_util, "is_installed", return_value=False), \
                mock.patch.dict(os.environ, env or {}), self.assertLogs(logger, "WARNING") as logs:
            if not env:
                os.environ.pop("UV_EXTRA_INDEX_URL", None)  # the image may set one: this run has no failing index
            t0 = time.monotonic()
            failed = inst._install_requirements(["slowpkg==1.0"])
            took = time.monotonic() - t0
        self.assertEqual(failed, ["slowpkg==1.0"])
        self.assertLess(took, 10)
        errors = [r.getMessage() for r in logs.records if r.levelname == "ERROR"]
        self.assertTrue(any("slowpkg==1.0" in m and "did not finish within 1s" in m for m in errors), logs.output)
        self.assertTrue(self._child_is_gone(), "what uv started is killed with it (process group)")
        return logs

    def test_installed_ha_stops_a_hung_uv(self):
        self._stops("homeassistant.util.package")

    def test_2026_5_shape_stops_a_hung_uv(self):
        with mock.patch.object(inst_mod, "pkg_util", _shape(SHAPE_2026_5, "hri_fixture_package_2026_5")):
            self._stops("hri_fixture.package_2026_5")

    def test_2026_8_shape_stops_a_hung_retry(self):
        """The deadline covers the requirement, the retry without a failing extra index included."""
        with mock.patch.object(inst_mod, "pkg_util", _shape(SHAPE_2026_8, "hri_fixture_package_2026_8")):
            logs = self._stops("hri_fixture.package_2026_8", env={"UV_EXTRA_INDEX_URL": "https://wheels.example.invalid/simple"})
        self.assertIn("retrying without it", "\n".join(logs.output))

    def test_a_stopped_uv_reads_as_a_failed_one(self):
        inst_mod._pip_deadline.at = time.monotonic() + 0.5
        try:
            for text in (False, True):
                with self.subTest(text=text), inst_mod._BoundedPopen([self.python], stdout=subprocess.PIPE,
                                                                     stderr=subprocess.PIPE, text=text) as proc:
                    _, err = proc.communicate()
                self.assertEqual(proc.returncode, -signal.SIGKILL)
                self.assertIsInstance(err, str if text else bytes)
                inst_mod._pip_deadline.at = time.monotonic() + 0.5
        finally:
            inst_mod._pip_deadline.at = None

    def test_without_a_deadline_it_is_popen(self):
        """HA's own installs (no deadline on their thread) keep HA's process: same session, no time limit."""
        self.assertTrue(inst_mod._bind_popen())
        with inst_mod.pkg_util.Popen(["sleep", "5"], stdout=subprocess.PIPE) as proc:
            try:
                self.assertEqual(os.getsid(proc.pid), os.getsid(0))
                with self.assertRaises(subprocess.TimeoutExpired):
                    proc.communicate(timeout=0.2)  # Popen's contract: the caller's timeout raises, nothing is killed
                self.assertIsNone(proc.poll())
            finally:
                proc.kill()

    def test_bind_replaces_an_older_copy_and_leaves_a_foreign_popen(self):
        module = _shape(SHAPE_2026_5, "hri_fixture_bind")
        module.Popen = type("_BoundedPopen", (subprocess.Popen,), {"_hri_bounded": True})  # this module, reloaded
        self.assertTrue(inst_mod._bind_popen(module))
        self.assertIs(module.Popen, inst_mod._BoundedPopen)
        foreign = type("Other", (subprocess.Popen,), {})
        module.Popen = foreign
        self.assertFalse(inst_mod._bind_popen(module))
        self.assertIs(module.Popen, foreign)

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
