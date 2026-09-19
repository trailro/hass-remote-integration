"""The preflight of a Home Assistant version: what pip is asked, what blocks, what only reads as "could not check",
the cache, and the two refusals (requires_python and the pins) reading the same way.

No network and no pip: ``_run_pip`` is the one seam, so every case below is a scripted pip."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import preflight

V = "2026.1.0"
PINS = ["aiohttp==3.14.3", "lru-dict==1.3.0", "PyYAML==6.0.2", "propcache>=0.3; python_version < '3.9'"]


def _report(pins):
    return json.dumps({"install": [{"metadata": {"name": "homeassistant", "version": V, "requires_dist": pins}}]})


def _proc(rc=0, out="", err=""):
    return subprocess.CompletedProcess(["pip"], rc, out, err)


NO_WHEEL = ("ERROR: Ignored the following versions that require a different python version: 1.13.0 Requires-Python <3.14\n"
            "ERROR: Could not find a version that satisfies the requirement lru-dict==1.3.0 (from homeassistant) (from versions: 1.4.1)\n"
            "ERROR: No matching distribution found for lru-dict==1.3.0\n")
TOO_DEEP = ("error: resolution-too-deep\n\n"
            "× Dependency resolution exceeded maximum depth\n"
            "╰─> Pip cannot resolve the current dependencies as the dependency graph is too complex.\n")


class _Hass:
    """Enough hass for async_add_executor_job."""

    def __init__(self):
        self.config = SimpleNamespace(config_dir="/config", path=lambda *p: os.path.join("/config", *p))

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def _check(script):
    """Run ha_version_report with ``script`` answering each _run_pip call in turn; returns (report, calls)."""
    preflight._HA_REPORTS.clear()
    calls = []

    def fake(cmd):
        calls.append(cmd)
        answer = script[min(len(calls) - 1, len(script) - 1)]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    with mock.patch.object(preflight, "_run_pip", fake):
        return asyncio.run(preflight.ha_version_report(_Hass(), V)), calls


class PipInvocationTest(unittest.TestCase):
    def test_the_resolve_is_wheels_only_no_deps_and_installs_nothing(self):
        _, calls = _check([_proc(out=_report(PINS)), _proc()])
        for cmd in calls:
            for flag in ("--dry-run", "--no-deps", "--only-binary=:all:", "--ignore-installed"):
                self.assertIn(flag, cmd)
        self.assertIn(f"homeassistant=={V}", calls[0])

    def test_the_second_run_asks_for_the_pins_without_the_conditional_ones(self):
        _, calls = _check([_proc(out=_report(PINS)), _proc()])
        asked = [a for a in calls[1] if "==" in a or ">=" in a]
        self.assertEqual(asked, ["aiohttp==3.14.3", "lru-dict==1.3.0", "PyYAML==6.0.2"])


class VerdictTest(unittest.TestCase):
    def test_a_version_that_resolves_is_accepted(self):
        rep, _ = _check([_proc(out=_report(PINS)), _proc()])
        self.assertTrue(rep["ok"])
        self.assertTrue(rep["checked"])
        self.assertEqual(rep["blockers"], [])
        self.assertIn("all 3 pinned requirements", rep["notes"][0])

    def test_a_pin_without_a_wheel_blocks_and_is_named(self):
        rep, calls = _check([_proc(out=_report(PINS)), _proc(rc=1, err=NO_WHEEL), _proc()])
        self.assertFalse(rep["ok"])
        self.assertTrue(rep["checked"])
        self.assertEqual(rep["missing"], ["lru-dict==1.3.0"])
        self.assertIn("lru-dict==1.3.0", rep["blockers"][0])
        self.assertIn("no compiler", rep["blockers"][0])
        self.assertIn("HRI_APT_PACKAGES=build-essential", rep["blockers"][0])
        self.assertNotIn("lru-dict==1.3.0", calls[2])  # the named pin is dropped, the rest asked again

    def test_a_resolver_artifact_is_could_not_check_and_does_not_block(self):
        rep, _ = _check([_proc(out=_report(PINS)), _proc(rc=1, err=TOO_DEEP)])
        self.assertTrue(rep["ok"])
        self.assertFalse(rep["checked"])
        self.assertEqual(rep["blockers"], [])
        self.assertIn("could not check", rep["notes"][0])
        self.assertIn("resolution-too-deep", rep["notes"][0])

    def test_a_timeout_is_could_not_check_too(self):
        rep, _ = _check([subprocess.TimeoutExpired(["pip"], 300)])
        self.assertTrue(rep["ok"])
        self.assertFalse(rep["checked"])
        self.assertIn("could not finish", rep["notes"][0].replace("did not finish", "could not finish"))

    def test_a_missing_wheel_found_before_an_artifact_still_blocks(self):
        rep, _ = _check([_proc(out=_report(PINS)), _proc(rc=1, err=NO_WHEEL), _proc(rc=1, err=TOO_DEEP)])
        self.assertFalse(rep["ok"])
        self.assertFalse(rep["checked"])  # the list may be incomplete
        self.assertEqual(rep["missing"], ["lru-dict==1.3.0"])

    def test_a_release_pip_cannot_take_at_all_blocks(self):
        err = "ERROR: Could not find a version that satisfies the requirement homeassistant==2026.1.0 (from versions: none)\n"
        rep, _ = _check([_proc(rc=1, err=err)])
        self.assertFalse(rep["ok"])
        self.assertIn("cannot be installed here", rep["blockers"][0])

    def test_many_missing_pins_are_bounded_and_said_so(self):
        errs = [_proc(rc=1, err=NO_WHEEL.replace("lru-dict==1.3.0", f"pkg{i}==1.0")) for i in range(preflight.MAX_HA_MISSING)]
        rep, calls = _check([_proc(out=_report([f"pkg{i}==1.0" for i in range(30)])), *errs])
        self.assertEqual(len(rep["missing"]), preflight.MAX_HA_MISSING)
        self.assertEqual(len(calls), preflight.MAX_HA_MISSING + 1)
        self.assertIn(f"at least {preflight.MAX_HA_MISSING}", rep["warnings"][0])  # eight and eighty look alike from here

    def test_a_conditional_pin_is_noted_not_checked(self):
        rep, _ = _check([_proc(out=_report(PINS)), _proc()])
        self.assertTrue(any("conditional requirement" in n for n in rep["notes"]))

    def test_a_version_that_is_not_one_is_refused_before_pip(self):
        with self.assertRaises(ValueError):
            asyncio.run(preflight.ha_version_report(_Hass(), "latest; rm -rf /"))


class CacheTest(unittest.TestCase):
    def test_the_second_call_does_not_run_pip_again(self):
        rep, calls = _check([_proc(out=_report(PINS)), _proc()])
        with mock.patch.object(preflight, "_run_pip", mock.Mock(side_effect=AssertionError("pip ran again"))):
            again = asyncio.run(preflight.ha_version_report(_Hass(), V))
        self.assertIs(again, rep)

    def test_the_key_carries_the_image_python(self):
        _check([_proc(out=_report(PINS)), _proc()])
        self.assertIn(sys.version.split()[0], preflight._ha_python_key())
        self.assertEqual(list(preflight._HA_REPORTS)[0], (V, preflight._ha_python_key()))

    def test_another_python_does_not_hit_the_stored_answer(self):
        _check([_proc(out=_report(PINS)), _proc()])
        with mock.patch.object(preflight, "_ha_python_key", return_value="9.9.9-riscv"):
            self.assertIsNone(preflight.ha_recent(V))

    def test_the_dict_stays_bounded(self):
        preflight._HA_REPORTS.clear()
        for i in range(preflight.MAX_HA_REPORTS + 5):
            preflight.ha_remember(f"2026.1.{i}", {"ok": True})
        self.assertEqual(len(preflight._HA_REPORTS), preflight.MAX_HA_REPORTS)


class UpdaterTest(unittest.IsolatedAsyncioTestCase):
    """HaUpdater.dependency_check: the venv shortcut, and the two refusals reading alike."""

    async def asyncSetUp(self):
        from custom_components.integration_manager import ha_updater

        self.ha_updater = ha_updater
        self.cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        os.makedirs(os.path.join(self.cfg, "integration_manager"))
        loop = asyncio.get_running_loop()
        self.hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg, path=lambda *p: os.path.join(self.cfg, *p)),
                                    async_add_executor_job=lambda f, *a: loop.run_in_executor(None, f, *a))
        self.up = ha_updater.HaUpdater(self.hass)

    async def test_an_installed_venv_for_this_python_is_not_resolved_again(self):
        from tests.test_r4_lifecycle import make_venv

        make_venv(self.cfg, V)
        with mock.patch.object(preflight, "ha_version_report", mock.AsyncMock(side_effect=AssertionError("pip ran"))):
            rep = await self.up.dependency_check(V)
        self.assertTrue(rep["ok"])
        self.assertFalse(rep["checked"])
        self.assertIn("already installed", rep["notes"][0])

    async def test_without_a_venv_the_preflight_decides(self):
        blocked = {"ok": False, "blockers": ["Home Assistant 2026.1.0 needs lru-dict==1.3.0, ..."]}
        with mock.patch.object(preflight, "ha_version_report", mock.AsyncMock(return_value=blocked)) as run:
            rep = await self.up.dependency_check(f" {V} ")
        self.assertIs(rep, blocked)
        self.assertEqual(run.await_args.args[1], V)

    async def test_both_refusals_name_the_version_what_it_needs_and_this_image(self):
        preflight._HA_REPORTS.clear()
        self.up._releases = {V: ">=3.15"}
        with mock.patch.object(self.up, "available", mock.AsyncMock(return_value={})), \
             mock.patch.dict(os.environ, {}, clear=False):
            for var in ("HA_VERSION_DEFAULT", "HA_VERSION_MIN"):  # no floor at all: the refusal under test is the Python one
                os.environ.pop(var, None)
            with self.assertRaises(ValueError) as ctx:
                await self.up.validate(V)
        python_refusal = str(ctx.exception)
        script = [_proc(out=_report(PINS)), _proc(rc=1, err=NO_WHEEL), _proc()]
        with mock.patch.object(preflight, "_run_pip", lambda cmd: script.pop(0)):
            deps_refusal = await preflight.ha_version_report(_Hass(), V)
        for text in (python_refusal, deps_refusal["blockers"][0]):
            self.assertTrue(text.startswith(f"Home Assistant {V} needs "), text)
            self.assertIn("this image has", text)
            self.assertIn("Home Assistant version", text)  # both end with the same way out


def _pypi_reachable() -> bool:
    import socket

    try:
        socket.create_connection(("pypi.org", 443), timeout=3).close()
        return True
    except OSError:
        return False


@unittest.skipUnless(_pypi_reachable(), "PyPI is not reachable (offline)")
class AgainstPypiTest(unittest.TestCase):
    """The only test here that really calls pip.  Slow (it talks to PyPI), skipped offline; it is what keeps the
    rest honest: the scripted answers above are what these two runs produce on this image's Python."""

    def setUp(self):
        preflight._HA_REPORTS.clear()

    def test_a_version_older_than_this_python_is_blocked_on_its_pins(self):
        rep = asyncio.run(preflight.ha_version_report(_Hass(), "2026.1.0"))
        self.assertTrue(rep["checked"], rep)
        self.assertFalse(rep["ok"], rep)
        self.assertIn("lru-dict==1.3.0", rep["missing"])

    def test_the_current_version_resolves(self):
        # the same version the container runs: the one resolve that must not be an artifact.  A full
        # dependency resolve answers "resolution-too-deep" here; --no-deps over the pins does not.
        import homeassistant.const

        rep = asyncio.run(preflight.ha_version_report(_Hass(), homeassistant.const.__version__))
        self.assertTrue(rep["checked"], rep)
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(rep["missing"], [])


if __name__ == "__main__":
    unittest.main()
