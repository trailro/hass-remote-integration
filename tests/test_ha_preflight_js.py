"""The System page's half of the Home Assistant dependency check, run for real under node
(tests/js/ha_preflight.mjs): the verdict it paints, the per-version cache, and the force path.

Needs node, which the container the unit tests run in does not have: it skips there and runs wherever node is
installed (a developer machine, CI)."""

import json
import os
import shutil
import subprocess
import unittest

# by path, not through an import of the component: this file must also run where Home Assistant is not installed
STATIC = os.path.join(os.path.dirname(__file__), os.pardir, "custom_components", "integration_manager", "static")
HARNESS = os.path.join(os.path.dirname(__file__), "js", "ha_preflight.mjs")


@unittest.skipUnless(shutil.which("node") and os.path.isfile(HARNESS), "node (or the harness) is not available here")
class SystemPageCheckTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        out = subprocess.run([shutil.which("node"), HARNESS, STATIC], capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise AssertionError(f"the harness failed: {out.stderr.strip()}")
        cls.out = json.loads(out.stdout)

    def test_a_blocked_version_reads_as_unlikely_and_names_the_package(self):
        blocked = self.out["verdicts"]["shown"][0]
        self.assertIn("unlikely to install", blocked["text"])
        self.assertIn("lru-dict==1.3.0", blocked["text"])
        self.assertEqual(blocked["classes"], ["bad"])

    def test_a_clean_version_says_so(self):
        clean = self.out["verdicts"]["shown"][1]
        self.assertIn("installs here", clean["text"])
        self.assertEqual(clean["classes"][0], "ok")

    def test_could_not_check_is_neither_green_nor_red(self):
        unchecked = self.out["verdicts"]["shown"][2]
        self.assertIn("could not check", unchecked["text"])
        self.assertEqual(unchecked["classes"], ["mut"])
        self.assertNotIn("unlikely", unchecked["text"])

    def test_a_failed_request_is_shown_not_taken_for_a_verdict(self):
        failed = self.out["verdicts"]["shown"][3]
        self.assertEqual((failed["text"], failed["classes"]), ("boom", ["warn"]))

    def test_every_check_goes_to_the_check_endpoint(self):
        self.assertEqual({u for u, _ in self.out["verdicts"]["sent"]}, {"api/ha/check"})

    def test_a_version_is_asked_about_once_and_the_refresh_only_repaints(self):
        cache = self.out["cache"]
        self.assertEqual(cache["calls"], 1)
        self.assertIn("lru-dict==1.3.0", cache["repainted"]["text"])  # what ha() paints on the 60 s refresh
        self.assertEqual(cache["unknown"]["text"], "")  # nothing is claimed about a version nobody checked

    def test_an_answer_that_arrives_after_the_selection_moved_on_is_kept_not_painted(self):
        stale = self.out["stale"]
        self.assertTrue(stale["remembered"])
        self.assertNotIn("lru-dict", stale["shown"]["text"])

    def test_the_install_button_offers_force_and_repeats_the_request_with_it(self):
        force = self.out["force"]
        self.assertTrue(force["forced"])
        self.assertEqual([u for u, _ in force["sent"]], ["api/ha/update", "api/ha/update", "api/restart"])
        self.assertNotIn("force", force["sent"][0][1])  # the first attempt never forces
        self.assertIn("unlikely to install", force["asked"][1])
        self.assertIn("lru-dict==1.3.0", force["asked"][1])

    def test_declining_the_force_schedules_nothing(self):
        declined = self.out["declined"]
        self.assertEqual(declined["calls"], 1)
        self.assertIn("lru-dict==1.3.0", declined["logs"][0])


if __name__ == "__main__":
    unittest.main()
