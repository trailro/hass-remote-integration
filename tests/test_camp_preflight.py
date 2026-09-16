"""Preflight verdicts the test campaign found too soft: an import of a module Python removed that nothing
can provide, a stored copy without a manifest, warnings that never leave the gate, and the report cache."""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import manage_views, preflight
from custom_components.integration_manager.installer import Installer, State


def _hass():
    async def job(fn, *args):
        return fn(*args)
    return SimpleNamespace(async_add_executor_job=job, is_running=True, loop=mock.Mock(), async_create_task=mock.Mock())


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _installer(test, stored_files, manifest=None, running="1.0", target="2.0"):
    """An installer whose store holds ``target`` as ``stored_files`` (plus ``manifest``, unless None)."""
    d = tempfile.mkdtemp(prefix="hri-ca6-")
    test.addCleanup(shutil.rmtree, d, ignore_errors=True)
    inst = object.__new__(Installer)
    inst.config_dir, inst.state_dir = d, os.path.join(d, "integration_manager")
    inst.versions_dir = os.path.join(inst.state_dir, "versions")
    inst.constraints = ""
    inst._req_versions_cache = {}
    inst.state = State(domain="demo", installed={"demo": {"running_tag": running, "versions": {
        running: {}, target: {"installed_at": "2026-09-01T10:00:00", "min_ha": None}}}})
    inst.spec = lambda dom: {"repo": "owner/repo"}
    inst.settings = SimpleNamespace(github_headers=lambda: {})
    inst.installed_manifest = lambda dom=None: {"version": running}
    inst._entries_of = lambda dom: []
    inst.site_packages_for = lambda dom: d
    stored = inst._version_dir("demo", target)
    os.makedirs(stored, exist_ok=True)
    if manifest is not None:
        _write(os.path.join(stored, "manifest.json"), json.dumps(manifest))
    for name, text in stored_files.items():
        _write(os.path.join(stored, name), text)
    return inst


def _removed_warning(report):
    return next((w for w in report["warnings"] if "no longer has" in w), None)


def _run(inst, install_rows=(), target="2.0"):
    pip = {"ok": True, "install": list(install_rows), "stderr": ""}
    with mock.patch.object(preflight, "_pip_dry_run", return_value=pip):
        return asyncio.run(preflight.run(_hass(), inst, "demo", target, source_dir=inst._version_dir("demo", target)))


class RemovedModuleImportTest(unittest.TestCase):
    """v4.0.0 of the test integration: `import imp`, no requirements at all."""

    def test_nothing_can_provide_it_so_it_blocks(self):
        inst = _installer(self, {"__init__.py": "import imp\n"}, {"domain": "demo", "version": "2.0", "config_flow": True, "requirements": []})
        report = _run(inst)
        self.assertFalse(report["ok"])
        self.assertEqual(len(report["blockers"]), 1)
        self.assertIn("__init__.py:1 imports imp", report["blockers"][0])
        self.assertIn("none of its requirements provides them", report["blockers"][0])
        self.assertEqual(_removed_warning(report), None)

    def test_a_requirement_named_like_the_module_stays_a_warning(self):
        inst = _installer(self, {"__init__.py": "import asyncore\n"},
                          {"domain": "demo", "version": "2.0", "config_flow": True, "requirements": ["asyncore==1.0"]})
        report = _run(inst)
        self.assertTrue(report["ok"])
        self.assertIn("imports asyncore", _removed_warning(report))

    def test_a_pep_594_shim_pulled_in_by_another_requirement_does_too(self):
        inst = _installer(self, {"__init__.py": "import imghdr\n"},
                          {"domain": "demo", "version": "2.0", "config_flow": True, "requirements": ["pillow-thumbs==1.0"]})
        report = _run(inst, [{"name": "standard-imghdr", "version": "3.13.0"}])  # resolved, not in the manifest
        self.assertTrue(report["ok"])
        self.assertEqual(report["blockers"], [])

    def test_provided_and_unprovided_in_the_same_version(self):
        inst = _installer(self, {"__init__.py": "import asyncore\nimport imp\n"},
                          {"domain": "demo", "version": "2.0", "config_flow": True, "requirements": ["asyncore==1.0"]})
        report = _run(inst)
        self.assertIn("imports imp", report["blockers"][0])
        self.assertNotIn("asyncore", report["blockers"][0])
        self.assertIn("imports asyncore", _removed_warning(report))


class StoredCopyWithoutManifestTest(unittest.TestCase):
    def test_the_gate_blocks_instead_of_skipping(self):
        inst = _installer(self, {"__init__.py": "x = 1\n"}, manifest=None)  # a half-written copy
        preflight._REPORTS.clear()
        with mock.patch.object(preflight, "_pip_dry_run", return_value={"ok": True, "install": [], "stderr": ""}):
            res = asyncio.run(preflight.gate(_hass(), inst, "demo", "2.0"))
        self.assertTrue(res["blocked"])
        self.assertIsNone(res["skipped"])
        self.assertIn("no manifest.json", res["report"]["blockers"][0])

    def test_a_transient_failure_still_only_skips(self):
        inst = _installer(self, {"__init__.py": "x = 1\n"}, {"domain": "demo", "version": "2.0"})
        preflight._REPORTS.clear()
        with mock.patch.object(preflight, "run", mock.AsyncMock(side_effect=ValueError("GitHub answered 503"))):
            res = asyncio.run(preflight.gate(None, inst, "demo", "2.0"))
        self.assertFalse(res["blocked"])
        self.assertIn("GitHub answered 503", res["skipped"])


class StartAnswersWithTheWarningsTest(unittest.TestCase):
    def _start(self, gate):
        view = object.__new__(manage_views.RunView)
        view.installer = SimpleNamespace(hass=None, start=mock.AsyncMock(return_value={"ok": True, "tag": "2.0"}))
        view.publisher = SimpleNamespace(stats={}, base_topic="t", async_after_start=mock.AsyncMock())
        request = SimpleNamespace(headers={}, query={}, content_type="application/json",
                                  json=mock.AsyncMock(return_value={"domain": "demo", "tag": "2.0"}))
        with mock.patch.object(manage_views.preflight, "gate", mock.AsyncMock(return_value=gate)):
            return json.loads(asyncio.run(view.post(request, action="start")).body)

    def test_a_passing_gate_with_warnings(self):
        res = self._start({"blocked": False, "skipped": None,
                           "report": {"ok": True, "blockers": [], "warnings": ["imports imp"]}})
        self.assertTrue(res["ok"])
        self.assertEqual(res["preflight_warnings"], ["imports imp"])

    def test_a_clean_gate_says_nothing(self):
        res = self._start({"blocked": False, "skipped": None, "report": {"ok": True, "blockers": [], "warnings": []}})
        self.assertNotIn("preflight_warnings", res)


class ReportCacheIsBoundedTest(unittest.TestCase):
    def setUp(self):
        preflight._REPORTS.clear()
        self.addCleanup(preflight._REPORTS.clear)

    def test_a_reinstall_loop_does_not_grow_it(self):
        for i in range(preflight.MAX_REPORTS * 3):
            preflight.remember("demo", f"stored:2.0\n2026-09-01T10:00:{i:02d}", {"ok": True, "blockers": []})
        self.assertEqual(len(preflight._REPORTS), preflight.MAX_REPORTS)

    def test_the_newest_survive(self):
        for i in range(preflight.MAX_REPORTS + 5):
            preflight.remember("demo", str(i), {"ok": True})
        self.assertIsNotNone(preflight.recent("demo", str(preflight.MAX_REPORTS + 4)))
        self.assertIsNone(preflight.recent("demo", "0"))

    def test_stale_entries_go_when_the_next_one_is_remembered(self):
        preflight.remember("demo", "old", {"ok": True})
        with mock.patch.object(preflight.time, "monotonic", return_value=preflight.time.monotonic() + preflight.CACHE_S + 1):
            preflight.remember("demo", "new", {"ok": True})
        self.assertEqual(list(preflight._REPORTS), [("demo", "new", preflight.ha_version)])


if __name__ == "__main__":
    unittest.main()
