"""The weekly app canary (.github/workflows/app-canary.yml) and the Supervisor check CI runs.

The Supervisor, Home Assistant OS and the app linter ship on their own schedules; the canary asks where they are,
compares with .github/app_versions.json, and reports.  What is worth pinning: the version arithmetic (a pre-release
is not newer than its release, a version never moves back), what counts as a release note worth reading, what the
report does with each outcome (and that it does not repeat itself), and that CI runs the Supervisor's own code on the
app rather than only a linter's copy of its schema.  No network: the scripts' pure parts run on fixtures, and the
report runs with `gh` and `git` answering from a state.
"""

import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml

from tests.ghstub import Stubs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GITHUB = os.path.join(ROOT, ".github")


def _module(name):
    path = os.path.join(GITHUB, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _needs(*files):
    missing = [f for f in files if not os.path.exists(os.path.join(ROOT, f))]
    return unittest.skipIf(missing, f"not copied next to the tests: {missing}")


RECORD = {"supervisor": "2026.09.3", "haos": "18.3", "core": "2026.9.3", "linter": "v2.21.1", "linter_sha": "a" * 40,
          "configuration_md_sha256": "d" * 64}
NOW = {"supervisor": "2026.09.3", "haos": "18.3", "core": "2026.9.3", "supervisor_beta": "2026.09.3",
       "supervisor_newest": "2026.09.3", "linter": "v2.21.1", "linter_sha": "a" * 40, "configuration_md_sha256": "d" * 64}


@_needs(".github/app_canary_versions.py")
class VersionsTest(unittest.TestCase):
    def setUp(self):
        self.m = _module("app_canary_versions")

    def test_the_orders_of_the_three_projects(self):
        key = self.m.key
        self.assertEqual(sorted(["2026.10.0", "2026.09.3", "2026.9.10"], key=key), ["2026.09.3", "2026.9.10", "2026.10.0"])
        self.assertEqual(sorted(["18.4", "18.4.rc1", "18.3", "18.10"], key=key), ["18.3", "18.4.rc1", "18.4", "18.10"])
        self.assertEqual(sorted(["2026.10.0", "2026.10.0b1", "2026.10.0.dev20260901", "2026.9.3"], key=key),
                         ["2026.9.3", "2026.10.0.dev20260901", "2026.10.0b1", "2026.10.0"])
        self.assertRaises(ValueError, key, "latest")

    def test_nothing_moved(self):
        out = self.m.decide(RECORD, NOW)
        self.assertEqual(out["changed"], "false")
        self.assertEqual(out["moved"], "")
        self.assertEqual(json.loads(out["proposed"]), RECORD)
        self.assertEqual(json.loads(out["schema"]), [{"kind": "stable", "ref": "2026.09.3"}, {"kind": "beta", "ref": "2026.09.3"},
                                                     {"kind": "main", "ref": "main"}])

    def test_a_stable_channel_behind_the_record_is_not_a_change(self):
        """The record can be ahead of the stable channel (reviewed on the beta Supervisor): that proposes no
        downgrade, week after week."""
        out = self.m.decide(RECORD, {**NOW, "supervisor": "2026.09.2"})
        self.assertEqual(out["changed"], "false")
        self.assertEqual(json.loads(out["proposed"])["supervisor"], "2026.09.3")
        self.assertEqual(json.loads(out["schema"])[0], {"kind": "stable", "ref": "2026.09.2"})

    def test_each_kind_of_move(self):
        now = {**NOW, "supervisor": "2026.10.0", "haos": "18.4", "core": "2026.10.1", "linter": "v2.22.0",
               "linter_sha": "b" * 40, "configuration_md_sha256": "e" * 64}
        out = self.m.decide(RECORD, now)
        self.assertEqual(out["changed"], "true")
        for part in ("Supervisor 2026.09.3 -> 2026.10.0", "Home Assistant OS 18.3 -> 18.4",
                     "Home Assistant Core 2026.9.3 -> 2026.10.1", "app linter v2.21.1 -> v2.22.0", "documentation"):
            self.assertIn(part, out["moved"])
        self.assertEqual(json.loads(out["proposed"]), {"supervisor": "2026.10.0", "haos": "18.4", "core": "2026.10.1",
                                                       "linter": "v2.22.0", "linter_sha": "b" * 40,
                                                       "configuration_md_sha256": "e" * 64})
        self.assertEqual(out["branch"], "app-canary/sup-2026.10.0-os-18.4-core-2026.10.1-lint-v2.22.0-docs-eeeeeee")

    def test_a_beta_is_not_newer_than_its_release(self):
        out = self.m.decide({**RECORD, "haos": "18.4"}, {**NOW, "haos": "18.4.rc2"})
        self.assertEqual(out["changed"], "false")

    def test_the_committed_record(self):
        with open(os.path.join(GITHUB, "app_versions.json"), encoding="utf-8") as fh:
            record = json.load(fh)
        self.assertEqual(set(record), set(RECORD))
        for field in ("supervisor", "haos", "core"):
            self.m.key(record[field])
        self.assertRegex(record["linter_sha"], r"^[0-9a-f]{40}$")
        self.assertRegex(record["configuration_md_sha256"], r"^[0-9a-f]{64}$")


SUPERVISOR_BODY = """## :boom: Breaking Changes

- #7153 Use port 80 as default for Core/landingpage @sairon

## :sparkles: New Features

- #7230 Add feature flag to drop NET_RAW from app containers @agners
- #7181 Add `/time` API for setting NTP servers on HAOS @sairon

## :arrow_up: Dependency Updates

<details>
<summary>2 changes</summary>

- #7222 Bump home-assistant/builder/actions/build-image from 2026.06.0 to 2026.09.0 @dependabot
</details>
"""
OS_BODY = """Home Assistant OS 18.4 is a bugfix release.

## Home Assistant Operating System

* Update Docker to v29.6.0 (#4800) @sairon
* Fix HDMI output on large displays (#4801) @sairon

### Breaking changes

* The swap file moves (#4802)

## Buildroot

* Bump Linux to 6.18.9 (#4803)
"""


def _rel(tag, body, prerelease=False, draft=False):
    return {"tag_name": tag, "body": body, "prerelease": prerelease, "draft": draft,
            "html_url": f"https://example.invalid/{tag}"}


@_needs(".github/app_release_scan.py")
class ReleaseScanTest(unittest.TestCase):
    def setUp(self):
        self.m = _module("app_release_scan")

    def test_breaking_changes_whole_and_keyword_lines_without_the_dependency_bumps(self):
        breaking, hits = self.m.sections(SUPERVISOR_BODY)
        self.assertEqual(breaking, ["- #7153 Use port 80 as default for Core/landingpage @sairon"])
        self.assertEqual(hits, ["- #7230 Add feature flag to drop NET_RAW from app containers @agners"])
        breaking, hits = self.m.sections(OS_BODY)
        self.assertEqual(breaking, ["* The swap file moves (#4802)"])  # a level-3 section, ended by the next level 2
        self.assertEqual(hits, ["* Update Docker to v29.6.0 (#4800) @sairon"])

    def test_only_releases_newer_than_the_record(self):
        releases = {
            "home-assistant/supervisor": [_rel("2026.09.3", SUPERVISOR_BODY), _rel("2026.10.0", SUPERVISOR_BODY),
                                          _rel("2026.10.1", "", draft=True)],
            "home-assistant/operating-system": [_rel("18.3", OS_BODY), _rel("18.4.rc1", OS_BODY, prerelease=True),
                                                _rel("18.4", "## Changes\n\n* Nothing for apps")],
        }
        found, body = self.m.scan(RECORD, releases, docs_sha="d" * 64)
        self.assertTrue(found)
        self.assertEqual(body.splitlines()[0], "<!-- app-canary-notes: supervisor 2026.10.0, haos 18.4.rc1 -->")
        self.assertIn("Supervisor 2026.10.0", body)
        self.assertIn("Home Assistant OS 18.4.rc1](https://example.invalid/18.4.rc1) (pre-release)", body)
        self.assertNotIn("2026.09.3", body)
        self.assertNotIn("Home Assistant OS 18.4]", body)  # nothing in it concerns the app
        self.assertNotIn("configuration.md", body)

    def test_the_documentation_digest(self):
        commits = [{"sha": "1234567890", "html_url": "https://example.invalid/c",
                    "commit": {"message": "Document app_config\n\nlong", "committer": {"date": "2026-09-20T10:00:00Z"}}}]
        found, body = self.m.scan(RECORD, {}, docs_sha="e" * 64, docs_commits=commits)
        self.assertTrue(found)
        self.assertIn("docs eeeeeee", body.splitlines()[0])
        self.assertIn("- 2026-09-20 [Document app_config](https://example.invalid/c)", body)

    def test_nothing_to_say(self):
        self.assertEqual(self.m.scan(RECORD, {"home-assistant/supervisor": [_rel("2026.09.3", SUPERVISOR_BODY)]},
                                     docs_sha="d" * 64), (False, ""))


@_needs(".github/app_canary_report.sh")
@unittest.skipUnless(shutil.which("sh"), "needs sh")
class ReportTest(unittest.TestCase):
    """app_canary_report.sh with `gh` and `git` answering from a state and logging what it would do."""

    MOVED = {"supervisor": "2026.10.0", "haos": "18.4", "core": "2026.10.1", "linter": "v2.21.1",
             "linter_sha": "a" * 40, "configuration_md_sha256": "d" * 64}
    BRANCH = "app-canary/sup-2026.10.0-os-18.4-core-2026.10.1-lint-v2.21.1-docs-ddddddd"

    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work, True)
        os.makedirs(os.path.join(self.work, ".github"))
        shutil.copy(os.path.join(GITHUB, "app_versions.json"), os.path.join(self.work, ".github"))
        self.notes = os.path.join(self.work, "notes.md")

    def _run(self, state=None, notes=None, **env):
        self.stubs = Stubs(self, state or {}, git=True)
        base = {"VERSIONS_RESULT": "success", "SCHEMA_STABLE": "success", "SCHEMA_BETA": "success",
                "SCHEMA_MAIN": "success", "LINT_RESULT": "success", "HAOS_RESULT": "success", "NOTES_FOUND": "false",
                "NOTES_FILE": self.notes, "CHANGED": "true", "MOVED": "Supervisor 2026.09.3 -> 2026.10.0",
                "PROPOSED": json.dumps(self.MOVED), "BRANCH": self.BRANCH, "SUPERVISOR": "2026.10.0",
                "SUPERVISOR_BETA": "2026.10.1", "HAOS": "18.4", "CORE": "2026.10.1", "RUN": "https://example.invalid/run",
                "GH_SERVER": "https://github.com", "GH_REPO": "trailro/hass-remote-integration", "GH_TOKEN": "x"}
        if notes is not None:
            with open(self.notes, "w", encoding="utf-8") as fh:
                fh.write(notes)
            base["NOTES_FOUND"] = "true"
        proc = subprocess.run(["sh", os.path.join(GITHUB, "app_canary_report.sh")], cwd=self.work,
                              env=self.stubs.env(**{**base, **env}), capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout + proc.stderr

    def _title(self, call):
        return call[call.index("--title") + 1]

    def test_all_passed_on_new_versions_proposes_the_record(self):
        self._run({"prs": [{"number": 3, "headRefName": "app-canary/sup-2026.09.4-x", "state": "OPEN"},
                           {"number": 4, "headRefName": "canary/ha-2026.10.0", "state": "OPEN"}]})
        self.assertEqual(self.stubs.called("gh", "issue", "create"), [])
        self.assertEqual(len(self.stubs.called("gh", "pr", "create")), 1)
        self.assertEqual(self.stubs.called("git", "push"), [["git", "push", "-q", "origin", self.BRANCH]])
        with open(os.path.join(self.work, ".github", "app_versions.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), self.MOVED)
        self.assertEqual([c[3] for c in self.stubs.called("gh", "pr", "close")], ["3"])  # the older app canary PR only
        self.assertEqual(self.stubs.called("gh", "workflow", "run"), [["gh", "workflow", "run", "ci.yml", "--ref", self.BRANCH]])

    def test_a_failure_is_an_issue_and_blocks_the_proposal(self):
        self._run(HAOS_RESULT="failure")
        created = self.stubs.called("gh", "issue", "create")
        self.assertEqual(len(created), 1)
        self.assertTrue(self._title(created[0]).startswith("[app-canary] the app fails on Supervisor 2026.10.0"))
        self.assertEqual(self.stubs.called("gh", "pr", "create"), [])

    def test_a_failing_beta_or_main_schema_is_reported_but_the_stable_path_is_proposed(self):
        self._run(SCHEMA_BETA="failure", SCHEMA_MAIN="failure")
        self.assertEqual(len(self.stubs.called("gh", "issue", "create")), 1)
        self.assertEqual(len(self.stubs.called("gh", "pr", "create")), 1)

    def test_the_same_failure_next_week_is_a_comment(self):
        title = ("[app-canary] the app fails on Supervisor 2026.10.0 (beta 2026.10.1), Home Assistant OS 18.4, "
                 "Core 2026.10.1")
        self._run({"issues": [{"number": 9, "title": title}]}, LINT_RESULT="failure")
        self.assertEqual(self.stubs.called("gh", "issue", "create"), [])
        self.assertEqual([c[3] for c in self.stubs.called("gh", "issue", "comment")], ["9"])

    def test_a_skipped_os_boot_proposes_nothing(self):
        out = self._run(HAOS_RESULT="skipped")
        self.assertEqual(self.stubs.called("gh", "pr", "create"), [])
        self.assertEqual(self.stubs.called("gh", "issue", "create"), [])
        self.assertIn("did not pass", out)

    def test_never_the_same_pull_request_twice(self):
        for state in ("OPEN", "CLOSED", "MERGED"):
            self._run({"prs": [{"number": 7, "headRefName": self.BRANCH, "state": state}]})
            self.assertEqual(self.stubs.called("gh", "pr", "create"), [], state)
            self.assertEqual(self.stubs.called("git", "push"), [], state)

    def test_nothing_moved_proposes_nothing(self):
        self._run(CHANGED="false")
        self.assertEqual([c for c in self.stubs.calls() if c[0] == "git" or c[1:3] == ["pr", "create"]], [])

    def test_notes_are_an_issue_once(self):
        notes = "<!-- app-canary-notes: supervisor 2026.10.0 -->\nbody\n"
        self._run(notes=notes)
        created = self.stubs.called("gh", "issue", "create")
        self.assertEqual([self._title(c) for c in created], ["[app-canary] release notes to review"])
        issue = {"number": 5, "title": "[app-canary] release notes to review", "body": "older\n",
                 "comments": [{"body": notes}]}
        self._run({"issues": [issue]}, notes=notes)  # said already: nothing
        self.assertEqual(self.stubs.called("gh", "issue", "comment"), [])
        self.assertEqual(self.stubs.called("gh", "issue", "create"), [])
        self._run({"issues": [issue]}, notes="<!-- app-canary-notes: supervisor 2026.10.1 -->\nnew\n")
        self.assertEqual([c[3] for c in self.stubs.called("gh", "issue", "comment")], ["5"])

    def test_the_versions_job_failing_is_an_issue_and_nothing_else(self):
        self._run(VERSIONS_RESULT="failure", notes="<!-- m -->\n")
        self.assertEqual(len(self.stubs.called("gh", "issue", "create")), 1)
        self.assertEqual(self.stubs.called("gh", "pr", "create"), [])

    def test_a_refused_pull_request_becomes_an_issue(self):
        self._run({"pr_create_rc": 1})
        created = self.stubs.called("gh", "issue", "create")
        self.assertEqual(len(created), 1)
        self.assertIn("is ready to be recorded", self._title(created[0]))
        self.assertEqual(self.stubs.called("gh", "pr", "close"), [])

    def test_dry_run_writes_nothing(self):
        out = self._run({"prs": [{"number": 3, "headRefName": "app-canary/old", "state": "OPEN"}]},
                        notes="<!-- m -->\n", LINT_RESULT="failure", SCHEMA_STABLE="success", DRY_RUN="1")
        writes = [c for c in self.stubs.calls()
                  if c[:2] == ["git", "push"] or (c[0] == "gh" and c[1:3] in (["issue", "create"], ["issue", "comment"],
                                                                              ["pr", "create"], ["pr", "close"], ["workflow", "run"]))]
        self.assertEqual(writes, [])
        self.assertIn("DRY_RUN: gh issue create --title [app-canary] the app fails", out)
        self.assertIn("DRY_RUN: gh issue create --title [app-canary] release notes to review", out)

    def test_it_never_touches_the_docs(self):
        with open(os.path.join(GITHUB, "app_canary_report.sh"), encoding="utf-8") as fh:
            script = fh.read()
        code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotRegex(code, r"(sed|>|git add|git commit)[^\n]*docs/")  # the "tested on" line is kept by hand
        self.assertIn("> .github/app_versions.json", code)


@_needs(".github/app_supervisor_check.py")
class SupervisorCheckTest(unittest.TestCase):
    def setUp(self):
        self.m = _module("app_supervisor_check")

    def test_a_key_the_schema_drops_is_found_at_any_depth(self):
        given = {"name": "x", "map": [{"type": "addon_config", "read_only": False, "new": 1}], "gone": True}
        kept = {"name": "x", "map": [{"type": "addon_config", "read_only": False}], "added": 1}
        self.assertEqual(self.m.dropped_keys(given, kept), ["map[0].new", "gone"])

    def test_the_allow_list(self):
        self.assertEqual(self.m.allowed_lines(), [])  # app/config.yaml maps app_config: nothing to let through
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = pathlib.Path(tmp, "allow.txt")
        path.write_text("# a comment\n\nuses legacy map type 'addon_config'; use 'app_config' instead\n", encoding="utf-8")
        allow = self.m.allowed_lines(path)
        self.assertEqual(allow, ["uses legacy map type 'addon_config'; use 'app_config' instead"])
        msg = "App 'hass-remote-integration' uses legacy map type 'addon_config'; use 'app_config' instead."
        self.assertEqual(self.m.unexpected([msg, "App 'x' uses deprecated 'arch' values: ['armv7']"], allow),
                         ["App 'x' uses deprecated 'arch' values: ['armv7']"])

    def test_example_paths_of_the_globs(self):
        self.assertEqual(self.m.example_path(".storage/tmp" + "[a-z0-9_]" * 8, "x1"), ".storage/tmpaaaaaaaa")
        self.assertEqual(self.m.example_path("venv-*", "x1"), "venv-x1")
        self.assertEqual(self.m.example_path("integration_manager/events.jsonl*", ""), "integration_manager/events.jsonl")

    def test_discovery_skips_dot_folders_and_rootfs(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        for rel in ("app/config.yaml", ".supervisor/tests/fixtures/config.yaml", "app/rootfs/config.json", "x/config.txt"):
            os.makedirs(os.path.dirname(os.path.join(tmp, rel)), exist_ok=True)
            open(os.path.join(tmp, rel), "w").close()
        import pathlib
        self.assertEqual(self.m.discovered(pathlib.Path(tmp), [".yaml", ".yml", ".json"]), ["app/config.yaml"])


@_needs(".github/workflows/ci.yml", ".github/workflows/app-canary.yml")
class WiringTest(unittest.TestCase):
    def _wf(self, name):
        with open(os.path.join(GITHUB, "workflows", name), encoding="utf-8") as fh:
            return yaml.safe_load(fh)

    def test_ci_runs_the_supervisor_check_on_the_recorded_supervisor(self):
        steps = self._wf("ci.yml")["jobs"]["app"]["steps"]
        uses = [s.get("uses", "") for s in steps]
        self.assertTrue(any(u.startswith("frenck/action-app-linter@") for u in uses))  # the linter stays
        pin = next(s for s in steps if s.get("id") == "pin")
        self.assertIn(".github/app_versions.json", pin["run"])
        checkout = next(s for s in steps if (s.get("with") or {}).get("repository") == "home-assistant/supervisor")
        self.assertEqual(checkout["with"]["ref"], "${{ steps.pin.outputs.supervisor }}")
        self.assertTrue(checkout["with"]["path"].startswith("."))  # the discovery check must not find its fixtures
        run = next(s["run"] for s in steps if "app_supervisor_check.py" in s.get("run", ""))
        self.assertIn(f"app_supervisor_check.py {checkout['with']['path']}", run)
        self.assertIn(f"-r {checkout['with']['path']}/requirements.txt", run)
        self.assertNotRegex(run, r"pip install[^\n]* supervisor\b")  # supervisord on PyPI

    def test_the_app_canary_workflow(self):
        wf = self._wf("app-canary.yml")
        on = wf.get("on", wf.get(True))
        self.assertIn("schedule", on)
        self.assertIn("workflow_dispatch", on)
        self.assertEqual(set(on), {"schedule", "workflow_dispatch"})  # no temporary push trigger left behind
        self.assertEqual(wf["permissions"], {"contents": "read"})
        self.assertEqual(wf["concurrency"], {"group": "app-canary", "cancel-in-progress": False})
        jobs = wf["jobs"]
        self.assertEqual(set(jobs), {"versions", "schema", "lint", "haos", "notes", "report"})
        writers = sorted(n for n, j in jobs.items() if (j.get("permissions") or {}).get("contents") == "write")
        self.assertEqual(writers, ["report"])
        self.assertEqual(jobs["report"]["permissions"].get("actions"), "write")
        self.assertEqual(jobs["haos"]["timeout-minutes"], 45)
        self.assertIn("app_supervisor_check.py", str(jobs["schema"]["steps"]))
        self.assertIn("app_canary_report.sh", jobs["report"]["steps"][-1]["run"])
        for job in jobs.values():
            for step in job.get("steps", []):
                uses = step.get("uses", "")
                if uses and not uses.startswith("./"):
                    self.assertRegex(uses, r"@[0-9a-f]{40}$", uses)


if __name__ == "__main__":
    unittest.main()
