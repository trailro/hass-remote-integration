"""The CI check that the newest released HRI Manager accepts and stamps app/config.yaml (.github/manager_compat_check.py),
against a stand-in for the manager's hrimgr.stamp: the real one is checked out in CI, not in the container."""

import contextlib
import importlib.util
import io
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github" / "manager_compat_check.py"

STAMP = '''
import yaml

ACCEPTED = {accepted!r}
FAIL_STAMP = {fail_stamp!r}


class TemplateError(Exception):
    pass


def vet_template(data):
    for key in data:
        if key not in ACCEPTED:
            raise TemplateError(f"HRI's app definition has {{key!r}}, which this manager version does not accept")


def parse_template(raw):
    data = yaml.safe_load(raw)
    vet_template(data)
    return data


def stamp(template, name, version, channel, bluetooth=False):
    if FAIL_STAMP and bluetooth:
        raise TemplateError("a backup_exclude entry names HRI's slug elsewhere")
    return dict(template, slug="hri_" + name, version=version, **({{"host_dbus": True}} if bluetooth else {{}}))


def dump(config, source):
    return yaml.safe_dump(config).encode()
'''


@unittest.skipUnless(SCRIPT.is_file() and (ROOT / "app" / "config.yaml").is_file(), ".github or app/ not copied next to the tests")
class ManagerCompatTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("manager_compat_check", SCRIPT)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.keys = list(yaml.safe_load((ROOT / "app" / "config.yaml").read_text(encoding="utf-8")))
        path = mock.patch.object(sys, "path", list(sys.path))
        path.start()
        self.addCleanup(path.stop)
        modules = mock.patch.dict(sys.modules)
        modules.start()
        self.addCleanup(modules.stop)

    def _manager(self, accepted, fail_stamp=False):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        pkg = pathlib.Path(tmp, "hri_manager", "hrimgr")
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text('VERSION = "9.9.9"\n', encoding="utf-8")
        (pkg / "stamp.py").write_text(textwrap.dedent(STAMP.format(accepted=set(accepted), fail_stamp=fail_stamp)), encoding="utf-8")
        for name in [m for m in sys.modules if m == "hrimgr" or m.startswith("hrimgr.")]:
            del sys.modules[name]
        return pathlib.Path(tmp)

    def test_a_manager_that_knows_every_key_passes(self):
        self.assertEqual(self.mod.check(self._manager(self.keys), ROOT), [])

    def test_a_key_the_manager_refuses_is_named(self):
        problems = self.mod.check(self._manager([k for k in self.keys if k not in ("backup_pre", "uart")]), ROOT)
        self.assertEqual([p.split(" (", 1)[0] for p in problems],
                         [f"release a manager that accepts {k} first" for k in self.keys if k in ("backup_pre", "uart")])

    def test_a_stamp_that_fails_fails(self):
        problems = self.mod.check(self._manager(self.keys, fail_stamp=True), ROOT)
        self.assertEqual(len(problems), 2)  # both channels, with Bluetooth
        self.assertTrue(all(p.startswith("release a manager that stamps this app/config.yaml first") for p in problems))

    def test_the_exit_status(self):
        for accepted, rc in ((self.keys, 0), (self.keys[1:], 1)):
            with self.subTest(rc=rc):
                manager = self._manager(accepted)
                err, out = io.StringIO(), io.StringIO()
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                    self.assertEqual(self.mod.main(["x", str(manager), str(ROOT)]), rc)
                self.assertIn("HRI Manager 9.9.9", (err if rc else out).getvalue())
                if rc:
                    self.assertIn(f"release a manager that accepts {self.keys[0]} first", err.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.mod.main(["x"]), 2)


WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
FAKE_GH = """#!/bin/sh
printf '%s\\n' "$@" > "$GH_ARGS"
[ -n "$GH_FAIL" ] && { echo "gh: HTTP 503" >&2; exit 1; }
printf '%s' "$GH_TAGS"
"""


@unittest.skipUnless(WORKFLOW.is_file() and shutil.which("bash") and shutil.which("sort"), ".github not copied, or no bash")
class ManagerTagTest(unittest.TestCase):
    """The CI step that picks the manager release to check against, run as GitHub runs it (bash -e) with a stand-in
    for gh."""

    def _pick(self, tags, fail=False):
        steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["manager"]["steps"]
        script = next(step for step in steps if step.get("id") == "release")["run"]
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        gh = tmp / "gh"
        gh.write_text(FAKE_GH, encoding="utf-8")
        gh.chmod(0o755)
        env = {**os.environ, "PATH": f"{tmp}:{os.environ.get('PATH', '/usr/bin:/bin')}", "GITHUB_OUTPUT": str(tmp / "out"),
               "GH_ARGS": str(tmp / "args"), "GH_TAGS": "".join(f"{t}\n" for t in tags), "GH_FAIL": "1" if fail else ""}
        proc = subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True, text=True, timeout=30)
        out = (tmp / "out").read_text(encoding="utf-8") if (tmp / "out").exists() else ""
        self.args = (tmp / "args").read_text(encoding="utf-8").splitlines()
        return proc.returncode, out, proc.stderr

    def test_the_highest_version_not_the_latest_mark(self):
        rc, out, _ = self._pick(["v0.2.0", "v0.10.0", "v0.9.9", "v0.1.1"])
        self.assertEqual((rc, out), (0, "tag=v0.10.0\n"))
        self.assertIn("--paginate", self.args)
        self.assertIn("repos/trailro/hass-remote-integration-manager/releases", self.args)
        self.assertIn(".[] | select(.draft == false and .prerelease == false) | .tag_name", self.args)

    def test_only_an_exact_vxyz_tag(self):
        near = ["v1.0.0-rc1", "v1.0", "1.2.3", "v1.2.3.4", "v01.2.3", "xv9.9.9", "v9.9.9 ", "v9.9.*", "v2.0.0beta"]
        rc, out, _ = self._pick(near + ["v0.3.0"])
        self.assertEqual((rc, out), (0, "tag=v0.3.0\n"))
        rc, out, err = self._pick(near)
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("no vX.Y.Z release of HRI Manager", err)

    def test_an_api_that_cannot_be_asked_is_red(self):
        for fail, tags in ((True, ["v0.3.0"]), (False, [])):
            with self.subTest(fail=fail):
                rc, out, _ = self._pick(tags, fail=fail)
                self.assertNotEqual(rc, 0)
                self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
