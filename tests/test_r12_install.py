"""Review round 12: patches, install, dev mode, stop budget."""

import difflib
import errno
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import run
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


if __name__ == "__main__":
    unittest.main()
