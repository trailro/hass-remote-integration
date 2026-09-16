"""Review round 12: patches, install, dev mode, stop budget."""

import difflib
import errno
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import asyncio

import run
from custom_components.integration_manager import build_views, views
from custom_components.integration_manager import installer as installer_mod
from custom_components.integration_manager import patches
from custom_components.integration_manager.installer import Installer

TWIN = ["# twin", "def twin():", "    x = 0", "    return 1", "    y = 0", "# end", "pass"]
INSERTED = [f"added_{i} = {i}" for i in range(60)]


def _source():
    lines = [f"line_{i} = {i}" for i in range(1, 161)]
    lines[45:52] = TWIN  # lines 46-52
    lines[95:102] = TWIN  # lines 96-102, identical
    return lines


def _second_twin_fixed(lines, at=95):
    out = list(lines)
    out[at + 3] = "    return 2"
    return out


def _diff(old, new):
    return "\n".join(difflib.unified_diff(old, new, "a/mod.py", "b/mod.py", lineterm="", n=3)) + "\n"


class R12PatchCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="hri-r12-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.comp = os.path.join(self.root, "custom_components", "demo")
        self.site = os.path.join(self.root, "site-packages")
        os.makedirs(self.comp)
        os.makedirs(self.site)
        self.ctx = patches.PatchContext(self.root, "demo", self.site, self.comp)
        self.path = os.path.join(self.comp, "mod.py")

    def write(self, lines):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def read(self):
        with open(self.path, encoding="utf-8") as fh:
            return fh.read().split("\n")[:-1]

    def hunks(self, diff):
        report = patches.check(self.root, "demo", self.site, self.comp, "1.0.0", "fix.patch", diff)
        return report["status"], [(h["state"], h["line"]) for h in report["files"][0]["hunks"]]


class DiffOffsetTest(R12PatchCase):
    """M1: a later hunk is located with the offset of the earlier ones, like GNU patch."""

    def two_hunks(self):
        old = _source()
        new = _second_twin_fixed(old)
        new[8:8] = INSERTED  # 60 lines before line 9
        diff = _diff(old, new)
        self.assertEqual(len(patches.parse_unified(diff)[0].hunks), 2)
        return old, new, diff

    def test_second_hunk_lands_on_its_own_twin(self):
        old, new, diff = self.two_hunks()
        self.write(old)
        self.assertEqual(patches._diff_status(diff, self.ctx), "pending")
        self.assertEqual(self.hunks(diff), ("pending", [("pending", 6), ("pending", 96)]))
        self.assertEqual(patches._diff_apply(diff, self.ctx), "applied")
        self.assertEqual(self.read(), new)
        self.assertEqual(self.read()[45 + 60 + 3], "    return 1")  # the first twin is untouched
        self.assertEqual(patches._diff_status(diff, self.ctx), "applied")
        self.assertEqual(self.hunks(diff), ("applied", [("applied", 6), ("applied", 156)]))
        self.assertEqual(patches._diff_apply(diff, self.ctx), "already applied")

    def test_half_applied_file(self):
        old, new, diff = self.two_hunks()
        half = list(old)
        half[8:8] = INSERTED  # only the first hunk is in the file
        self.write(half)
        self.assertEqual(patches._diff_status(diff, self.ctx), "pending")
        self.assertEqual(self.hunks(diff), ("pending", [("applied", 6), ("pending", 156)]))
        self.assertEqual(patches._diff_apply(diff, self.ctx), "applied")
        self.assertEqual(self.read(), new)

    def test_drifted_file_still_finds_the_nearer_twin(self):
        old = _source()
        diff = _diff(old, _second_twin_fixed(old))
        drifted = [f"top_{i} = {i}" for i in range(5)] + old
        self.write(drifted)
        self.assertEqual(patches._diff_apply(diff, self.ctx), "applied")
        self.assertEqual(self.read(), _second_twin_fixed(drifted, 100))

    def test_twins_equally_far_are_ambiguous(self):
        old = _source()
        diff = _diff(old, _second_twin_fixed(old))
        drifted = [f"top_{i} = {i}" for i in range(25)] + old  # twins at 71 and 121, the hunk says 96
        self.write(drifted)
        self.assertEqual(patches._diff_status(diff, self.ctx), "not applicable")
        self.assertEqual(self.hunks(diff), ("not applicable", [("ambiguous", None)]))
        self.assertEqual(patches._diff_apply(diff, self.ctx), "not applicable")
        self.assertEqual(self.read(), drifted)


class R12InstallerCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hri-r12-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        os.makedirs(os.path.join(self.dir, "integration_manager"))
        self.cc = os.path.join(self.dir, "custom_components")

    def installer(self, state=None):
        if state is not None:
            with open(os.path.join(self.dir, "integration_manager", "state.json"), "w", encoding="utf-8") as fh:
                json.dump(state, fh)
        return Installer(SimpleNamespace(config=SimpleNamespace(config_dir=self.dir)))

    def store(self, inst, domain, tag, version="1.0.0"):
        src = inst._version_dir(domain, tag)
        os.makedirs(src, exist_ok=True)
        for name, text in (("manifest.json", json.dumps({"domain": domain, "version": version})),
                           ("__init__.py", "X = 1\n"), ("sensor.py", "Y = 2\n")):
            with open(os.path.join(src, name), "w", encoding="utf-8") as fh:
                fh.write(text)
        return src


class DeployLeftoverTest(R12InstallerCase):
    """M2: a copy that fails half-way leaves no second directory with the domain's manifest in custom_components."""

    def test_disk_full_during_the_copy(self):
        inst = self.installer()
        self.store(inst, "demo", "1.0.0")
        self.store(inst, "demo", "2.0.0", version="2.0.0")
        inst._deploy("demo", "1.0.0")
        real = shutil.copytree

        def disk_full(src, dst, *a, **kw):
            def copy(s, d, **k):
                if os.path.basename(s) != "manifest.json":
                    raise OSError(errno.ENOSPC, "No space left on device", d)
                return shutil.copy2(s, d, **k)
            return real(src, dst, *a, copy_function=copy, **kw)

        with mock.patch.object(installer_mod.shutil, "copytree", disk_full):
            with self.assertRaises(OSError):
                inst._deploy("demo", "2.0.0")
        self.assertEqual(os.listdir(self.cc), ["demo"])
        self.assertEqual(inst.installed_manifest("demo")["version"], "1.0.0")


def _request(body):
    return SimpleNamespace(headers={}, query={}, content_type="application/json", json=mock.AsyncMock(return_value=body))


class ManagerDomainTest(R12InstallerCase):
    """m10: integration_manager is never a managed integration."""

    def test_registry(self):
        inst = self.installer()
        with self.assertRaisesRegex(ValueError, "this manager itself"):
            inst.add_to_registry("integration_manager", "owner/repo")
        res = json.loads(asyncio.run(views.RegistryView(inst).post(_request({"domain": "Integration_Manager", "repo": "owner/repo"}))).body)
        self.assertFalse(res["ok"])
        self.assertIn("this manager itself", res["error"])
        self.assertFalse(os.path.exists(inst.user_registry_file))
        # a hand-edited registry entry is ignored
        with open(inst.user_registry_file, "w", encoding="utf-8") as fh:
            json.dump({"integrations": {"integration_manager": {"repo": "owner/repo"}, "demo": {"repo": "owner/demo"}}}, fh)
        with self.assertLogs(installer_mod._LOGGER, "WARNING"):
            registry = inst.registry()
        self.assertIn("demo", registry)
        self.assertNotIn("integration_manager", registry)

    def test_build_check_refuses_and_registers_nothing(self):
        inst = self.installer()
        check = object.__new__(build_views.BuildCheckView)
        check.hass, check.installer, check.updater, check._checks = None, inst, None, {}
        with self.assertRaisesRegex(ValueError, "this manager itself"):
            asyncio.run(check._resolve({"domain": "integration_manager", "repo": "owner/repo", "ref": "main"}))
        self.assertFalse(os.path.exists(inst.user_registry_file))

    def test_install_start_uninstall_install_local(self):
        tag = "1.0.0"
        inst = self.installer({"domain": "integration_manager", "installed": {
            "integration_manager": {"versions": {tag: {"requirements": []}}, "running_tag": tag}}})
        self.store(inst, "integration_manager", tag)
        os.makedirs(os.path.join(self.cc, "integration_manager"))
        with open(inst.user_registry_file, "w", encoding="utf-8") as fh:
            json.dump({"integrations": {"integration_manager": {"repo": "owner/repo"}}}, fh)
        for call in (lambda: inst.install(tag, domain="integration_manager"),
                     lambda: inst.install_local("integration_manager"),
                     lambda: inst.start("integration_manager", tag),
                     lambda: inst.uninstall("integration_manager")):
            res = asyncio.run(call())
            self.assertFalse(res["ok"])
            self.assertIn("this manager itself", res["error"])
        self.assertFalse(inst.busy)
        self.assertTrue(os.path.isdir(os.path.join(self.cc, "integration_manager")))

    def test_dev_candidates_leave_the_manager_out(self):
        inst = self.installer()
        src = os.path.join(self.dir, "src")
        for domain in ("integration_manager", "demo"):
            os.makedirs(os.path.join(src, "custom_components", domain))
            with open(os.path.join(src, "custom_components", domain, "manifest.json"), "w", encoding="utf-8") as fh:
                json.dump({"domain": domain, "version": "1.0.0"}, fh)
        with mock.patch.dict(inst.settings.data, {"dev_source_dir": src}):
            self.assertEqual([c["domain"] for c in inst.dev_candidates()["candidates"]], ["demo"])


class BootSweepTest(unittest.TestCase):
    """M2: a deploy killed half-way is cleaned before Home Assistant scans custom_components."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hri-r12-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.cc = os.path.join(self.dir, "custom_components")

    def mkcomp(self, name, version):
        os.makedirs(os.path.join(self.cc, name))
        with open(os.path.join(self.cc, name, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"domain": "demo", "version": version}, fh)

    def sweep(self):
        with mock.patch.object(run, "CONFIG_DIR", self.dir):
            run._sweep_deploy_leftovers()

    def version(self, name):
        with open(os.path.join(self.cc, name, "manifest.json"), encoding="utf-8") as fh:
            return json.load(fh)["version"]

    def test_killed_during_the_copy(self):
        self.mkcomp("demo", "1.0.0")
        self.mkcomp("demo.deploying", "2.0.0")
        self.mkcomp("demo.replaced", "0.9.0")
        self.mkcomp("other", "1.0.0")
        self.sweep()
        self.assertEqual(sorted(os.listdir(self.cc)), ["demo", "other"])
        self.assertEqual(self.version("demo"), "1.0.0")

    def test_killed_between_the_two_renames(self):
        self.mkcomp("demo.deploying", "2.0.0")
        self.mkcomp("demo.replaced", "1.0.0")
        self.sweep()
        self.assertEqual(os.listdir(self.cc), ["demo"])
        self.assertEqual(self.version("demo"), "1.0.0")  # the copy that ran; the reconcile deploys the new one again

    def test_no_custom_components_yet(self):
        self.sweep()
        self.assertFalse(os.path.exists(self.cc))


class StopWatchdogBudgetTest(unittest.TestCase):
    """m6: every drain the watchdog runs counts against Docker's 240 s stop_grace_period."""

    def test_budget_counts_every_drain(self):
        from homeassistant.core import STOPPING_STAGE_SHUTDOWN_TIMEOUT

        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docker-compose.yml"),
                  encoding="utf-8") as fh:
            self.assertIn("stop_grace_period: 240s", fh.read())
        drain_s, flush_s = 0.3, 0.3

        def stuck(timeout):  # like writer.drain / events.drain / flush_queue when nothing gets written: waits it all
            threading.Event().wait(timeout)
            return False

        slept = []
        with mock.patch.object(run, "_stop_watchdog", None), mock.patch.object(run.threading, "Thread") as thread:
            run._arm_stop_watchdog(run.STOP_WATCHDOG_S)
        watch = thread.call_args.kwargs["target"]
        fakes = {"custom_components.integration_manager.writer": SimpleNamespace(drain=stuck),
                 "custom_components.integration_manager.events": SimpleNamespace(drain=stuck)}
        with mock.patch.dict(sys.modules, fakes), mock.patch.object(run, "WATCHDOG_DRAIN_S", drain_s), \
                mock.patch.object(run, "LOG_FLUSH_S", flush_s), mock.patch.object(run.time, "sleep", slept.append), \
                mock.patch.object(run.logbuffer, "flush_queue", stuck), mock.patch.object(run.logbuffer, "find", return_value=None), \
                mock.patch.object(run.os, "_exit", side_effect=SystemExit), self.assertLogs(run._LOGGER, "CRITICAL"):
            start = time.monotonic()
            with self.assertRaises(SystemExit):
                watch()
            spent = time.monotonic() - start
        self.assertEqual(slept, [run.STOP_WATCHDOG_S])
        self.assertLess(spent, drain_s + flush_s + 0.15)  # the drains share WATCHDOG_DRAIN_S, the log gets LOG_FLUSH_S
        self.assertLess(STOPPING_STAGE_SHUTDOWN_TIMEOUT + run.STOP_WATCHDOG_S + run.WATCHDOG_DRAIN_S + run.LOG_FLUSH_S, 240)


if __name__ == "__main__":
    unittest.main()
