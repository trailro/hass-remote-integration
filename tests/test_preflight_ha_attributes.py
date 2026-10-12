"""Attributes of Home Assistant modules an integration reads (``cv.vol``) and the Home Assistant it is checked
for no longer has: the scan of the integration's code, the module contents of the target Home Assistant read
from its source (never imported), and the blocker in the preflight report.

ramses_cc 0.60.4 did ``if cv.vol is not _REAL_VOL:`` with ``config_validation`` imported as ``cv``; Home
Assistant 2026.10.0 dropped ``import voluptuous as vol`` from that module, so the integration failed at setup
while the preflight, which only looked at ``from homeassistant.x import Y``, answered ok."""

import asyncio
import os
import shutil
import tempfile
import unittest
import zipfile
from unittest import mock

from custom_components.integration_manager import preflight
from tests.test_preflight_ha_symbols import _installer, _report

TARGET = "2026.10.0"

# A Home Assistant tree as the target has it: config_validation without `vol`, with a name it imports, a
# deprecated constant behind HA's module __getattr__, and a package with a submodule.
TARGET_TREE = {
    "homeassistant/__init__.py": "",
    "homeassistant/const.py": ("from functools import partial\n"
                               "from .helpers.deprecation import check_if_deprecated_constant\n"
                               "STATE_ON = 'on'\n"
                               "_DEPRECATED_OLD_NAME = object()\n"
                               "__getattr__ = partial(check_if_deprecated_constant, module_globals=globals())\n"),
    "homeassistant/helpers/__init__.py": "",
    "homeassistant/helpers/deprecation.py": "def check_if_deprecated_constant(name, module_globals):\n    raise AttributeError(name)\n",
    "homeassistant/helpers/config_validation.py": ("import probatio\n"
                                                   "from homeassistant.const import STATE_ON as ON\n"
                                                   "string = probatio.Coerce(str)\n"
                                                   "def make_entity_service_schema(schema):\n    return schema\n"
                                                   "try:\n    import ujson as fastjson\nexcept ImportError:\n    fastjson = None\n"),
    "homeassistant/helpers/magic.py": "def __getattr__(name):\n    return name\n",
    "homeassistant/components/__init__.py": "",
    "homeassistant/components/sensor/__init__.py": "class SensorEntity:\n    pass\n",
    "homeassistant/components/sensor/const.py": "UNIT = 'x'\n",
}


def _tree(test, files):
    root = tempfile.mkdtemp(prefix="hri-haattr-")
    test.addCleanup(shutil.rmtree, root, ignore_errors=True)
    for rel, text in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return root


class _Base(unittest.TestCase):
    def setUp(self):
        preflight._HA_MODULE_NAMES.clear()
        self.addCleanup(preflight._HA_MODULE_NAMES.clear)
        self.target_root = _tree(self, TARGET_TREE)
        # what the running Home Assistant has: config_validation with `vol`, as 2026.9 had it
        running = dict(TARGET_TREE)
        running["homeassistant/helpers/config_validation.py"] = "import voluptuous as vol\n" + TARGET_TREE["homeassistant/helpers/config_validation.py"]
        self.running_root = _tree(self, running)
        p = mock.patch.object(preflight, "_running_ha_root", return_value=self.running_root)
        p.start()
        self.addCleanup(p.stop)

    def check(self, files, target=TARGET):
        component = _tree(self, files)
        with mock.patch.object(preflight, "_target_ha_root", return_value=(self.target_root, None, "")):
            return preflight._ha_attribute_checks(component, target, "python", "/nonexistent")


class ScanTest(_Base):
    def test_an_alias_of_config_validation_reading_vol_is_reported(self):
        hits, note = self.check({"ha_compat.py": "from homeassistant.helpers import config_validation as cv\n"
                                                 "def f():\n    if cv.vol is not None:\n        return 1\n"})
        self.assertEqual(note, "")
        self.assertEqual(hits, ["ha_compat.py:3 uses homeassistant.helpers.config_validation.vol, "
                                "which Home Assistant 2026.10.0 does not have"])

    def test_import_as_and_the_first_level_of_a_chain(self):
        hits, _ = self.check({"__init__.py": "import homeassistant.helpers.config_validation as cv\nX = cv.vol.Schema({})\n"})
        self.assertEqual(hits, ["__init__.py:2 uses homeassistant.helpers.config_validation.vol, "
                                "which Home Assistant 2026.10.0 does not have"])

    def test_a_dotted_use_of_a_plain_import(self):
        hits, _ = self.check({"__init__.py": "import homeassistant.helpers.config_validation\n"
                                             "S = homeassistant.helpers.config_validation.vol.Schema\n"})
        self.assertEqual(len(hits), 1)
        self.assertIn("homeassistant.helpers.config_validation.vol", hits[0])

    def test_getattr_with_a_default_is_not_reported(self):
        hits, _ = self.check({"ha_compat.py": "from homeassistant.helpers import config_validation as cv\n"
                                              "if getattr(cv, 'vol', None) is not None:\n    pass\n"})
        self.assertEqual(hits, [])

    def test_a_use_behind_hasattr_or_except_attribute_error_is_not_reported(self):
        hits, _ = self.check({"a.py": "from homeassistant.helpers import config_validation as cv\n"
                                      "if hasattr(cv, 'vol'):\n    V = cv.vol\n",
                              "b.py": "from homeassistant.helpers import config_validation as cv\n"
                                      "try:\n    V = cv.vol\nexcept AttributeError:\n    V = None\n"})
        self.assertEqual(hits, [])

    def test_attributes_the_target_has_are_not_reported(self):
        hits, _ = self.check({"__init__.py": "from homeassistant.helpers import config_validation as cv\n"
                                             "from homeassistant import const\n"
                                             "from homeassistant.components import sensor\n"
                                             "A = cv.string\nB = cv.make_entity_service_schema\nC = cv.probatio\nD = cv.ON\n"
                                             "E = cv.fastjson\nF = const.OLD_NAME\nG = sensor.SensorEntity\nH = sensor.const\n"
                                             "I = cv.__name__\n"})
        self.assertEqual(hits, [])

    def test_a_module_with_its_own_getattr_is_not_judged(self):
        hits, _ = self.check({"__init__.py": "from homeassistant.helpers import magic\nX = magic.anything\n"})
        self.assertEqual(hits, [])

    def test_a_name_imported_from_a_module_is_not_a_module(self):
        hits, _ = self.check({"__init__.py": "from homeassistant.helpers.config_validation import string\nX = string.anything\n"})
        self.assertEqual(hits, [])

    def test_assigning_an_attribute_is_not_reading_it(self):
        hits, _ = self.check({"__init__.py": "from homeassistant.helpers import config_validation as cv\ncv.vol = None\n"})
        self.assertEqual(hits, [])

    def test_a_name_bound_again_is_not_the_module(self):
        # both seen in Home Assistant's own components (assist_satellite, wyoming): the alias reused as a variable
        hits, _ = self.check({"a.py": "from homeassistant.helpers import config_validation as cv\n"
                                      "X = {n: cv.vol for n, cv in {}.items()}\n"
                                      "def f(cv):\n    return cv.vol\n"
                                      "def g():\n    cv = object()\n    return cv.vol\n",
                              "b.py": "from homeassistant.helpers import config_validation as cv\n"
                                      "for cv in []:\n    pass\nY = cv.vol\n"})
        self.assertEqual(hits, [])

    def test_the_module_is_still_seen_beside_a_scope_that_shadows_it(self):
        hits, _ = self.check({"a.py": "from homeassistant.helpers import config_validation as cv\n"
                                      "def f(cv):\n    return cv.vol\n"
                                      "def g():\n    return [x for x in cv.vol]\n"})
        self.assertEqual([h.split(" uses ")[0] for h in hits], ["a.py:5"])

    def test_one_line_per_attribute_and_file(self):
        hits, _ = self.check({"__init__.py": "from homeassistant.helpers import config_validation as cv\n"
                                             "A = cv.vol\nB = cv.vol\nC = cv.gone\n"})
        self.assertEqual([h.split(" uses ")[0] for h in hits], ["__init__.py:2", "__init__.py:4"])

    def test_folders_home_assistant_never_loads_are_not_scanned(self):
        hits, _ = self.check({"__init__.py": "X = 1\n",
                              "tests/test_x.py": "from homeassistant.helpers import config_validation as cv\nA = cv.vol\n"})
        self.assertEqual(hits, [])

    def test_nothing_used_means_nothing_resolved(self):
        component = _tree(self, {"__init__.py": "import os\nX = os.sep\n"})
        with mock.patch.object(preflight, "_target_ha_root", side_effect=AssertionError("resolved for nothing")):
            self.assertEqual(preflight._ha_attribute_checks(component, TARGET, "python", "/nonexistent"), ([], ""))

    def test_a_target_that_cannot_be_read_is_a_note_not_a_hit(self):
        component = _tree(self, {"__init__.py": "from homeassistant.helpers import config_validation as cv\nA = cv.vol\n"})
        with mock.patch.object(preflight, "_target_ha_root", return_value=(None, None, "PyPI could not be reached")):
            hits, note = preflight._ha_attribute_checks(component, TARGET, "python", "/nonexistent")
        self.assertEqual(hits, [])
        self.assertIn("PyPI could not be reached", note)


class TargetSourceTest(_Base):
    def test_the_running_version_is_read_from_the_running_install(self):
        root, cleanup, note = preflight._target_ha_root(preflight.ha_version, "python", "/nonexistent")
        self.assertEqual((root, cleanup, note), (self.running_root, None, ""))

    def test_a_venv_on_the_volume_is_read_in_place(self):
        cfg = _tree(self, {})
        site = os.path.join(cfg, f"venv-{TARGET}", "lib", f"python{preflight.sys.version_info[0]}.{preflight.sys.version_info[1]}", "site-packages")
        os.makedirs(os.path.join(site, "homeassistant"))
        open(os.path.join(site, "homeassistant", "__init__.py"), "w").close()
        with mock.patch.object(preflight, "_run_pip", side_effect=AssertionError("downloaded although the venv is there")):
            root, cleanup, note = preflight._target_ha_root(TARGET, "python", cfg)
        self.assertEqual((root, cleanup, note), (site, None, ""))

    def test_any_other_version_comes_from_its_wheel_and_the_download_is_removed(self):
        wheel_src = _tree(self, {})
        wheel = os.path.join(wheel_src, f"homeassistant-{TARGET}-py3-none-any.whl")
        with zipfile.ZipFile(wheel, "w") as zf:
            for rel, text in TARGET_TREE.items():
                zf.writestr(rel, text)
        calls = []

        def pip(cmd):
            calls.append(cmd)
            dest = cmd[cmd.index("--dest") + 1]
            os.makedirs(dest)
            shutil.copy(wheel, dest)
            return preflight.subprocess.CompletedProcess(cmd, 0, "", "")

        component = _tree(self, {"__init__.py": "from homeassistant.helpers import config_validation as cv\nA = cv.vol\nB = cv.string\n"})
        with mock.patch.object(preflight, "_run_pip", side_effect=pip):
            hits, note = preflight._ha_attribute_checks(component, TARGET, "python", "/nonexistent")
            self.assertEqual(note, "")
            self.assertEqual(len(hits), 1)
            self.assertIn("config_validation.vol", hits[0])
            self.assertIn(f"homeassistant=={TARGET}", calls[0])
            for flag in ("download", "--no-deps", "--only-binary=:all:"):
                self.assertIn(flag, calls[0])
            dest = calls[0][calls[0].index("--dest") + 1]
            self.assertFalse(os.path.exists(dest), "the downloaded wheel was left behind")
            # the module contents are kept per version: a second check downloads nothing
            self.assertEqual(preflight._ha_attribute_checks(component, TARGET, "python", "/nonexistent"), (hits, ""))
        self.assertEqual(len(calls), 1)

    def test_a_download_that_fails_says_so(self):
        failed = preflight.subprocess.CompletedProcess([], 1, "", "ERROR: Could not find a version that satisfies the requirement")
        with mock.patch.object(preflight, "_run_pip", return_value=failed):
            root, cleanup, note = preflight._target_ha_root(TARGET, "python", "/nonexistent")
        self.assertIsNone(root)
        self.assertIn(f"Home Assistant {TARGET}", note)

    def test_a_target_that_is_not_a_version_is_never_handed_to_pip(self):
        with mock.patch.object(preflight, "_run_pip", side_effect=AssertionError("pip ran")):
            root, _cleanup, note = preflight._target_ha_root("2026.10.0 --index-url=http://x", "python", "/nonexistent")
        self.assertIsNone(root)
        self.assertTrue(note)


class ReportTest(_Base):
    def report(self, code, target=TARGET):
        inst = _installer(self, code)
        with mock.patch.object(preflight, "_target_ha_root", return_value=(self.target_root, None, "")):
            return _report(inst, target)

    def test_the_attribute_the_target_lacks_blocks_the_release(self):
        report = self.report("from homeassistant.helpers import config_validation as cv\n"
                             "def f():\n    return cv.vol is None\n")
        self.assertFalse(report["ok"])
        blocker = next(b for b in report["blockers"] if "config_validation.vol" in b)
        self.assertIn("__init__.py:3", blocker)
        self.assertIn(f"Home Assistant {TARGET}", blocker)
        self.assertEqual(len(report["removed_ha_attributes"]), 1)

    def test_getattr_with_a_default_does_not_block(self):
        report = self.report("from homeassistant.helpers import config_validation as cv\n"
                             "def f():\n    return getattr(cv, 'vol', None) is None\n")
        self.assertTrue(report["ok"], report["blockers"])
        self.assertEqual(report["removed_ha_attributes"], [])

    def test_an_attribute_the_target_has_does_not_block(self):
        report = self.report("from homeassistant.helpers import config_validation as cv\nS = cv.string\n")
        self.assertTrue(report["ok"], report["blockers"])
        self.assertEqual(report["removed_ha_attributes"], [])

    def test_a_target_that_cannot_be_read_warns_and_does_not_block(self):
        inst = _installer(self, "from homeassistant.helpers import config_validation as cv\nS = cv.vol\n")
        with mock.patch.object(preflight, "_target_ha_root", return_value=(None, None, "PyPI could not be reached")):
            report = _report(inst, TARGET)
        self.assertTrue(report["ok"], report["blockers"])
        self.assertTrue(any("PyPI could not be reached" in w for w in report["warnings"]), report["warnings"])


if __name__ == "__main__":
    unittest.main()
