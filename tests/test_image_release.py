"""Which registry tags a release build moves (image.yml): :latest and the Docker Hub overview follow the newest stable
release, :X.Y the newest stable release of its series.  "Stable" is what GitHub says about the release, not what the
event payload or the tag's spelling says: a manual run carries no release payload, and a pre-release can have a plain
vX.Y.Z tag.  These run the workflow's own step scripts (not copies) with `gh` answering from a list of releases."""

import os
import shutil
import subprocess
import tempfile
import unittest

import yaml

from tests.ghstub import Stubs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "image.yml")


def release(tag, prerelease=False, draft=False):
    return {"tagName": tag, "isPrerelease": prerelease, "isDraft": draft}


# v0.26.0 is a pre-release with a plain tag, v0.26.0b1 one with a beta tag, v0.27.0 a draft
RELEASES = [release("v0.24.0"), release("v0.24.1"), release("v0.25.0"), release("v0.25.1"),
            release("v0.26.0b1", prerelease=True), release("v0.26.0", prerelease=True), release("v0.27.0", draft=True)]


def _step(job: str, step_id: str) -> str:
    with open(WORKFLOW, encoding="utf-8") as fh:
        wf = yaml.safe_load(fh)
    return next(s["run"] for s in wf["jobs"][job]["steps"] if s.get("id") == step_id)


@unittest.skipUnless(os.path.isfile(WORKFLOW), "the workflows are not copied next to the tests")
@unittest.skipUnless(shutil.which("bash") and shutil.which("sort"), "needs bash and sort")
class ReleaseTagsTest(unittest.TestCase):
    def _outputs(self, job, step_id, tag, event="release", releases=RELEASES) -> dict:
        stubs = Stubs(self, {"releases": releases})
        out = os.path.join(stubs.dir, "out")
        env = stubs.env(TAG=tag, GITHUB_EVENT_NAME=event, GITHUB_OUTPUT=out, GITHUB_REPOSITORY="trailro/hass-remote-integration")
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)
        # the shell GitHub runs a step with: bash -e -o pipefail
        proc = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", _step(job, step_id)], cwd=work, env=env,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(stubs.called("gh", "release", "list"), "the releases were not asked")
        with open(out, encoding="utf-8") as fh:
            return dict(line.split("=", 1) for line in fh.read().splitlines() if "=" in line)

    def test_the_newest_stable_release_takes_latest_and_its_series(self):
        self.assertEqual(self._outputs("image", "latest", "v0.25.1"), {"enable": "true", "minor": "true"})

    def test_a_stable_patch_published_while_a_newer_prerelease_exists_is_the_newest(self):
        """v0.26.0 is a pre-release with a plain tag: the raw tags called it the newest, and v0.25.1 moved nothing."""
        self.assertEqual(self._outputs("image", "latest", "v0.25.1")["enable"], "true")
        self.assertEqual(self._outputs("dockerhub-overview", "newest", "v0.25.1")["enable"], "true")

    def test_a_prerelease_with_a_plain_tag_moves_nothing(self):
        for event in ("release", "workflow_dispatch"):
            self.assertEqual(self._outputs("image", "latest", "v0.26.0", event), {"enable": "false", "minor": "false"}, event)
            self.assertEqual(self._outputs("dockerhub-overview", "newest", "v0.26.0", event)["enable"], "false", event)

    def test_an_older_patch_published_again_leaves_its_series_alone(self):
        self.assertEqual(self._outputs("image", "latest", "v0.24.0", "workflow_dispatch"), {"enable": "false", "minor": "false"})
        self.assertEqual(self._outputs("image", "latest", "v0.24.1", "workflow_dispatch"), {"enable": "false", "minor": "true"})

    def test_a_manual_run_never_moves_latest(self):
        self.assertEqual(self._outputs("image", "latest", "v0.25.1", "workflow_dispatch"), {"enable": "false", "minor": "true"})

    def test_a_draft_is_not_a_release(self):
        self.assertEqual(self._outputs("image", "latest", "v0.27.0"), {"enable": "false", "minor": "false"})

    def test_the_minor_tag_is_gated(self):
        with open(WORKFLOW, encoding="utf-8") as fh:
            wf = fh.read()
        self.assertIn("type=semver,pattern={{major}}.{{minor}},value=${{ env.TAG }},enable=${{ steps.latest.outputs.minor }}", wf)
        self.assertNotIn("PRERELEASE:", wf)  # the event payload no longer decides what is a pre-release


if __name__ == "__main__":
    unittest.main()
