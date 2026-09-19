"""A patch scoped to one version, still sitting in an installed library after the switch.

R2/F-07: `# integration-version:` makes a patch apply to the versions it lists, and the status page
said "skipped" for every other version.  That is the whole truth for a patch that edits the
integration's own files, because a version change redeploys those.  It is not true of a patch that
edits an installed library: pip reinstalls nothing when the requirement pin did not change, so the
library keeps the patch while the page says it is not applied.
"""

import os
import shutil
import tempfile
import unittest

from custom_components.integration_manager import patches

LIB_DIFF = ("--- a/lib/thing.py\n+++ b/lib/thing.py\n@@ -1,3 +1,3 @@\n"
            " value = 1\n-limit = 30\n+limit = 60\n end = 2\n")
SCOPED = "# integration-version: 1.0.0\n" + LIB_DIFF


class ScopedPatchLeftBehindTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="hri-r2-patches-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.comp = os.path.join(self.root, "custom_components", "demo")
        self.site = os.path.join(self.root, "site-packages")
        os.makedirs(os.path.join(self.site, "lib"))
        os.makedirs(self.comp)
        self.ctx = patches.PatchContext(self.root, "demo", self.site, self.comp)
        self.lib = os.path.join(self.site, "lib", "thing.py")
        self._write(self.lib, "value = 1\nlimit = 30\nend = 2\n")
        patch_dir = os.path.join(self.root, "integration_manager", "patches", "demo")
        os.makedirs(patch_dir)
        self._write(os.path.join(patch_dir, "limit.patch"), SCOPED)

    @staticmethod
    def _write(path, text):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def rows(self, running_tag):
        return patches.status(self.root, "demo", self.site, self.comp, running_tag)

    def test_in_scope_it_is_the_ordinary_pending_patch(self):
        row = self.rows("1.0.0")[0]
        self.assertEqual(row["status"], "pending")

    def test_out_of_scope_and_never_applied_is_just_skipped(self):
        row = self.rows("1.1.0")[0]
        self.assertEqual(row["status"], "skipped")
        self.assertIn("scoped to version(s) 1.0.0", row["detail"])

    def test_out_of_scope_but_still_in_the_library_says_so(self):
        patches.apply_all(self.root, "demo", self.site, self.comp, "1.0.0")
        with open(self.lib, encoding="utf-8") as fh:
            self.assertIn("limit = 60", fh.read())
        row = self.rows("1.1.0")[0]  # the switch happened; pip reinstalled nothing
        self.assertEqual(row["status"], "skipped, still applied")
        self.assertIn("reinstall that distribution", row["detail"])

    def test_the_summary_no_longer_calls_that_fine(self):
        """installer._patch_summary treats anything but applied/skipped as the problem to report."""
        from custom_components.integration_manager.installer import Installer

        patches.apply_all(self.root, "demo", self.site, self.comp, "1.0.0")
        rows = self.rows("1.1.0")
        summary = Installer._patch_summary(None, rows)
        self.assertIn("limit.patch", summary)
        self.assertIn("still applied", summary)

    def test_a_patch_of_the_integrations_own_files_is_not_flagged(self):
        """Those are redeployed by the version change, so "skipped" remains the whole story."""
        own = os.path.join(self.comp, "mod.py")
        self._write(own, "a = 1\nb = 2\nc = 4\n")
        patch_dir = os.path.join(self.root, "integration_manager", "patches", "demo")
        self._write(os.path.join(patch_dir, "limit.patch"),
                    "# integration-version: 1.0.0\n--- a/mod.py\n+++ b/mod.py\n@@ -1,3 +1,3 @@\n a = 1\n-b = 2\n+b = 3\n c = 4\n")
        patches.apply_all(self.root, "demo", self.site, self.comp, "1.0.0")
        self._write(own, "a = 1\nb = 2\nc = 4\n")  # the version change put the file back
        self.assertEqual(self.rows("1.1.0")[0]["status"], "skipped")
