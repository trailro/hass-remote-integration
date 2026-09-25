"""verify.sh stops, removes and starts a container by name.  Its default name was the production container's
(hass-remote-integration), so `sh verify.sh recreate` without an .env replaced a user's running install; and `start`
piped `docker run` into cut, so a run that failed (name taken, port in use) went on to wait 15 minutes for an API
that could never come.  These run the script with `docker` and `curl` stand-ins that log what they are asked."""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATUS = ('{"ha_version": "2026.9.3", "components": [], "running": {}, "installed": {}, '
          '"state": {"restart_required": false, "last_action": "", "last_error": ""}}')
DOCKER = """#!/bin/sh
printf 'docker %s\\n' "$*" >> "$STUB_LOG"
case "$1" in
  run) [ "${STUB_RUN_RC:-0}" = 0 ] && echo 0123456789abcdef0123; exit "${STUB_RUN_RC:-0}" ;;
  inspect) echo false ;;
esac
exit 0
"""
CURL = """#!/bin/sh
printf 'curl %s\\n' "$*" >> "$STUB_LOG"
printf '%s' "$STUB_STATUS"
"""


@unittest.skipUnless(shutil.which("sh") and os.path.isfile(os.path.join(ROOT, "verify.sh")), "needs sh and verify.sh")
class VerifyShTest(unittest.TestCase):
    def _run(self, *args, **env):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copy(os.path.join(ROOT, "verify.sh"), tmp)  # a folder with no .env
        bin_dir = os.path.join(tmp, "bin")
        os.mkdir(bin_dir)
        for name, source in (("docker", DOCKER), ("curl", CURL)):
            path = os.path.join(bin_dir, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(source)
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        log = os.path.join(tmp, "log")
        clean = {k: v for k, v in os.environ.items() if not k.startswith("HRI_")}
        full = {**clean, "PATH": f"{bin_dir}{os.pathsep}{clean.get('PATH', '')}", "STUB_LOG": log, "STUB_STATUS": STATUS, **env}
        proc = subprocess.run(["sh", os.path.join(tmp, "verify.sh"), *args], env=full, capture_output=True, text=True,
                              timeout=30)
        with open(log, encoding="utf-8") as fh:
            return proc, fh.read().splitlines()

    def test_the_default_name_is_a_throwaway_one(self):
        proc, calls = self._run("recreate")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        docker = [c for c in calls if c.startswith("docker ")]
        self.assertIn("docker stop hri-verify", docker)
        self.assertIn("docker rm hri-verify", docker)
        self.assertTrue(any(c.startswith("docker run -d --name hri-verify ") and "-v hri-verify:/config" in c for c in docker))
        self.assertFalse([c for c in docker if "hass-remote-integration " in c + " " and "hass-remote-integration:local" not in c])

    def test_an_explicit_name_is_used(self):
        proc, calls = self._run("status", HRI_NAME="hri-mine")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(any(c.startswith("docker stats") and c.endswith(" hri-mine") for c in calls))

    def test_a_failed_docker_run_fails_at_once(self):
        proc, calls = self._run("start", STUB_RUN_RC="125")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("docker run failed", proc.stdout)
        waits = [c for c in calls if c.startswith("curl -s --max-time 2 ")]  # the wait loop; status asks once, with 5
        self.assertFalse(waits, "it waited for the API of a container that never ran")


if __name__ == "__main__":
    unittest.main()
