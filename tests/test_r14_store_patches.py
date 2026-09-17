"""Review round 14, store: a patched block that also exists as a twin is applied, not ambiguous."""

import difflib
import os
import shutil
import tempfile
import unittest

from custom_components.integration_manager import patches

TWIN = ["# twin", "def twin():", "    x = 0", "    return 1", "    y = 0", "# end", "pass"]
FIXED = ["# twin", "def twin():", "    x = 0", "    return 2", "    y = 0", "# end", "pass"]


def _base():
    """TWIN at index 45 (the hunk's target) and, 10 lines further, a copy of the fixed shape."""
    lines = [f"line_{i} = {i}" for i in range(1, 161)]
    lines[45:52] = TWIN
    lines[55:62] = FIXED
    return lines


def _fixed(lines, at):
    out = list(lines)
    out[at:at + 7] = FIXED
    return out


def _diff(old, new):
    return "\n".join(difflib.unified_diff(old, new, "a/mod.py", "b/mod.py", lineterm="", n=3)) + "\n"


class TwinOfThePatchedBlockTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="hri-r14-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.comp = os.path.join(self.root, "custom_components", "demo")
        self.site = os.path.join(self.root, "site-packages")
        os.makedirs(self.comp)
        os.makedirs(self.site)
        self.ctx = patches.PatchContext(self.root, "demo", self.site, self.comp)
        self.path = os.path.join(self.comp, "mod.py")
        base = _base()
        self.diff = _diff(base, _fixed(base, 45))
        hunk = patches.parse_unified(self.diff)[0].hunks[0]
        self.assertEqual((hunk.old_start, hunk.old_lines, hunk.new_lines), (46, TWIN, FIXED))

    def write(self, lines):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def read(self):
        with open(self.path, encoding="utf-8") as fh:
            return fh.read().split("\n")[:-1]

    def check(self):
        report = patches.check(self.root, "demo", self.site, self.comp, "1.0.0", "fix.patch", self.diff)
        return report["status"], [(h["state"], h["line"]) for h in report["files"][0]["hunks"]]

    def rows(self):
        os.makedirs(patches.patch_dir(self.root, "demo"), exist_ok=True)
        with open(os.path.join(patches.patch_dir(self.root, "demo"), "fix.patch"), "w", encoding="utf-8") as fh:
            fh.write(self.diff)
        return [r["status"] for r in patches.status(self.root, "demo", self.site, self.comp, "1.0.0")]

    def test_drifted_patched_file_with_a_twin_is_applied(self):
        drifted = [f"top_{i} = {i}" for i in range(12)] + _base()  # the target at 57, the twin at 67, the hunk says 45
        self.write(drifted)
        self.assertEqual(patches._diff_status(self.diff, self.ctx), "pending")
        self.assertEqual(patches._diff_apply(self.diff, self.ctx), "applied")
        self.assertEqual(self.read(), _fixed(drifted, 57))
        self.assertEqual(patches._diff_status(self.diff, self.ctx), "applied")
        self.assertEqual(self.check(), ("applied", [("applied", 58)]))
        self.assertEqual(self.rows(), ["applied"])
        self.assertEqual(patches._diff_apply(self.diff, self.ctx), "already applied")
        self.assertEqual(self.read(), _fixed(drifted, 57))

    def test_an_unpatched_copy_near_the_patched_twins_stays_ambiguous(self):
        lines = [f"line_{i} = {i}" for i in range(1, 161)]
        lines[65:72] = FIXED  # 20 lines from the hunk
        lines[75:82] = FIXED  # 30
        lines[85:92] = TWIN  # 40: still no further than twice the nearest fixed copy
        self.write(lines)
        self.assertEqual(patches._diff_status(self.diff, self.ctx), "not applicable")
        self.assertEqual(self.check(), ("not applicable", [("ambiguous", None)]))
        self.assertEqual(self.rows(), ["not applicable"])
        self.assertEqual(patches._diff_apply(self.diff, self.ctx), "not applicable")
        self.assertEqual(self.read(), lines)

    def test_an_unpatched_copy_far_away_does_not_matter(self):
        lines = [f"line_{i} = {i}" for i in range(1, 161)]
        lines[65:72] = FIXED
        lines[75:82] = FIXED
        lines[125:132] = TWIN  # 80 lines away
        self.write(lines)
        self.assertEqual(patches._diff_status(self.diff, self.ctx), "applied")
        self.assertEqual(self.check(), ("applied", [("applied", 66)]))
        self.assertEqual(patches._diff_apply(self.diff, self.ctx), "already applied")

    def test_ambiguous_unpatched_twins_are_still_refused(self):
        lines = [f"line_{i} = {i}" for i in range(1, 161)]
        lines[65:72] = TWIN
        lines[75:82] = TWIN
        self.write(lines)
        self.assertEqual(patches._diff_status(self.diff, self.ctx), "not applicable")
        self.assertEqual(patches._diff_apply(self.diff, self.ctx), "not applicable")
        self.assertEqual(self.read(), lines)


if __name__ == "__main__":
    unittest.main()
