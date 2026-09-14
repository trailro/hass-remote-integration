"""patches: unified diffs, modules and the dry-run check."""

import os
import shutil
import tempfile
import textwrap
import unittest

from custom_components.integration_manager import patches

DIFF = "--- a/mod.py\n+++ b/mod.py\n@@ -1,3 +1,3 @@\n a = 1\n-b = 2\n+b = 3\n c = 4\n"


class ParseTest(unittest.TestCase):
    def test_trailing_blank_lines(self):
        files = patches.parse_unified(DIFF + "\n\n\n")
        self.assertEqual(len(files), 1)
        h = files[0].hunks[0]
        self.assertEqual(h.old_lines, ["a = 1", "b = 2", "c = 4"])
        self.assertEqual(h.new_lines, ["a = 1", "b = 3", "c = 4"])

    def test_two_files_separated_by_a_blank_line(self):
        text = DIFF + "\n--- a/two.py\n+++ b/two.py\n@@ -1 +1 @@\n-z\n+Z\n"
        files = patches.parse_unified(text)
        self.assertEqual([f.path for f in files], ["b/mod.py", "b/two.py"])
        self.assertEqual((files[1].hunks[0].old_lines, files[1].hunks[0].new_lines), (["z"], ["Z"]))

    def test_blank_context_line_inside_a_hunk(self):
        h = patches.parse_unified("--- a/m.py\n+++ b/m.py\n@@ -1,3 +1,3 @@\n a\n\n-b\n+B\n")[0].hunks[0]
        self.assertEqual(h.old_lines, ["a", "", "b"])

    def test_not_a_diff(self):
        self.assertEqual(patches.parse_unified("hello\n"), [])


class PatchTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="hri-unit-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.comp = os.path.join(self.root, "custom_components", "demo")
        self.site = os.path.join(self.root, "site-packages")
        os.makedirs(self.comp)
        os.makedirs(self.site)
        self.ctx = patches.PatchContext(self.root, "demo", self.site, self.comp)
        self.write("mod.py", "a = 1\nb = 2\nc = 4\n")

    def write(self, name, text):
        with open(os.path.join(self.comp, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def read(self, name):
        with open(os.path.join(self.comp, name), encoding="utf-8") as fh:
            return fh.read()

    def check(self, name, text, running_tag="1.0.0"):
        return patches.check(self.root, "demo", self.site, self.comp, running_tag, name, text)


class DiffTest(PatchTestCase):
    def test_pending_applied_already_applied(self):
        self.assertEqual(patches._diff_status(DIFF, self.ctx), "pending")
        self.assertEqual(patches._diff_apply(DIFF, self.ctx), "applied")
        self.assertEqual(self.read("mod.py"), "a = 1\nb = 3\nc = 4\n")
        self.assertEqual(patches._diff_status(DIFF, self.ctx), "applied")
        self.assertEqual(patches._diff_apply(DIFF, self.ctx), "already applied")

    def test_context_changed(self):
        self.write("mod.py", "a = 1\nb = 22\nc = 4\n")
        self.assertEqual(patches._diff_status(DIFF, self.ctx), "not applicable")
        self.assertEqual(patches._diff_apply(DIFF, self.ctx), "not applicable")
        self.assertEqual(self.read("mod.py"), "a = 1\nb = 22\nc = 4\n")

    def test_absent(self):
        self.assertEqual(patches._diff_status(DIFF.replace("mod.py", "gone.py"), self.ctx), "absent (b/gone.py not found)")

    def test_broken_python_is_not_written(self):
        with self.assertRaises(SyntaxError):
            patches._diff_apply(DIFF.replace("+b = 3", "+b = ("), self.ctx)
        self.assertEqual(self.read("mod.py"), "a = 1\nb = 2\nc = 4\n")


class CheckTest(PatchTestCase):
    def test_pending_then_applied(self):
        out = self.check("fix.patch", DIFF)
        self.assertEqual((out["ok"], out["status"], out["applies"]), (True, "pending", True))
        self.assertEqual(out["files"][0]["target"], "custom_components/demo/mod.py")
        self.assertEqual(out["files"][0]["hunks"][0]["state"], "pending")
        self.assertEqual(out["files"][0]["hunks"][0]["line"], 1)
        patches._diff_apply(DIFF, self.ctx)
        out = self.check("fix.patch", DIFF)
        self.assertEqual((out["status"], out["files"][0]["hunks"][0]["state"]), ("applied", "applied"))

    def test_not_applicable_reports_the_difference(self):
        self.write("mod.py", "x = 0\na = 1\nb = 22\nc = 4\n")
        out = self.check("fix.patch", DIFF)
        self.assertEqual(out["status"], "not applicable")
        hunk = out["files"][0]["hunks"][0]
        self.assertEqual((hunk["state"], hunk["line"]), ("not applicable", 2))
        self.assertIn("-b = 2", hunk["found_diff"])
        self.assertIn("+b = 22", hunk["found_diff"])

    def test_scoped(self):
        out = self.check("fix.patch", "# integration-version: 2.0.0, 2.0.1\n" + DIFF)
        self.assertEqual(out["scope"], ["2.0.0", "2.0.1"])
        self.assertFalse(out["applies"])
        self.assertEqual((out["status"], out["status_if_applied"]), ("skipped", "pending"))
        self.assertIn("running 1.0.0", out["detail"])

    def test_module_with_future_annotations_and_dataclass(self):
        text = textwrap.dedent("""\
            from __future__ import annotations
            from dataclasses import dataclass


            @dataclass
            class Target:
                path: str
                marker: str = "b = 3"


            def status(ctx) -> str:
                t = Target(ctx.component_dir + "/mod.py")
                with open(t.path) as fh:
                    return "applied" if t.marker in fh.read() else "pending"


            def apply(ctx) -> str:
                return "applied"
            """)
        self.assertEqual(self.check("fix.py", text)["status"], "pending")

    def test_module_raising(self):
        text = "def apply(ctx):\n    return 'applied'\n\ndef status(ctx):\n    raise RuntimeError('boom')\n"
        out = self.check("fix.py", text)
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], "error: RuntimeError: boom")

    def test_invalid(self):
        self.assertEqual(self.check("fix.txt", DIFF), {"ok": False, "error": "file must be <name>.py or <name>.patch"})

    def test_validate(self):
        self.assertIsNone(patches.validate("fix.patch", DIFF))
        self.assertIsNone(patches.validate("fix.py", "def apply(ctx): pass\ndef status(ctx): pass\n"))
        self.assertIn("not valid Python", patches.validate("fix.py", "def apply(:\n"))
        self.assertIn("must define", patches.validate("fix.py", "def apply(ctx): pass\n"))
        self.assertIn("no hunks", patches.validate("fix.patch", "just text\n"))
        self.assertIsNotNone(patches.validate("../fix.py", "def apply(ctx): pass\ndef status(ctx): pass\n"))
        self.assertIsNotNone(patches.validate(".fix.patch", DIFF))


class ClosestTest(unittest.TestCase):
    def test_tie_goes_to_the_nearest(self):
        lines = ["a", "b", "c", "x", "b", "c"]
        self.assertEqual(patches._closest(lines, ["q", "b", "c"], 3), 3)
        self.assertEqual(patches._closest(lines, ["q", "b", "c"], 0), 0)

    def test_best_window_beats_a_nearer_one(self):
        self.assertEqual(patches._closest(["a", "b", "z", "a", "b", "c"], ["a", "b", "c"], 0), 3)

    def test_needle_longer_than_lines(self):
        self.assertEqual(patches._closest(["a"], ["a", "b"], 5), 0)


if __name__ == "__main__":
    unittest.main()
