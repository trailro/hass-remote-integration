"""Stored generations invalidate deployment and preflight even in one clock second."""

import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

from custom_components.integration_manager import preflight
from custom_components.integration_manager.installer import Installer, State
from tests.test_preflight_gate import FakeInstaller


class DeploymentGenerationTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="hri-deploy-generation-")
        self.addCleanup(tmp.cleanup)
        self.inst = object.__new__(Installer)
        self.inst.config_dir = tmp.name
        self.inst.versions_dir = os.path.join(tmp.name, "versions")
        self.rec = {"installed_at": "2026-10-05T10:00:00", "stored": "first"}
        self.inst.state = State(installed={"demo": {"versions": {"local": self.rec}}})
        self.source = self.inst._version_dir("demo", "local")
        os.makedirs(self.source)
        with open(os.path.join(self.source, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"domain": "demo", "version": "1.0"}, fh)
        self.write_code("VALUE = 1\n")

    def write_code(self, code):
        with open(os.path.join(self.source, "__init__.py"), "w", encoding="utf-8") as fh:
            fh.write(code)

    def test_changed_copy_with_same_tag_version_and_second_is_deployed(self):
        self.assertTrue(self.inst._ensure_deployed("demo", "local"))
        self.write_code("VALUE = 2\n")
        self.rec["stored"] = "second"
        self.assertTrue(self.inst._ensure_deployed("demo", "local"))
        with open(os.path.join(self.inst._component_dir("demo"), "__init__.py"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "VALUE = 2\n")
        self.assertFalse(self.inst._ensure_deployed("demo", "local"))
        self.assertEqual(self.inst._tag_of_deployed("demo"), ("local", self.rec["installed_at"]))

    def test_legacy_copy_stays_and_stamped_copy_migrates_once(self):
        self.rec.pop("stored")
        self.assertTrue(self.inst._ensure_deployed("demo", "local"))
        self.assertFalse(self.inst._ensure_deployed("demo", "local"))
        self.rec["stored"] = "first"
        self.assertTrue(self.inst._ensure_deployed("demo", "local"))
        self.assertFalse(self.inst._ensure_deployed("demo", "local"))

    def test_generation_marker_preserves_an_empty_legacy_timestamp(self):
        self.rec.pop("installed_at")
        self.assertTrue(self.inst._ensure_deployed("demo", "local"))
        self.assertEqual(self.inst._tag_of_deployed("demo"), ("local", ""))
        self.assertFalse(self.inst._ensure_deployed("demo", "local"))


class PreflightGenerationTest(unittest.TestCase):
    def setUp(self):
        preflight._REPORTS.clear()
        self.addCleanup(preflight._REPORTS.clear)
        self.inst = FakeInstaller()
        self.rec = self.inst.state.installed["probe"]["versions"]["v2.0.0"]
        self.rec.update(installed_at="2026-10-05T10:00:00", stored="first")

    def test_same_second_reinstall_invalidates_cached_report(self):
        async def check():
            run = mock.AsyncMock(return_value={"ok": True, "blockers": []})
            with mock.patch.object(preflight, "run", run):
                await preflight.gate(None, self.inst, "probe", "v2.0.0")
                self.rec["stored"] = "second"
                await preflight.gate(None, self.inst, "probe", "v2.0.0")
            self.assertEqual(run.await_count, 2)
        asyncio.run(check())

    def test_same_second_reinstall_during_check_blocks_stale_start(self):
        async def run(*args, **kwargs):
            self.rec["stored"] = "second"
            return {"ok": True, "blockers": []}
        with mock.patch.object(preflight, "run", run):
            result = asyncio.run(preflight.gate(None, self.inst, "probe", "v2.0.0"))
        self.assertTrue(result["blocked"])
        self.assertIn("installed again", result["report"]["blockers"][0])
