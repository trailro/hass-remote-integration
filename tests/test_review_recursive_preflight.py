"""Preflight and start resolve the same guarded recursive dependency closure."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import installer as inst_mod, preflight
from tests.test_camp_preflight import _hass, _installer


def integration(requirements=(), dependencies=(), after_dependencies=()):
    return SimpleNamespace(requirements=list(requirements), dependencies=list(dependencies),
                           after_dependencies=list(after_dependencies))


class RecursivePreflightTest(unittest.TestCase):
    def fixture(self, dependencies=("direct",), after=()):
        manifest = {"domain": "demo", "version": "2.0", "config_flow": True,
                    "requirements": ["rootpkg==1"], "dependencies": list(dependencies), "after_dependencies": list(after)}
        inst = _installer(self, {"__init__.py": ""}, manifest)
        inst.hass = _hass()
        inst.installed_manifest = lambda domain=None: manifest
        return inst, manifest

    def report(self, inst):
        return preflight.run(_hass(), inst, "demo", "2.0", source_dir=inst._version_dir("demo", "2.0"))

    def test_nested_requirements_cycles_shared_children_and_root_are_guarded(self):
        inst, _ = self.fixture()
        graph = {"direct": integration(["directpkg==1"], ["nested"], ["shared"]),
                 "nested": integration(["nestedpkg==9"], ["direct", "demo", "shared"]),
                 "shared": integration(["sharedpkg==1"])}
        async def load(hass, domain):
            return graph[domain]
        loader = mock.AsyncMock(side_effect=load)
        pip = {"ok": False, "install": [], "stderr": "nestedpkg is incompatible"}
        async def check():
            with mock.patch.object(inst_mod.loader, "async_get_integration", loader), \
                    mock.patch.object(inst, "_requirement_versions", return_value={}), \
                    mock.patch.object(preflight, "_pip_dry_run", return_value=pip) as resolve:
                report = await self.report(inst)
                checked = resolve.call_args.args[1]
                self.assertEqual(set(checked), {"rootpkg==1", "directpkg==1", "nestedpkg==9", "sharedpkg==1"})
                self.assertEqual(len(checked), 4)
                self.assertFalse(report["ok"])
                self.assertIn("nestedpkg", " ".join(report["blockers"]))
                self.assertEqual(set(checked), set(await inst._requirements_for("demo")))
            # Each traversal loads three domains once; never loads the candidate itself.
            self.assertEqual([c.args[1] for c in loader.await_args_list].count("shared"), 2)
            self.assertEqual(loader.await_count, 6)
        asyncio.run(check())

    def test_nested_missing_hard_dependency_blocks_optional_missing_only_warns(self):
        inst, _ = self.fixture(after=("optional", "missing"))
        graph = {"direct": integration(dependencies=["missing"]),
                 "optional": integration(dependencies=["optional_missing"])}
        async def load(hass, domain):
            if domain not in graph:
                raise inst_mod.loader.IntegrationNotFound(domain)
            return graph[domain]
        with mock.patch.object(inst_mod.loader, "async_get_integration", load), \
                mock.patch.object(inst, "_requirement_versions", return_value={}), \
                mock.patch.object(preflight, "_resolution_warnings", mock.AsyncMock(return_value=[])), \
                mock.patch.object(preflight, "_pip_dry_run", return_value={"ok": True, "install": [], "stderr": ""}):
            report = asyncio.run(self.report(inst))
        self.assertIn("dependency 'missing'", " ".join(report["blockers"]))
        self.assertNotIn("optional_missing", " ".join(report["blockers"]))
        self.assertIn("after_dependency 'optional_missing'", " ".join(report["warnings"]))
        self.assertEqual([r["domain"] for r in report["dependencies"]].count("missing"), 1)

    def test_hard_path_upgrades_already_walked_optional_descendants_without_reloading(self):
        manifest = {"dependencies": ["hard_parent"], "after_dependencies": ["optional_parent"]}
        graph = {"hard_parent": integration(dependencies=["shared"]),
                 "optional_parent": integration(dependencies=["shared"]),
                 "shared": integration(dependencies=["missing"])}
        async def load(hass, domain):
            if domain not in graph:
                raise inst_mod.loader.IntegrationNotFound(domain)
            return graph[domain]
        loader = mock.AsyncMock(side_effect=load)
        with mock.patch.object(inst_mod.loader, "async_get_integration", loader):
            rows = asyncio.run(inst_mod._dependency_rows(_hass(), "demo", manifest))
        by_domain = {row["domain"]: row for row in rows}
        self.assertTrue(by_domain["shared"]["required"])
        self.assertTrue(by_domain["missing"]["required"])
        self.assertEqual([call.args[1] for call in loader.await_args_list].count("shared"), 1)
        self.assertEqual([call.args[1] for call in loader.await_args_list].count("missing"), 1)

    def test_cancellation_during_nested_loader_never_reaches_pip(self):
        inst, _ = self.fixture()
        async def load(hass, domain):
            if domain == "direct":
                return integration(dependencies=["nested"])
            raise asyncio.CancelledError()
        with mock.patch.object(inst_mod.loader, "async_get_integration", load), \
                mock.patch.object(preflight, "_pip_dry_run") as pip:
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(self.report(inst))
        pip.assert_not_called()

    def test_nested_unsafe_requirement_blocks_and_is_never_handed_to_pip(self):
        inst, _ = self.fixture()
        unsafe = "--index-url=https://example.invalid"
        async def load(hass, domain):
            return integration(dependencies=["nested"]) if domain == "direct" else integration([unsafe])
        with mock.patch.object(inst_mod.loader, "async_get_integration", load), \
                mock.patch.object(inst, "_requirement_versions", return_value={}), \
                mock.patch.object(preflight, "_resolution_warnings", mock.AsyncMock(return_value=[])), \
                mock.patch.object(preflight, "_pip_dry_run", return_value={"ok": True, "install": [], "stderr": ""}) as pip:
            report = asyncio.run(self.report(inst))
        self.assertFalse(report["ok"])
        self.assertNotIn(unsafe, pip.call_args.args[1])
        self.assertTrue(any(inst_mod.bad_requirement(unsafe) == reason for reason in report["blockers"]))
