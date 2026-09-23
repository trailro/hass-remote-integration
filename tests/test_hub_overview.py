"""The Docker Hub overview is built from README.md by a step of the release workflow, and Docker Hub keeps at most
25000 bytes of it.  The upload action cuts anything longer with only a warning, and the first thing it cuts is the
last line: the link to the rest of the README.  This runs the workflow's own script (not a copy of it) on the
README of this commit, so a README that grows past the limit fails the pull request, not the release."""

import os
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "image.yml")
LIMIT = 25000
STEP = "Build the overview"


def _step() -> dict:
    with open(WORKFLOW, encoding="utf-8") as fh:
        wf = yaml.safe_load(fh)
    for job in wf["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") == STEP:
                return step
    raise AssertionError(f"no step named {STEP!r} in image.yml")


@unittest.skipUnless(shutil.which("bash") and shutil.which("awk") and shutil.which("sed"), "needs bash, awk and sed")
class HubOverviewTest(unittest.TestCase):
    def _run(self, readme: str | None = None) -> tuple[subprocess.CompletedProcess, str]:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        work = os.path.join(tmp, "repo")
        os.makedirs(work)
        if readme is None:
            shutil.copy(os.path.join(ROOT, "README.md"), work)
        else:
            with open(os.path.join(work, "README.md"), "w", encoding="utf-8") as fh:
                fh.write(readme)
        # the longest tag and repository URL a release realistically has: the links grow with both
        env = {**os.environ, "RUNNER_TEMP": tmp, "TAG": "v10.100.100",
               "REPO_URL": "https://github.com/trailro/hass-remote-integration"}
        proc = subprocess.run(["bash", "-e", "-c", _step()["run"]], cwd=work, env=env, capture_output=True, text=True)
        return proc, os.path.join(tmp, "hub", "README.md")

    def test_this_readme_fits_docker_hub(self):
        proc, out = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        size = os.path.getsize(out)
        self.assertLessEqual(size, LIMIT, f"the overview is {size} bytes: shorten README.md above '## Everyday operation'")
        with open(out, encoding="utf-8") as fh:
            self.assertIn("full README on GitHub", fh.read().splitlines()[-1])

    def test_the_step_refuses_an_overview_over_the_limit(self):
        proc, _ = self._run("# t\n\n" + "x" * (LIMIT + 10) + "\n\n## Everyday operation\n")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("over the 25000", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
