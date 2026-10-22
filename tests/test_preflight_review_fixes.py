"""Review of the target-pins preflight and the switch's requirement check.

* A conflict is put on a Home Assistant pin only when pip's conflict is about a requirement that pin does not
  satisfy - never because a pinned name appears somewhere in pip's output.  pip's output below is what pip 25
  prints with --quiet (as the preflight runs it), captured in this image.
* The switch's requirement check: a pip that timed out or gave no report, or a lock another check holds, is
  "could not check" (a warning, not kept).
* The production scenario through the API: ramses_cc 0.60.4 running, Home Assistant 2026.10.2 the target (here
  TARGET, a version these tests never run on), its pins read from a venv-<target> with its .ok marker."""

import asyncio
import json
import os
import shutil
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock
import unittest

from custom_components.integration_manager import build_views, installer as inst_mod, preflight
from tests.test_camp_preflight import _hass, _installer
from tests.test_ha_preflight_views import CLEAN, _GateBase
from tests.test_preflight_ha_attributes import TARGET_TREE
from tests.test_preflight_target_pins import MANIFEST, _Base, _ha_files, _resolve

TARGET = "2099.10.2"
CONFLICT_CONSTRAINT = ("ERROR: Cannot install serialx>=1.11.0 because these package versions have conflicting dependencies.\n"
                       "ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/en/latest/topics/dependency-resolution/"
                       "#dealing-with-dependency-conflicts")
CONFLICT_DEPENDENCY = ("ERROR: Cannot install serialx==1.10.0 and serialx>=1.11.0 because these package versions have conflicting "
                       "dependencies.\nERROR: ResolutionImpossible: for help visit https://pip.pypa.io/en/latest/topics/"
                       "dependency-resolution/#dealing-with-dependency-conflicts")
NOT_FOUND = ("ERROR: Could not find a version that satisfies the requirement serialx>=9 (from versions: 0.1.0, 1.9.0, 1.10.0, "
             "1.11.0, 1.12.0)\nERROR: No matching distribution found for serialx>=9")
UNRELATED = ("ERROR: Cannot install bar>=2, foo==1.0 and serialx>=1.10.0 because these package versions have conflicting "
             "dependencies.\nERROR: ResolutionImpossible: for help visit https://pip.pypa.io/en/latest/topics/dependency-resolution/"
             "#dealing-with-dependency-conflicts")
VERBOSE = ("ERROR: Cannot install ramses-rf==0.61.1 because these package versions have conflicting dependencies.\n\n"
           "The conflict is caused by:\n    ramses-rf 0.61.1 depends on serialx>=1.11.0\n"
           "    The user requested (constraint) serialx==1.10.0\n\nTo fix this you could try to:\n")


def _failed(err):
    return lambda *a: {"ok": False, "install": [], "stderr": err, "stderr_full": err}


class PinConflictTest(unittest.TestCase):
    def test_a_constraint_the_requirement_excludes(self):
        self.assertEqual(preflight._ha_pin_conflicts(CONFLICT_CONSTRAINT, "serialx==1.10.0\n", []), ["serialx"])

    def test_a_dependency_requirement_against_the_integration_one(self):
        self.assertEqual(preflight._ha_pin_conflicts(CONFLICT_DEPENDENCY, "", ["serialx==1.10.0"]), ["serialx"])

    def test_a_missing_version_is_no_pin_conflict(self):
        self.assertEqual(preflight._ha_pin_conflicts(NOT_FOUND, "serialx==1.10.0\n", ["serialx==1.10.0"]), [])

    def test_a_conflict_elsewhere_that_names_a_pinned_package_is_not_put_on_the_pin(self):
        self.assertEqual(preflight._ha_pin_conflicts(UNRELATED, "serialx==1.10.0\n", ["serialx==1.10.0"]), [])

    def test_the_longer_output_names_the_constraint(self):
        self.assertEqual(preflight._ha_pin_conflicts(VERBOSE, "", []), [])  # the requirement comes from another package
        self.assertEqual(preflight._ha_pin_conflicts(VERBOSE.replace("ramses-rf 0.61.1 depends on", "The user requested"), "", []),
                         ["serialx"])


class RunVerdictTest(_Base):
    def test_unreadable_target_pins_and_a_missing_version_still_block(self):
        report = self.report(TARGET, root=(None, None, "no wheel"), resolve=_failed(NOT_FOUND))
        self.assertFalse(report["ok"])

    def test_unreadable_target_pins_and_an_unrelated_conflict_still_block(self):
        report = self.report(TARGET, root=(None, None, "no wheel"), resolve=_failed(UNRELATED))
        self.assertFalse(report["ok"])
        self.assertFalse(any("were not checked against" in w for w in report["warnings"]))

    def test_unreadable_target_pins_and_the_running_pin_is_a_warning(self):
        report = self.report(TARGET, root=(None, None, "no wheel"), resolve=_failed(CONFLICT_DEPENDENCY))
        self.assertTrue(report["ok"], report["blockers"])

    def test_the_running_version_gets_no_hint_for_a_conflict_it_did_not_cause(self):
        report = self.report(resolve=_failed(UNRELATED))
        blocker = next(b for b in report["blockers"] if b.startswith("requirements cannot be resolved"))
        self.assertNotIn("pinned by the running Home Assistant", blocker)
        report = self.report(resolve=_failed(NOT_FOUND))
        blocker = next(b for b in report["blockers"] if b.startswith("requirements cannot be resolved"))
        self.assertNotIn("pinned by the running Home Assistant", blocker)


def _write_tree(root, files):
    for rel, text in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)


def _target_venv(cfg, extra=None):
    """venv-<TARGET> as the entrypoint leaves a finished install: its package and the .ok marker."""
    venv = os.path.join(cfg, f"venv-{TARGET}")
    site = os.path.join(venv, "lib", f"python{sys.version_info[0]}.{sys.version_info[1]}", "site-packages")
    _write_tree(site, {**TARGET_TREE, **_ha_files("1.11.0"), **(extra or {})})
    open(os.path.join(venv, ".ok"), "w").close()
    return site


class SwitchProductionTest(_GateBase):
    """ramses_cc 0.60.4 running, a switch to the target: its requirements resolve against the target's pins, and the
    attribute check blocks with the cv.vol reason."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        for cache in (preflight._HA_PINS, preflight._SWITCH_REPORTS, preflight._HA_MODULE_NAMES):
            cache.clear()
            self.addCleanup(cache.clear)
        self.addCleanup(preflight._drop_wheels)
        running = dict(TARGET_TREE)
        running["homeassistant/helpers/config_validation.py"] = "import voluptuous as vol\n" + TARGET_TREE["homeassistant/helpers/config_validation.py"]
        running_root = tempfile.mkdtemp(prefix="hri-prod-")
        self.addCleanup(shutil.rmtree, running_root, True)
        _write_tree(running_root, running)
        _target_venv(self.cfg)
        comp = os.path.join(self.cfg, "custom_components", "ramses_cc")
        _write_tree(comp, {"manifest.json": json.dumps({"domain": "ramses_cc", "version": "0.60.4",
                                                        "requirements": ["serialx>=1.10.0"], "dependencies": ["usb"]}),
                           "ha_compat.py": "from homeassistant.helpers import config_validation as cv\nif cv.vol is not None:\n    pass\n"})
        self.installer.running, self.installer.running_tag = "ramses_cc", "0.60.4"
        self.installer.config_dir = self.cfg
        self.installer.component_dir = lambda d: os.path.join(self.cfg, "custom_components", d)
        self.installer._entries_of = lambda d: []
        self.hass.config.components = set()
        self.pip = mock.Mock(side_effect=_resolve)
        usb = SimpleNamespace(requirements=["serialx==1.10.0"], dependencies=[], after_dependencies=[], is_built_in=True)
        for p in (mock.patch.object(preflight, "_running_ha_root", return_value=running_root),
                  mock.patch.object(preflight, "_pip_dry_run", self.pip),
                  mock.patch.object(preflight, "_run_pip", side_effect=AssertionError("downloaded although the venv is there")),
                  mock.patch.object(inst_mod.loader, "async_get_integration", mock.AsyncMock(return_value=usb))):
            p.start()
            self.addCleanup(p.stop)
        self._view(CLEAN)

    async def test_requirements_pass_and_cv_vol_blocks(self):
        r = await self._post({"version": TARGET}, "check")
        check = r["check"]
        self.assertFalse(check["ok"])
        self.assertTrue(any("config_validation.vol" in b for b in check["blockers"]), check["blockers"])
        self.assertFalse(any("requirements of" in b for b in check["blockers"]), check["blockers"])
        reqs, constraints = self.pip.call_args.args[1], self.pip.call_args.args[2]
        self.assertIn("serialx==1.11.0", reqs, "usb's requirements come from the target venv")
        self.assertFalse(os.path.exists(constraints))

    async def test_a_timeout_is_could_not_check_and_not_kept(self):
        self.pip.side_effect = lambda *a: {"ok": False, "install": [], "stderr": f"pip did not finish within {preflight.PIP_TIMEOUT_S}s",
                                           "unchecked": True}
        for _ in range(2):
            blockers, warnings = await preflight.switch_requirement_checks(self.hass, self.installer, TARGET)
            self.assertEqual(blockers, [])
            self.assertTrue(any("were not checked" in w and "did not finish" in w for w in warnings), warnings)
        self.assertEqual(self.pip.call_count, 2)

    async def test_a_report_that_is_not_json_is_could_not_check(self):
        self.pip.side_effect = _REAL_DRY_RUN  # the real one, over a pip that answers something that is not a report
        with mock.patch.object(preflight, "_run_pip", return_value=preflight.subprocess.CompletedProcess([], 0, "not json", "")):
            blockers, warnings = await preflight.switch_requirement_checks(self.hass, self.installer, TARGET)
        self.assertEqual(blockers, [])
        self.assertTrue(any("not JSON" in w for w in warnings), warnings)
        self.assertEqual(preflight._SWITCH_REPORTS, {})

    async def test_a_busy_lock_is_could_not_check_after_a_bounded_wait(self):
        with mock.patch.object(preflight, "SWITCH_LOCK_WAIT_S", 0.05):
            await preflight.LOCK.acquire()
            try:
                blockers, warnings = await asyncio.wait_for(
                    preflight.switch_requirement_checks(self.hass, self.installer, TARGET), 5)
            finally:
                preflight.LOCK.release()
        self.assertEqual(blockers, [])
        self.assertTrue(any("another integration check is running" in w for w in warnings), warnings)
        self.pip.assert_not_called()
        self.assertEqual(preflight._SWITCH_REPORTS, {})


_REAL_DRY_RUN = preflight._pip_dry_run


class PreflightProductionTest(unittest.TestCase):
    """ramses_cc 0.61.1 checked for the target through POST /api/releases/preflight: it passes, with zha (an
    after_dependency the running Home Assistant does not load here) taken from the target."""

    def setUp(self):
        for cache in (preflight._HA_PINS, preflight._HA_MODULE_NAMES):
            cache.clear()
            self.addCleanup(cache.clear)
        manifest = {**MANIFEST, "version": "0.61.1", "after_dependencies": ["zha"]}
        self.inst = _installer(self, {"__init__.py": "X = 1\n"}, manifest)
        self.inst.yaml_path = lambda domain: os.path.join(self.inst.config_dir, "none.yaml")
        _write_tree(self.inst.config_dir, {"running/homeassistant/package_constraints.txt": "serialx==1.10.0\n"})
        self.inst.constraints = os.path.join(self.inst.config_dir, "running", "homeassistant", "package_constraints.txt")
        _target_venv(self.inst.config_dir, {"homeassistant/components/zha/manifest.json":
                                            '{"domain": "zha", "requirements": ["zha==0.0.80"], "dependencies": []}'})

    def test_it_passes(self):
        usb = SimpleNamespace(requirements=["serialx==1.10.0"], dependencies=[], after_dependencies=[], is_built_in=True)

        async def load(hass, domain):
            if domain == "usb":
                return usb
            raise inst_mod.loader.IntegrationNotFound(domain)

        real_run = preflight.run

        async def stored(hass, installer, domain, ref, target_ha=None, **kwargs):  # the stored copy, not a GitHub download
            return await real_run(hass, installer, domain, ref, target_ha, source_dir=installer._version_dir(domain, "2.0"))

        pip = mock.Mock(side_effect=_resolve)
        view = build_views.PreflightView(_hass(), self.inst)
        body = {"domain": "demo", "tag": "2.0", "ha": TARGET}
        request = SimpleNamespace(content_type="application/json", headers={"X-Requested-With": "fetch"},
                                  json=mock.AsyncMock(return_value=body))
        with mock.patch.object(build_views.preflight, "run", stored), \
                mock.patch.object(inst_mod.loader, "async_get_integration", mock.AsyncMock(side_effect=load)), \
                mock.patch.object(self.inst, "_requirement_versions", return_value={}), \
                mock.patch.object(preflight, "_pip_dry_run", pip), \
                mock.patch.object(preflight, "_pypi_index", mock.AsyncMock(return_value=None)), \
                mock.patch.object(preflight, "_run_pip", side_effect=AssertionError("downloaded although the venv is there")):
            res = json.loads(asyncio.run(view.post(request)).body)
        self.assertTrue(res["ok"], res)
        report = res["report"]
        self.assertTrue(report["ok"], report["blockers"])
        self.assertEqual(report["target_ha"], TARGET)
        self.assertFalse(any("zha" in w for w in report["warnings"]), report["warnings"])
        self.assertTrue(any(d["domain"] == "zha" and d["found"] for d in report["dependencies"]), report["dependencies"])
        reqs = pip.call_args.args[1]
        self.assertIn("zha==0.0.80", reqs)
        self.assertIn("serialx==1.11.0", reqs)
        self.assertNotIn("serialx==1.10.0", reqs)


if __name__ == "__main__":
    unittest.main()
