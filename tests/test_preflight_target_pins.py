"""The integration preflight for another Home Assistant version resolves the requirements against that version's
pins, not the running one's.

ramses_cc 0.61.1 requires serialx>=1.11.0 and depends on ``usb``.  Home Assistant 2026.9.4 pins serialx==1.10.0
(package_constraints.txt, and its ``usb`` component's requirements); 2026.10.x pins 1.11.0.  The preflight of
0.61.1 for 2026.10.2, run on 2026.9.4, resolved against 2026.9.4's pins and blocked a valid combination."""

import asyncio
import os
import shutil
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest import mock

from packaging.requirements import Requirement

from custom_components.integration_manager import installer as inst_mod, preflight
from tests.test_camp_preflight import _hass, _installer, _write

TARGET = "2099.10.2"  # never the version these tests run on
SERIALX = ("1.10.0", "1.11.0")
MANIFEST = {"domain": "demo", "version": "2.0", "config_flow": True,
            "requirements": ["serialx>=1.11.0"], "dependencies": ["usb"]}
OFFLINE = ("WARNING: Retrying (Retry(total=4, connect=None, read=None, redirect=None, status=None)) after connection broken by "
           "'NewConnectionError(\"Failed to establish a new connection: [Errno -3] Temporary failure in name resolution\")'\n"
           "ERROR: Could not find a version that satisfies the requirement serialx>=1.11.0 (from versions: none)")


def _tree(test, files):
    root = tempfile.mkdtemp(prefix="hri-pins-")
    test.addCleanup(shutil.rmtree, root, ignore_errors=True)
    for rel, text in files.items():
        _write(os.path.join(root, rel), text)
    return root


def _ha_files(serialx):
    return {"homeassistant/__init__.py": "",
            "homeassistant/package_constraints.txt": f"# pins\nserialx=={serialx}\naiousbwatcher==1.1.2\n",
            "homeassistant/components/usb/manifest.json":
                f'{{"domain": "usb", "requirements": ["aiousbwatcher==1.1.2", "serialx=={serialx}"], "dependencies": []}}'}


def _cannot_install(reqs):
    joined = reqs[0] if len(reqs) == 1 else ", ".join(reqs[:-1]) + " and " + reqs[-1]
    return (f"ERROR: Cannot install {joined} because these package versions have conflicting dependencies.\n"
            "ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/en/latest/topics/dependency-resolution/"
            "#dealing-with-dependency-conflicts")


def _resolve(python, requirements, constraints):
    """What pip answers, for serialx only: one version satisfying every requirement and constraint, or a conflict."""
    specs = [(req, False) for req in requirements]
    if constraints and os.path.isfile(constraints):
        with open(constraints, encoding="utf-8") as fh:
            specs += [(line.strip(), True) for line in fh if line.strip() and not line.startswith("#")]
    wanted = [(Requirement(r), c) for r, c in specs if Requirement(r).name == "serialx"]
    if not any(not c for _r, c in wanted):
        return {"ok": True, "install": [], "stderr": ""}
    fits = [v for v in SERIALX if all(r.specifier.contains(v) for r, _c in wanted)]
    if fits:
        return {"ok": True, "install": [{"name": "serialx", "version": fits[-1], "url": "", "requested": True,
                                         "requires_python": None, "source_only": False}], "stderr": ""}
    # what pip 25 prints with --quiet (as _pip_dry_run runs it): only the requirements it was given, never the constraint
    err = _cannot_install(sorted(str(r) for r, c in wanted if not c))
    return {"ok": False, "install": [], "stderr": err, "stderr_full": err}


class _Base(unittest.TestCase):
    def setUp(self):
        for cache in (preflight._HA_PINS, preflight._HA_MODULE_NAMES):
            cache.clear()
            self.addCleanup(cache.clear)
        self.addCleanup(preflight._drop_wheels)
        running = _tree(self, _ha_files("1.10.0"))
        self.inst = _installer(self, {"__init__.py": "X = 1\n"}, dict(MANIFEST))
        self.inst.constraints = os.path.join(running, "homeassistant", "package_constraints.txt")
        self.inst.yaml_path = lambda domain: os.path.join(self.inst.config_dir, "none.yaml")
        self.target_root = _tree(self, _ha_files("1.11.0"))
        self.loader = {"usb": SimpleNamespace(requirements=["aiousbwatcher==1.1.2", "serialx==1.10.0"], dependencies=[],
                                              after_dependencies=[], is_built_in=True)}
        self.resolved = []

    def resolve(self, python, requirements, constraints):
        text = ""
        if constraints and os.path.isfile(constraints):
            with open(constraints, encoding="utf-8") as fh:
                text = fh.read()
        self.resolved.append((list(requirements), constraints, text))
        return _resolve(python, requirements, constraints)

    def report(self, target=None, root=None, resolve=None):
        async def load(hass, domain):
            if domain not in self.loader:
                raise inst_mod.loader.IntegrationNotFound(domain)
            return self.loader[domain]

        source = root if root is not None else (self.target_root, None, "")
        with mock.patch.object(inst_mod.loader, "async_get_integration", mock.AsyncMock(side_effect=load)), \
                mock.patch.object(self.inst, "_requirement_versions", return_value={}), \
                mock.patch.object(preflight, "_pip_dry_run", side_effect=resolve or self.resolve), \
                mock.patch.object(preflight, "_pypi_index", mock.AsyncMock(return_value=None)), \
                mock.patch.object(preflight, "_target_ha_root", return_value=source) as where:
            report = asyncio.run(preflight.run(_hass(), self.inst, "demo", "2.0", target_ha=target,
                                               source_dir=self.inst._version_dir("demo", "2.0")))
        self.where = where
        return report


class TargetPinsTest(_Base):
    def test_the_running_home_assistant_blocks_and_says_where_the_pin_comes_from(self):
        report = self.report()
        self.assertFalse(report["ok"])
        blocker = next(b for b in report["blockers"] if b.startswith("requirements cannot be resolved"))
        self.assertIn(f"serialx pinned by the running Home Assistant {preflight.ha_version}", blocker)
        self.assertIn("environment builder", blocker)
        self.assertIn("switching Home Assistant alone first works only if the integration version running now supports", blocker)
        self.assertLess(blocker.index("environment builder"), blocker.index("switching Home Assistant alone"))
        self.where.assert_not_called()  # the running version's pins are its own: nothing else is read
        reqs, constraints, _text = self.resolved[0]
        self.assertEqual(constraints, self.inst.constraints)
        self.assertIn("serialx==1.10.0", reqs)

    def test_the_target_resolves_against_its_own_pins(self):
        report = self.report(TARGET)
        self.assertTrue(report["ok"], report["blockers"])
        reqs, constraints, text = self.resolved[0]
        self.assertIn("serialx==1.11.0", text)
        self.assertNotIn("serialx==1.10.0", text)
        self.assertIn("serialx==1.11.0", reqs, "the usb component's requirements are the target's")
        self.assertNotIn("serialx==1.10.0", reqs)
        self.assertEqual([r for r in report["requirements"] if r["requirement"] == "serialx>=1.11.0"][0]["after"], "1.11.0")
        self.assertTrue(any(f"resolved against the pins of Home Assistant {TARGET}" in w for w in report["warnings"]), report["warnings"])
        self.assertFalse(os.path.exists(constraints), "the target's constraints file outlived the preflight")

    def test_a_requirement_the_target_cannot_satisfy_still_blocks(self):
        self.inst = _installer(self, {"__init__.py": "X = 1\n"}, {**MANIFEST, "requirements": ["serialx>=1.12.0"]})
        self.inst.constraints = ""
        self.inst.yaml_path = lambda domain: os.path.join(self.inst.config_dir, "none.yaml")
        report = self.report(TARGET)
        self.assertFalse(report["ok"])
        self.assertTrue(any(b.startswith("requirements cannot be resolved") for b in report["blockers"]), report["blockers"])

    def test_a_dependency_the_target_does_not_ship_is_said_for_the_target(self):
        self.loader["gone"] = SimpleNamespace(requirements=[], dependencies=[], after_dependencies=[], is_built_in=True)
        self.inst = _installer(self, {"__init__.py": "X = 1\n"}, {**MANIFEST, "dependencies": ["usb", "gone"]})
        self.inst.yaml_path = lambda domain: os.path.join(self.inst.config_dir, "none.yaml")
        report = self.report(TARGET)
        self.assertIn(f"dependency 'gone' is not available in Home Assistant {TARGET}", report["blockers"])

    def test_a_custom_integration_dependency_still_comes_from_the_volume(self):
        self.loader["mine"] = SimpleNamespace(requirements=["minepkg==2"], dependencies=[], after_dependencies=[], is_built_in=False)
        self.inst = _installer(self, {"__init__.py": "X = 1\n"}, {**MANIFEST, "dependencies": ["usb", "mine"]})
        self.inst.yaml_path = lambda domain: os.path.join(self.inst.config_dir, "none.yaml")
        self.report(TARGET)
        self.assertIn("minepkg==2", self.resolved[0][0])

    def test_the_target_from_a_wheel_is_read_once_per_process(self):
        wheel_src = _tree(self, {})
        wheel = os.path.join(wheel_src, f"homeassistant-{TARGET}-py3-none-any.whl")
        with zipfile.ZipFile(wheel, "w") as zf:
            for rel, text in _ha_files("1.11.0").items():
                zf.writestr(rel, text)
        calls = []

        def pip(cmd):
            calls.append(cmd)
            dest = cmd[cmd.index("--dest") + 1]
            os.makedirs(dest)
            shutil.copy(wheel, dest)
            return preflight.subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(preflight, "_run_pip", side_effect=pip):
            first, why = preflight._target_pins(TARGET, "python", "/nonexistent")
            self.assertEqual(why, "")
            self.assertIn("serialx==1.11.0", first[0])
            self.assertEqual(first[1]["usb"]["requirements"], ["aiousbwatcher==1.1.2", "serialx==1.11.0"])
            self.assertEqual(preflight._target_pins(TARGET, "python", "/nonexistent"), (first, ""))
            preflight._HA_PINS.clear()  # the pins forgotten, the wheel kept: still no second download
            self.assertEqual(preflight._target_pins(TARGET, "python", "/nonexistent")[0], first)
        self.assertEqual(len(calls), 1)

    def test_only_a_few_versions_are_kept(self):
        for minor in range(1, preflight.MAX_HA_MODULE_VERSIONS + 2):
            with mock.patch.object(preflight, "_target_ha_root", return_value=(self.target_root, None, "")):
                preflight._target_pins(f"2099.{minor}.0", "python", "/nonexistent")
        self.assertEqual(len(preflight._HA_PINS), preflight.MAX_HA_MODULE_VERSIONS)
        self.assertNotIn("2099.1.0", preflight._HA_PINS)

    def test_a_target_without_package_constraints_is_not_read(self):
        bare = _tree(self, {"homeassistant/__init__.py": ""})
        with mock.patch.object(preflight, "_target_ha_root", return_value=(bare, None, "")):
            pins, why = preflight._target_pins(TARGET, "python", "/nonexistent")
        self.assertIsNone(pins)
        self.assertIn("package_constraints.txt", why)


class CouldNotCheckTest(_Base):
    def test_target_pins_unreadable_and_a_conflict_on_a_running_pin_does_not_block(self):
        report = self.report(TARGET, root=(None, None, f"Home Assistant {TARGET} could not be downloaded (PyPI could not be reached: x)"))
        self.assertTrue(report["ok"], report["blockers"])
        warning = next(w for w in report["warnings"] if "were not checked against" in w)
        self.assertIn(f"Home Assistant {TARGET}'s pins", warning)
        self.assertIn("could not be downloaded", warning)
        self.assertIn("serialx", warning)

    def test_pypi_unreachable_for_another_target_does_not_block(self):
        offline = {"ok": False, "install": [], "stderr": OFFLINE, "stderr_full": OFFLINE}
        report = self.report(TARGET, resolve=lambda *a: offline)
        self.assertTrue(report["ok"], report["blockers"])
        self.assertTrue(any("PyPI could not be reached" in w for w in report["warnings"]), report["warnings"])

    def test_target_pins_unreadable_and_a_conflict_elsewhere_still_blocks(self):
        def resolve(python, requirements, constraints):
            err = "ERROR: Could not find a version that satisfies the requirement nowheel==1 (from versions: 0.1, 0.2)"
            return {"ok": False, "install": [], "stderr": err, "stderr_full": err}

        report = self.report(TARGET, root=(None, None, "no wheel"), resolve=resolve)
        self.assertFalse(report["ok"])

    def test_the_running_version_unchanged_offline(self):
        offline = {"ok": False, "install": [], "stderr": OFFLINE, "stderr_full": OFFLINE}
        report = self.report(resolve=lambda *a: offline)
        self.assertFalse(report["ok"])
        blocker = next(b for b in report["blockers"] if b.startswith("requirements cannot be resolved"))
        self.assertNotIn("switch Home Assistant", blocker, "no network is not a pin of the running version")


if __name__ == "__main__":
    unittest.main()
