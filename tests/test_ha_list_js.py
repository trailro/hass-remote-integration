"""The System page's half of the Home Assistant version list, run for real under node
(tests/js/ha_list.mjs): what the selector offers by default, the mark each version carries, "show all
versions", and that painting the page never starts a check.

Needs node, which the container the unit tests run in does not have: it skips there and runs wherever node is
installed (a developer machine, CI)."""

import json
import os
import shutil
import subprocess
import unittest

# by path, not through an import of the component: this file must also run where Home Assistant is not installed
STATIC = os.path.join(os.path.dirname(__file__), os.pardir, "custom_components", "integration_manager", "static")
HARNESS = os.path.join(os.path.dirname(__file__), "js", "ha_list.mjs")


@unittest.skipUnless(shutil.which("node") and os.path.isfile(HARNESS), "node (or the harness) is not available here")
class SystemPageVersionListTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        out = subprocess.run([shutil.which("node"), HARNESS, STATIC], capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise AssertionError(f"the harness failed: {out.stderr.strip()}")
        cls.out = json.loads(out.stdout)

    def test_the_list_is_what_the_answer_offers_newest_first(self):
        shown = self.out["default"]["options"]
        self.assertEqual([o["value"] for o in shown],
                         ["2026.9.2", "2026.9.1", "2026.9.0", "2026.8.4", "2026.8.3", "2025.3.1"])
        self.assertEqual([o["value"] for o in shown if o["selected"]], ["2026.9.2"])  # the running version

    def test_an_old_venv_on_the_volume_is_in_the_list_and_says_why_it_would_be_refused(self):
        old = [o for o in self.out["default"]["options"] if o["value"] == "2025.3.1"][0]
        self.assertIn("✗", old["text"])  # far outside the newest ten, kept because this box has it

    def test_the_mark_comes_from_what_is_known_and_claims_nothing_otherwise(self):
        marks = {o["value"]: o["text"].replace(o["value"], "").strip() for o in self.out["default"]["options"]}
        self.assertEqual(marks["2026.9.0"], "✓")   # a cached report that resolved
        self.assertEqual(marks["2026.9.1"], "✗")   # a cached report that did not
        self.assertEqual(marks["2026.8.3"], "✓")   # installed on the volume
        self.assertEqual(marks["2026.8.4"], "")         # nobody has checked it

    def test_the_note_says_what_the_list_holds_and_what_it_leaves_out(self):
        self.assertEqual(self.out["default"]["note"],
                         "newest 10 of 1420 releases, plus what this box has installed, runs or has scheduled")

    def test_painting_the_page_starts_no_check(self):
        self.assertEqual(self.out["default"]["checks"], 0)
        self.assertEqual(self.out["all"]["checks"], 0)
        self.assertEqual(self.out["default"]["fetched"], ["api/ha"])

    def test_show_all_versions_asks_for_the_rest_and_loses_nothing(self):
        self.assertEqual(self.out["all"]["fetched"], ["api/ha", "api/ha?all=1"])
        self.assertEqual(self.out["all"]["options"],
                         ["2026.9.2", "2026.9.1", "2026.9.0", "2026.8.4", "2026.8.3", "2025.3.1", "2021.6.0", "2014.1.0"])
        self.assertEqual(self.out["all"]["note"], "all 8 releases")

    def test_a_refresh_keeps_show_all_and_the_selection(self):
        refresh = self.out["refresh"]
        self.assertEqual(refresh["url"], "api/ha?refresh=1&all=1")
        self.assertEqual((refresh["picked"], refresh["selected"]), ("2026.8.4", ["2026.8.4"]))

    def test_a_version_nobody_checked_is_marked_only_once_it_is_checked(self):
        checked = self.out["checked"]
        self.assertEqual(checked["before"], "2026.8.4")
        self.assertEqual(checked["after"], "2026.8.4 ✓")
        self.assertEqual(checked["sent"], [["api/ha/check", "2026.8.4"]])

    def test_a_version_below_the_baseline_is_refused_without_asking_the_server(self):
        baseline = self.out["baseline"]
        self.assertEqual(baseline["sent"], 0)  # arithmetic, not a pip run
        self.assertFalse(baseline["verdict"]["ok"])
        self.assertIn("older than this image's baseline 2026.8.3", baseline["painted"])


if __name__ == "__main__":
    unittest.main()
