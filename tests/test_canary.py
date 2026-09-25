"""The weekly canary: which versions it picks, and what it is allowed to do with the answer.

CI runs when somebody opens a pull request; Home Assistant ships on its own schedule.  The canary is
what notices a release that breaks the manager in a quiet week, so the parts worth pinning are the
version arithmetic (a beta is newer than the release before it, older than its own release) and the
blast radius of the job that writes: it may move what a fresh volume installs, never the measured floor.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

from tests.ghstub import Stubs

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
        self.assertIn('[ "$STABLE_RESULT" != "success" ]', before_pr)  # the stable leg's own result
        self.assertIn('[ "$STABLE" = "$DEFAULT" ]', before_pr)  # nothing to propose
        self.assertIn("gh pr list --head", before_pr)           # and never twice for the same version

    def test_a_refused_pull_request_becomes_an_issue_rather_than_a_failed_run(self):
        """Creating pull requests from Actions is a repository setting that is off by default, and a
        canary that reports nothing because of a permission is worse than one that reports by hand."""
        tail = self.report.split("gh pr create", 1)[1]
        self.assertIn("compare/main...", tail)  # the branch is pushed: say where it is
        self.assertIn("gh issue create", tail)

    def test_a_failure_reuses_its_issue_instead_of_opening_another_every_week(self):
        self.assertIn("gh issue list", self.report)
        self.assertIn("gh issue comment", self.report)
        self.assertLess(self.report.index("gh issue comment"), self.report.index("gh issue create"))


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("sh"), "needs the sed -i of the Linux runner")
class ReportScriptTest(unittest.TestCase):
    """canary_report.sh itself, with `gh` and `git` answering from a state and logging what it would do."""

    def setUp(self):
        script = os.path.join(ROOT, ".github", "canary_report.sh")
        if not os.path.isfile(script):
            self.skipTest("the workflows are not copied next to the tests")
        self.script = script
        self.work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work, True)
        with open(os.path.join(self.work, "Dockerfile"), "w", encoding="utf-8") as fh:
            fh.write("FROM x\nARG HA_VERSION=2026.9.3\n")

    def _run(self, state=None, **env):
        self.stubs = Stubs(self, state or {}, git=True)
        base = {"STABLE_RESULT": "success", "PRERELEASE_RESULT": "success", "STABLE": "2026.10.0",
                "PRERELEASE": "2026.11.0b1", "DEFAULT": "2026.9.3", "RUN": "https://example.invalid/run",
                "GH_SERVER": "https://github.com", "GH_REPO": "trailro/hass-remote-integration", "GH_TOKEN": "x",
                # the combined matrix result the workflow passed before each leg reported its own
                "RESULT": "failure" if "failure" in (env.get("STABLE_RESULT"), env.get("PRERELEASE_RESULT")) else "success"}
        proc = subprocess.run(["sh", self.script], cwd=self.work, env=self.stubs.env(**{**base, **env}),
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout + proc.stderr

    def test_a_failing_prerelease_does_not_hold_back_the_passing_stable(self):
        self._run(PRERELEASE_RESULT="failure")
        created = self.stubs.called("gh", "issue", "create")
        self.assertEqual(len(created), 1)
        title = created[0][created[0].index("--title") + 1]
        self.assertEqual(title, "[canary] Home Assistant 2026.11.0b1 breaks the manager")
        self.assertEqual(len(self.stubs.called("gh", "pr", "create")), 1)

    def test_a_failing_stable_proposes_nothing(self):
        self._run(STABLE_RESULT="failure")
        self.assertEqual(len(self.stubs.called("gh", "issue", "create")), 1)
        self.assertEqual(self.stubs.called("gh", "pr", "create"), [])
        self.assertEqual(self.stubs.called("git", "push"), [])

    def test_a_closed_unmerged_pull_request_is_not_opened_again(self):
        out = self._run({"prs": [{"number": 7, "headRefName": "canary/ha-2026.10.0", "state": "CLOSED"}]})
        self.assertEqual(self.stubs.called("gh", "pr", "create"), [])
        self.assertEqual(self.stubs.called("git", "push"), [])
        self.assertIn("closed without merging", out)

    def test_an_open_pull_request_is_left_alone(self):
        self._run({"prs": [{"number": 7, "headRefName": "canary/ha-2026.10.0", "state": "OPEN"}]})
        self.assertEqual(self.stubs.called("gh", "pr", "create"), [])
        self.assertEqual(self.stubs.called("gh", "workflow", "run"), [])

    def test_a_new_pull_request_closes_the_older_ones_and_starts_ci(self):
        prs = [{"number": 3, "headRefName": "canary/ha-2026.9.4", "state": "OPEN"},
               {"number": 4, "headRefName": "canary/ha-2026.9.3", "state": "CLOSED"},
               {"number": 5, "headRefName": "dependabot/x", "state": "OPEN"}]
        self._run({"prs": prs})
        self.assertEqual(len(self.stubs.called("gh", "pr", "create")), 1)
        self.assertEqual([c[3] for c in self.stubs.called("gh", "pr", "close")], ["3"])
        self.assertEqual(self.stubs.called("gh", "workflow", "run"),
                         [["gh", "workflow", "run", "ci.yml", "--ref", "canary/ha-2026.10.0"]])
        with open(os.path.join(self.work, "Dockerfile"), encoding="utf-8") as fh:
            self.assertIn("ARG HA_VERSION=2026.10.0", fh.read())

    def test_a_refused_pull_request_still_starts_ci_on_the_pushed_branch(self):
        self._run({"pr_create_rc": 1})
        self.assertEqual(len(self.stubs.called("gh", "workflow", "run")), 1)
        self.assertEqual(self.stubs.called("gh", "pr", "close"), [])
        self.assertEqual(len(self.stubs.called("gh", "issue", "create")), 1)


class CanaryLegsTest(unittest.TestCase):
    def test_the_report_reads_each_leg(self):
        path = os.path.join(ROOT, ".github", "workflows", "canary.yml")
        if not os.path.isfile(path):
            self.skipTest("the workflows are not copied next to the tests")
        with open(path, encoding="utf-8") as fh:
            wf = yaml.safe_load(fh)
        boot = [s.get("uses", "") + str(s.get("with", "")) for s in wf["jobs"]["boot"]["steps"]]
        self.assertTrue(any("upload-artifact" in s and "canary-${{ matrix.kind }}" in s for s in boot))
        report = wf["jobs"]["report"]
        self.assertEqual(report["permissions"].get("actions"), "write")  # gh workflow run
        step = report["steps"][-1]
        self.assertIn("STABLE_RESULT=$(leg stable)", step["run"])
        self.assertNotIn("RESULT", step["env"])  # the combined matrix result no longer decides anything
