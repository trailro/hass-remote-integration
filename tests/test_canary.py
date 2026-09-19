"""The weekly canary: which versions it picks, and what it is allowed to do with the answer.

CI runs when somebody opens a pull request; Home Assistant ships on its own schedule.  The canary is
what notices a release that breaks the manager in a quiet week, so the parts worth pinning are the
version arithmetic (a beta is newer than the release before it, older than its own release) and the
blast radius of the job that writes: it may move what a fresh volume installs, never the measured floor.
"""

import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _versions_module():
    path = os.path.join(ROOT, ".github", "canary_versions.py")
    spec = importlib.util.spec_from_file_location("canary_versions", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VersionOrderTest(unittest.TestCase):
    def setUp(self):
        if not os.path.isfile(os.path.join(ROOT, ".github", "canary_versions.py")):
            self.skipTest("the workflows are not copied next to the tests")
        self.key = _versions_module().key

    def test_a_beta_sorts_between_the_release_before_it_and_its_own(self):
        order = ["2026.9.2", "2026.9.3", "2026.10.0b1", "2026.10.0b2", "2026.10.0", "2026.10.1"]
        self.assertEqual(sorted(order, key=self.key), order)

    def test_a_double_digit_minor_is_newer_than_a_single_digit_one(self):
        self.assertGreater(self.key("2026.10.0"), self.key("2026.9.9"))  # not a string comparison

    def test_the_stable_pattern_excludes_betas(self):
        stable = _versions_module().STABLE
        self.assertTrue(stable.match("2026.9.3"))
        for other in ("2026.10.0b1", "2026.9", "2026.9.3.post1", "2026.9.3\n"):
            self.assertFalse(stable.match(other), other)


class CanaryWorkflowTest(unittest.TestCase):
    def setUp(self):
        if not os.path.isfile(os.path.join(ROOT, ".github", "workflows", "canary.yml")):
            self.skipTest("the workflows are not copied next to the tests")
        self.wf = _read(".github", "workflows", "canary.yml")
        self.report = _read(".github", "canary_report.sh")

    def test_it_runs_on_a_schedule_and_by_hand(self):
        self.assertIn("schedule:", self.wf)
        self.assertIn("workflow_dispatch:", self.wf)

    def test_only_the_reporting_job_may_write(self):
        top = self.wf.split("\njobs:", 1)[0]
        self.assertIn("permissions:\n  contents: read", top)
        for write in ("contents: write", "issues: write", "pull-requests: write"):
            self.assertEqual(self.wf.count(write), 1, write)
        report = self.wf.split("\n  report:", 1)[1]
        for write in ("contents: write", "issues: write", "pull-requests: write"):
            self.assertIn(write, report)

    def test_the_boot_job_pins_the_version_and_checks_it_got_it(self):
        boot = self.wf.split("\n  boot:", 1)[1].split("\n  report:", 1)[0]
        self.assertIn("HA_VERSION_LATEST=0", boot)
        self.assertIn('test "$got" = "$VERSION"', boot)  # a silent fallback is a failure, not a pass
        self.assertIn("verify.sh test", boot)
        self.assertIn("verify.sh unit", boot)

    def test_a_pull_request_only_moves_what_a_fresh_volume_installs(self):
        self.assertIn("ARG HA_VERSION=", self.report)
        self.assertNotIn("HA_VERSION_MIN=", self.report)  # the floor is a measurement, not a moving target

    def test_it_proposes_nothing_unless_the_boot_passed(self):
        before_pr = self.report.split("git checkout -q -b", 1)[0]
        self.assertIn('[ "$RESULT" != "success" ]', before_pr)
        self.assertIn('[ "$STABLE" = "$DEFAULT" ]', before_pr)  # nothing to propose
        self.assertIn("gh pr list --head", before_pr)           # and never twice for the same version

    def test_a_failure_reuses_its_issue_instead_of_opening_another_every_week(self):
        self.assertIn("gh issue list", self.report)
        self.assertIn("gh issue comment", self.report)
        self.assertLess(self.report.index("gh issue comment"), self.report.index("gh issue create"))
