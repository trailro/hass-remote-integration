"""Findings from an external review: clean start storage reset, stable-only MQTT update, patch safety, parity message size."""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import manager_device as md
from custom_components.integration_manager import parity
from custom_components.integration_manager import patches


def _ctx(root):
    comp = os.path.join(root, "comp")
    os.makedirs(comp, exist_ok=True)
    return patches.PatchContext(config_dir=root, domain="demo", site_packages=os.path.join(root, "site"), component_dir=comp)


def _write(ctx, name, text):
    with open(os.path.join(ctx.component_dir, name), "w", encoding="utf-8") as fh:
        fh.write(text)


def _read(ctx, name):
    with open(os.path.join(ctx.component_dir, name), encoding="utf-8") as fh:
        return fh.read()


class PatchSafetyTest(unittest.TestCase):
    def setUp(self):
        self.ctx = _ctx(tempfile.mkdtemp())

    def test_multi_file_patch_is_all_or_nothing(self):
        _write(self.ctx, "a.py", "x = 1\n")
        _write(self.ctx, "b.py", "y = 1\n")
        diff = ("--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-x = 1\n+x = 2\n"
                "--- a/b.py\n+++ b/b.py\n@@ -1,1 +1,1 @@\n-y = 1\n+y = (\n")
        with self.assertRaises(SyntaxError):
            patches._diff_apply(diff, self.ctx)
        self.assertEqual(_read(self.ctx, "a.py"), "x = 1\n")
        self.assertEqual(_read(self.ctx, "b.py"), "y = 1\n")
        self.assertEqual([n for n in os.listdir(self.ctx.component_dir) if n.endswith(".tmp")], [])

    def test_result_text_elsewhere_is_not_applied(self):
        src = "def other():\n    return True\n\n\ndef target():\n    return False\n"
        _write(self.ctx, "m.py", src)
        diff = "--- a/m.py\n+++ b/m.py\n@@ -6,1 +6,1 @@\n-    return False\n+    return True\n"
        self.assertEqual(patches._diff_status(diff, self.ctx), "pending")
        self.assertEqual(patches._diff_apply(diff, self.ctx), "applied")
        self.assertTrue(_read(self.ctx, "m.py").endswith("def target():\n    return True\n"))
        self.assertEqual(patches._diff_status(diff, self.ctx), "applied")
        self.assertEqual(patches._diff_apply(diff, self.ctx), "already applied")

    def test_truncated_hunk_is_rejected(self):
        _write(self.ctx, "t.py", "a = 1\nb = 2\nc = 3\n")
        diff = "--- a/t.py\n+++ b/t.py\n@@ -1,3 +1,3 @@\n-a = 1\n+a = 9\n"
        with self.assertRaises(ValueError):
            patches.parse_unified(diff)
        self.assertIn("truncated", patches.validate("t.patch", diff) or "")
        self.assertEqual(_read(self.ctx, "t.py"), "a = 1\nb = 2\nc = 3\n")

    def test_complete_hunk_still_applies(self):
        _write(self.ctx, "t.py", "a = 1\nb = 2\nc = 3\n")
        diff = "--- a/t.py\n+++ b/t.py\n@@ -1,3 +1,3 @@\n a = 1\n-b = 2\n+b = 9\n c = 3\n"
        self.assertIsNone(patches.validate("t.patch", diff))
        self.assertEqual(patches._diff_apply(diff, self.ctx), "applied")
        self.assertEqual(_read(self.ctx, "t.py"), "a = 1\nb = 9\nc = 3\n")


class StableOnlyUpdateTest(unittest.TestCase):
    def _device(self, versions, updates=None):
        inst = SimpleNamespace(running="demo", LOCAL_TAG="local", updates=updates or {},
                               state=SimpleNamespace(installed={"demo": {"versions": dict.fromkeys(versions, {})}}))
        dev = md.ManagerDevice.__new__(md.ManagerDevice)
        dev.installer = inst
        return dev

    def test_beta_branch_and_sha_are_not_updates(self):
        dev = self._device(["v1.2.0", "v1.2.0b1", "feature/999", "1.3.0rc1", "a1b2c3d"])
        self.assertEqual(dev.integration_latest(), "v1.2.0")

    def test_known_stable_update_wins(self):
        dev = self._device(["v1.2.0", "feature/999"], {"demo": "v1.3.0"})
        self.assertEqual(dev.integration_latest(), "v1.3.0")


class ParityMessageSizeTest(unittest.TestCase):
    def test_limit_above_a_large_entity_registry(self):
        self.assertGreater(parity.WS_MAX_MSG, 4.57 * 1024 * 1024 * 10)


if __name__ == "__main__":
    unittest.main()
