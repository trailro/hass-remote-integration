"""Names Home Assistant removed: the table, the scan of a release's imports against the Home Assistant the
container will run, and the warning's way into the preflight report (a warning, never a blocker)."""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import preflight
from custom_components.integration_manager.installer import Installer, State
from jsonio import ha_vkey


def _component(files):
    d = tempfile.mkdtemp(prefix="hri-hasym-")
    for rel, text in files.items():
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
    return d


def _check(test, files, target="2026.9.3"):
    d = _component(files)
    test.addCleanup(shutil.rmtree, d, ignore_errors=True)
    return preflight._ha_symbol_checks(d, target)


class TableTest(unittest.TestCase):
    def test_every_row_is_a_home_assistant_path_with_a_version_that_parses(self):
        for module, symbols in preflight._REMOVED_HA_SYMBOLS.items():
            self.assertTrue(module.startswith("homeassistant."), module)
            for symbol, (removed_in, replacement) in symbols.items():
                self.assertEqual(ha_vkey(removed_in)[3], 1, f"{module}.{symbol}: {removed_in} is not a release")
                self.assertGreaterEqual(ha_vkey(removed_in), ha_vkey("2026.1.0"), f"{module}.{symbol}")
                self.assertNotEqual(replacement, f"{module}.{symbol}", f"{module}.{symbol} replaces itself")

    def test_the_symbols_the_survey_found_are_in_it(self):
        for module, symbol in (("homeassistant.const", "CLOUD_NEVER_EXPOSED_ENTITIES"),
                               ("homeassistant.helpers.trigger", "async_track_same_state"),
                               ("homeassistant.helpers.service", "async_extract_referenced_entity_ids"),
                               ("homeassistant.helpers.device_registry", "DEVICE_INFO_TYPES"),
                               ("homeassistant.components.vacuum", "ATTR_BATTERY_LEVEL")):
            self.assertIn(symbol, preflight._REMOVED_HA_SYMBOLS.get(module, {}), f"{module}.{symbol}")


class ScanTest(unittest.TestCase):
    def test_a_removed_symbol_is_reported_with_its_file_line_and_version(self):
        hits = _check(self, {"__init__.py": "import asyncio\nfrom homeassistant.const import CLOUD_NEVER_EXPOSED_ENTITIES\n"})
        self.assertEqual(hits, ["__init__.py:2 imports homeassistant.const.CLOUD_NEVER_EXPOSED_ENTITIES, removed in 2026.6.0"])

    def test_the_replacement_is_named_when_there_is_one(self):
        hits = _check(self, {"helper.py": "from homeassistant.helpers.service import async_extract_referenced_entity_ids\n"})
        self.assertEqual(hits, ["helper.py:1 imports homeassistant.helpers.service.async_extract_referenced_entity_ids, "
                                "removed in 2026.8.0, now homeassistant.helpers.target.async_extract_referenced_entity_ids"])

    def test_no_replacement_is_invented_for_a_name_that_only_went_away(self):
        hits = _check(self, {"helper.py": "from homeassistant.helpers.service import ServiceTargetSelector\n"})
        self.assertEqual(len(hits), 1)
        self.assertNotIn(", now ", hits[0])

    def test_a_symbol_removed_in_a_newer_home_assistant_than_ours_is_quiet(self):
        files = {"__init__.py": "from homeassistant.helpers.device_registry import DEVICE_INFO_TYPES\n"}
        self.assertEqual(_check(self, files, target="2026.8.3"), [])
        self.assertEqual(len(_check(self, files, target="2026.9.3")), 1)

    def test_it_fires_from_the_removal_version_on(self):
        files = {"__init__.py": "from homeassistant.helpers.trigger import async_track_same_state\n"}
        self.assertEqual(_check(self, files, target="2026.6.4"), [])
        self.assertEqual(len(_check(self, files, target="2026.7.0")), 1)
        self.assertEqual(len(_check(self, files, target="2026.11.1")), 1)

    def test_a_beta_of_the_removal_version_is_quiet(self):
        files = {"__init__.py": "from homeassistant.helpers.trigger import async_track_same_state\n"}
        self.assertEqual(_check(self, files, target="2026.7.0b3"), [])

    def test_an_import_guarded_by_try_except_import_error_is_quiet(self):
        hits = _check(self, {"compat.py": "try:\n    from homeassistant.helpers.service import SelectedEntities\n"
                                          "except ImportError:\n    from homeassistant.helpers.target import SelectedEntities\n"})
        self.assertEqual(hits, [])

    def test_the_replacement_import_itself_says_nothing(self):
        hits = _check(self, {"__init__.py": "from homeassistant.helpers.target import async_extract_referenced_entity_ids\n"})
        self.assertEqual(hits, [])

    def test_importing_the_module_is_not_importing_the_symbol(self):
        hits = _check(self, {"__init__.py": "import homeassistant.helpers.service\nfrom homeassistant.helpers import service\n"})
        self.assertEqual(hits, [])

    def test_a_relative_import_of_the_same_name_is_the_integration_s_own(self):
        hits = _check(self, {"service.py": "X = 1\n", "__init__.py": "from .service import SelectedEntities\n"})
        self.assertEqual(hits, [])

    def test_an_alias_is_reported_under_the_name_home_assistant_removed(self):
        hits = _check(self, {"__init__.py": "from homeassistant.runner import HassEventLoopPolicy as Policy\n"})
        self.assertEqual(len(hits), 1)
        self.assertIn("homeassistant.runner.HassEventLoopPolicy, removed in 2026.9.3, "
                      "now homeassistant.runner.create_event_loop", hits[0])

    def test_folders_home_assistant_never_loads_are_not_scanned(self):
        hits = _check(self, {"__init__.py": "X = 1\n",
                             "tests/test_it.py": "from homeassistant.const import CLOUD_NEVER_EXPOSED_ENTITIES\n",
                             "scripts/gen.py": "from homeassistant.runner import HassEventLoopPolicy\n"})
        self.assertEqual(hits, [])

    def test_a_file_that_does_not_parse_is_left_to_the_syntax_check(self):
        hits = _check(self, {"__init__.py": "print 'py2'\n"})
        self.assertEqual(hits, [])

    def test_every_hit_in_a_file_is_reported(self):
        hits = _check(self, {"__init__.py": "from homeassistant.components.http import SERVER_PORT, MAX_CLIENT_SIZE\n"})
        self.assertEqual(len(hits), 2)
        self.assertTrue(all(h.startswith("__init__.py:1 imports homeassistant.components.http.") for h in hits))


# ----- the report ---------------------------------------------------------


def _hass():
    async def job(fn, *args):
        return fn(*args)
    return SimpleNamespace(async_add_executor_job=job, is_running=True, loop=mock.Mock(), async_create_task=mock.Mock())


def _installer(test, code, ref="2.0"):
    """An installer whose store holds ``ref``: a manifest with no requirements and ``code`` as __init__.py."""
    d = tempfile.mkdtemp(prefix="hri-hasym-run-")
    test.addCleanup(shutil.rmtree, d, ignore_errors=True)
    inst = object.__new__(Installer)
    inst.config_dir, inst.state_dir = d, os.path.join(d, "integration_manager")
    inst.versions_dir = os.path.join(inst.state_dir, "versions")
    inst.constraints = ""
    inst._req_versions_cache = {}
    inst.state = State(domain="demo", installed={"demo": {"running_tag": "1.0", "versions": {
        "1.0": {}, ref: {"installed_at": "2026-09-01T10:00:00", "min_ha": None}}}})
    inst.spec = lambda dom: {"repo": "owner/repo"}
    inst.settings = SimpleNamespace(github_headers=lambda: {})
    inst.installed_manifest = lambda dom=None: {"version": "1.0"}
    inst._entries_of = lambda dom: []
    inst.site_packages_for = lambda dom: d
    stored = inst._version_dir("demo", ref)
    os.makedirs(stored, exist_ok=True)
    with open(os.path.join(stored, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"domain": "demo", "version": ref, "config_flow": True, "requirements": []}, fh)
    with open(os.path.join(stored, "__init__.py"), "w", encoding="utf-8") as fh:
        fh.write(code)
    return inst


def _report(inst, target_ha, ref="2.0"):
    with mock.patch.object(preflight, "_pip_dry_run", return_value={"ok": True, "install": [], "stderr": ""}):
        return asyncio.run(preflight.run(_hass(), inst, "demo", ref, target_ha=target_ha,
                                         source_dir=inst._version_dir("demo", ref)))


def _symbol_warning(report):
    return next((w for w in report["warnings"] if "no longer has" in w and "Home Assistant" in w), None)


class ReportTest(unittest.TestCase):
    CODE = "from homeassistant.helpers.service import async_extract_referenced_entity_ids\n"

    def test_the_warning_names_the_file_the_version_and_the_replacement(self):
        report = _report(_installer(self, self.CODE), "2026.8.3")
        warning = _symbol_warning(report)
        self.assertIsNotNone(warning, report["warnings"])
        self.assertIn("__init__.py:1", warning)
        self.assertIn("removed in 2026.8.0", warning)
        self.assertIn("now homeassistant.helpers.target.async_extract_referenced_entity_ids", warning)
        self.assertEqual(len(report["removed_ha_symbols"]), 1)

    def test_it_never_blocks(self):
        report = _report(_installer(self, self.CODE), "2026.8.3")
        self.assertEqual(report["blockers"], [])
        self.assertTrue(report["ok"])

    def test_an_older_target_home_assistant_still_has_it_and_the_report_is_silent(self):
        report = _report(_installer(self, self.CODE), "2026.7.3")
        self.assertIsNone(_symbol_warning(report))
        self.assertEqual(report["removed_ha_symbols"], [])
        self.assertTrue(report["ok"])

    def test_a_release_that_imports_nothing_removed_says_nothing(self):
        report = _report(_installer(self, "from homeassistant.helpers.target import SelectedEntities\n"), "2026.9.3")
        self.assertIsNone(_symbol_warning(report))
        self.assertEqual(report["removed_ha_symbols"], [])

    def test_more_than_three_hits_are_counted_not_listed(self):
        code = ("from homeassistant.components.http import (HomeAssistantApplication, MAX_CLIENT_SIZE, ConfData,\n"
                "                                           SERVER_PORT, HomeAssistantTCPSite)\n")
        report = _report(_installer(self, code), "2026.9.3")
        warning = _symbol_warning(report)
        self.assertIn("(+2 more)", warning)
        self.assertEqual(len(report["removed_ha_symbols"]), 5)
        self.assertEqual(report["blockers"], [])


if __name__ == "__main__":
    unittest.main()
